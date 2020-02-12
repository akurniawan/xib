import logging
from abc import ABCMeta
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional

import torch
import torch.optim as optim
from torch.nn.utils.clip_grad import clip_grad_norm_

from dev_misc import get_tensor
from dev_misc.arglib import add_argument, g
from dev_misc.trainlib import (Metric, Metrics, freeze, get_grad_norm,
                               get_trainable_params)
from dev_misc.trainlib.base_trainer import BaseTrainer as BaseTrainerDev
from dev_misc.trainlib.tb_writer import MetricWriter
from dev_misc.utils import deprecated, global_property, pbar
from xib.data_loader import ContinuousTextDataLoader, IpaDataLoader
from xib.model.decipher_model import DecipherModel
from xib.model.extract_model import ExtractModel
from xib.model.lm_model import LM, AdaptLM
from xib.training.analyzer import (AdaptLMAnalyzer, DecipherAnalyzer,
                                   ExtractAnalyzer, LMAnalyzer)
from xib.training.optim import AdamInverseSqrtWithWarmup


class BaseTrainer(BaseTrainerDev, metaclass=ABCMeta):  # pylint: disable=abstract-method

    add_argument('num_steps', default=10, dtype=int, msg='number of steps to train')
    add_argument('learning_rate', default=2e-3, dtype=float, msg='learning rate')
    add_argument('check_interval', default=2, dtype=int, msg='check metrics after this many steps')
    add_argument('eval_interval', default=500, dtype=int, msg='save models after this many steps')
    add_argument('save_interval', default=0, dtype=int, msg='save models after this many steps')

    def save_to(self, path: Path):
        to_save = {
            'model': self.model.state_dict(),
            'g': g.state_dict(),
            'optimizer': self.optimizer.state_dict()
        }
        torch.save(to_save, path)
        logging.imp(f'Model saved to {path}.')


class LMTrainer(BaseTrainer):

    add_argument('feat_groups', default='pcvdst', dtype=str,
                 msg='what to include during training: p(type), c(onstonant), v(vowel), d(iacritics), s(tress) and t(one).')

    model: LM
    analyzer_cls: ClassVar = LMAnalyzer

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # IDEA(j_luo) Preparing the trainer should be handled by the manager, not by __init__ call.
        logging.warning('Init model.')
        for p in get_trainable_params(self.model, named=False):
            if p.ndim == 2:
                torch.nn.init.xavier_uniform_(p)

        self.set_optimizer(optim.Adam, lr=g.learning_rate)
        self.analyzer = self.analyzer_cls()

    def add_trackables(self):
        self.tracker.add_trackable('total_step', total=g.num_steps)
        self.tracker.add_min_trackable('best_loss')

    def train_one_step(self, dl: IpaDataLoader) -> Metrics:
        self.model.train()
        self.optimizer.zero_grad()
        batch = dl.get_next_batch()
        ret = self.model.score(batch)
        # for idx, segment in enumerate(batch.segments):
        #     if str(segment).startswith('e-s-t-a-n'):
        #         break
        # from xib.ipa import Name
        # name = Name('Ptype', 'camel')
        # print(torch.stack([ret.distr[name][0], ret.distr_noise[name][0]], new_name='tmp')[idx])
        # import time; time.sleep(1)
        metrics = self.analyzer.analyze(ret)
        metrics.loss.mean.backward()
        grad_norm = get_grad_norm(self.model)
        grad_norm = Metric('grad_norm', grad_norm * len(batch), len(batch))
        metrics += grad_norm
        self.optimizer.step()
        return metrics

    def save(self, eval_metrics: Metrics):
        new_value = eval_metrics.loss.mean
        self.save_to(g.log_dir / 'saved.latest')
        if self.tracker.update('best_loss', value=new_value):
            out_path = g.log_dir / 'saved.best'
            logging.imp(f'Best model updated: new best is {self.tracker.best_loss:.3f}')
            self.save_to(out_path)


class AdaptLMTrainer(LMTrainer):

    model: AdaptLM
    analyzer_cls: ClassVar = AdaptLMAnalyzer


class DecipherTrainer(BaseTrainer):

    add_argument('score_per_word', default=1.0, dtype=float, msg='score added for each word')
    add_argument('concentration', default=1e-2, dtype=float, msg='concentration hyperparameter')
    add_argument('supervised', dtype=bool, default=False, msg='supervised mode')
    # add_argument('mode', default='local-supervised', dtype=str,
    #              choices=['local-supervised', 'global-supervised'], msg='training mode')
    add_argument('mlm_coeff', dtype=float, default=0.05, msg='Flag to use mlm loss.')
    add_argument('warmup_updates', dtype=int, default=4000, msg='Number of warmup updates for Adam.')

    model: DecipherModel

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.analyzer = DecipherAnalyzer()
        self.set_optimizer()

    def set_optimizer(self):
        super().set_optimizer(AdamInverseSqrtWithWarmup,
                              lr=g.learning_rate, betas=(0.9, 0.98),
                              warmup_updates=g.warmup_updates)

    def add_trackables(self):
        self.tracker.add_trackable('total_step', total=g.num_steps)
        self.tracker.add_max_trackable('best_f1')

    def train_one_step(self, dl: ContinuousTextDataLoader) -> Metrics:
        self.model.train()
        self.optimizer.zero_grad()
        batch = dl.get_next_batch()
        ret = self.model(batch)
        metrics = self.analyzer.analyze(ret, batch)
        metrics.total_loss.mean.backward()
        self.optimizer.step()
        grad_norm = clip_grad_norm_(self.model.parameters(), 5.0)
        weight = (~batch.source_padding).sum()
        metrics += Metric('grad_norm', grad_norm * weight, weight)
        return metrics.with_prefix_('decipher')

    def load(self, path: Path, load_lm_model: bool = False, load_optimizer_state: bool = False, load_phi_scorer: bool = False):
        saved = torch.load(path)
        smsd = saved['model']
        if not load_lm_model:
            smsd = {k: v for k, v in smsd.items() if not k.startswith('lm_model')}
        if not load_phi_scorer:
            smsd = {k: v for k, v in smsd.items() if not k.startswith('phi_scorer')}
        self.model.load_state_dict(smsd, strict=False)
        if load_optimizer_state:
            self.optimizer.load_state_dict(saved['optimizer'])
        logging.imp(f'Loading model from {path}.')

    def save(self, eval_metrics: Metrics):
        self.save_to(g.log_dir / 'saved.latest')
        # self.tracker.update('best_loss', value=eval_metrics.dev_total_loss.mean)
        if self.tracker.update('best_f1', value=eval_metrics.dev_prf_f1.value):
            out_path = g.log_dir / f'saved.best'
            logging.imp(f'Best model updated: new best is {self.tracker.best_f1:.3f}')
            self.save_to(out_path)


add_argument('accum_gradients', default=1, dtype=int, msg='Accumulate this many steps of gradients.')


class ExtractTrainer(BaseTrainer):

    model: ExtractModel

    add_argument('reg_hyper', default=1.0, dtype=float, msg='Hyperparameter for alignment regularization.')
    add_argument('pr_hyper', default=1.0, dtype=float)
    add_argument('main_loss_hyper', default=1.0, dtype=float)
    add_argument('l_pr_hyper', default=1.0, dtype=float)
    add_argument('coverage_hyper', default=1.0, dtype=float)
    add_argument('weight_hyper', default=0.0, dtype=float)
    add_argument('save_alignment', default=False, dtype=bool, msg='Flag to save alignment every step.')
    add_argument('update_p_weights', default=False, dtype=bool, msg='Flag to save alignment every step.')
    add_argument('p_weight_inc', default=50, dtype=int)
    add_argument('take_best_span', default=False, dtype=bool,
                 msg='Flag to take the best ll instead of the marginal ll.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.analyzer = ExtractAnalyzer()
        self.ins_del_cost = g.init_ins_del_cost
        if g.save_alignment:
            self.add_callback('total_step', 1, self.save_alignment)
        # if g.update_p_weights:
        #     self.add_callback('total_step', g.num_steps, self.update_p_weights)
        self.metric_writer = MetricWriter(log_dir=g.log_dir, flush_secs=5)
        self.add_callback('check', self.check_interval, self.write_summaries)
        # HACK(j_luo)
        self._cnt = 100

    def update_p_weights(self):
        sentences, spans = self.evaluator.get_best_spans(self.stage, self._cnt)
        self.model.span_proposer.update(sentences, spans)
        self._cnt += g.p_weight_inc

    def write_summaries(self, metrics: Metrics):
        metrics = metrics.with_prefix_('check')
        self.metric_writer.add_metrics(metrics, self.global_step)
        self.metric_writer.add_histogram(
            'unit_aligner', self.model.unit_aligner.weight.data, global_step=self.global_step)

    def save_alignment(self):
        if g.input_format == 'text':
            try:
                to_save = {
                    'unit_aligner': self.model.g2p.unit_aligner.state_dict(),
                }
            except:
                to_save = {
                    'alignment': self.model.get_alignment().rename(None)
                }
        else:
            to_save = {
                'adapter': self.model.adapter.state_dict()
            }
        path = g.log_dir / 'almt' / f'saved.{self.stage}.almt'
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(to_save, path)
        logging.debug(f'Alignment saved to {path}.')

    @global_property
    def ins_del_cost(self):
        pass

    @ins_del_cost.setter
    def ins_del_cost(self, value):
        logging.imp(f'Setting ins_del_cost to {value}.')

    @global_property
    def threshold(self):
        pass

    @threshold.setter
    def threshold(self, value):
        logging.imp(f'Setting threshold to {value}.')

    @global_property
    def er(self):
        pass

    @er.setter
    def er(self, value):
        logging.imp(f'Setting er to {value}.')

    def add_trackables(self):
        self.tracker.add_trackable('round', endless=True)
        self.tracker.add_trackable('total_step', total=g.num_steps)
        self.tracker.add_max_trackable('best_f1')

    def reset(self, reset_params: bool = False):
        """Reset the tracker. But keep the best_f1 since it's related to evaluation."""
        self.tracker.reset('total_step')
        if reset_params:
            logging.imp('Init params')
            self.model.unit_aligner.weight.data.fill_(0.0)

    def load(self, path: Path):
        saved = torch.load(path)
        smsd = saved['model']
        self.model.load_state_dict(smsd, strict=False)
        logging.imp(f'Loading model from {path}.')

    def save(self, eval_metrics: Metrics):
        self.save_to(g.log_dir / f'saved.{self.stage}.latest')
        if eval_metrics is not None:
            if self.tracker.update('best_f1', value=eval_metrics.prf_exact_span_f1.value):
                out_path = g.log_dir / f'saved.{self.stage}.best'
                logging.warning('Do NOT use this number since this f1 is compared against ground truths.')
                logging.imp(f'Best model updated: new best is {self.tracker.best_f1:.3f}')
                self.save_to(out_path)

    @property
    def global_step(self) -> int:
        # HACK(j_luo)
        nr, ns = map(int, self.stage.split('_'))
        global_step = nr * g.num_steps + ns
        return global_step

    def evaluate(self):
        eval_metrics = super().evaluate()
        self.metric_writer.add_metrics(eval_metrics, self.global_step)

    def should_terminate(self):
        return self.tracker.is_finished('total_step')

    def train_one_step(self, dl: ContinuousTextDataLoader) -> Metrics:
        self.model.train()
        self.optimizer.zero_grad()
        accum_metrics = Metrics()

        for _ in pbar(range(g.accum_gradients), desc='accum_gradients'):
            batch = dl.get_next_batch()
            ret = self.model(batch)
            metrics = self.analyzer.analyze(ret, batch)

            wc = Metric('weight', self.model.unit_aligner.weight.abs().sum(), batch.batch_size)
            metrics += wc

            if g.take_best_span:
                loss = -metrics.best_ll.mean
            else:
                loss = -metrics.marginal.mean

            if g.use_posterior_reg:
                # loss = loss + g.pr_hyper * (metrics.posterior_spans.mean -
                #                             g.expected_ratio).clamp(max=0.0).abs()  # ** 2

                pr_loss = (metrics.posterior_spans.mean - self.er).clamp(max=0.0).abs()
                # pr_loss = (metrics.posterior_spans.mean - g.expected_ratio).clamp(max=0.0) ** 2
                # pr_loss = -metrics.posterior_spans.mean
                l_pr_loss = -metrics.avg_log_probs.mean
                loss = g.main_loss_hyper * loss + g.pr_hyper * pr_loss + g.l_pr_hyper * l_pr_loss

            if g.use_constrained_learning:
                loss = g.main_loss_hyper * loss + g.coverage_hyper * (metrics.coverage.mean - g.expected_ratio) ** 2

            try:
                loss = loss + metrics.reg.mean * g.reg_hyper
            except AttributeError:
                pass

            loss = loss + g.weight_hyper * wc.mean

            loss_per_split = loss / g.accum_gradients
            loss_per_split.backward()

            accum_metrics += metrics

        grad_norm = clip_grad_norm_(self.model.parameters(), 5.0)
        self.optimizer.step()
        # FIXME(j_luo) avg for grad_norm might be wrong.
        accum_metrics += Metric('grad_norm', grad_norm * batch.batch_size, batch.batch_size)

        return accum_metrics
