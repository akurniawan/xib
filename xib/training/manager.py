import logging
import os
import random

import torch
import torch.nn as nn
from torch.optim import Adam

from dev_misc import g
from dev_misc.arglib import add_argument, init_g_attr
from dev_misc.trainlib import Metrics, has_gpus, set_random_seeds
from dev_misc.trainlib.trainer import freeze
from dev_misc.utils import deprecated
from xib.data_loader import (ContinuousTextDataLoader, DataLoaderRegistry,
                             DenseIpaDataLoader, IpaDataLoader)
from xib.model.decipher_model import DecipherModel
from xib.model.extract_model import ExtractModel
from xib.model.lm_model import LM, AdaptedLM
from xib.search.search_solver import SearchSolver
from xib.search.searcher import BruteForceSearcher
from xib.training.evaluator import (DecipherEvaluator, ExtractEvaluator,
                                    LMEvaluator, SearchSolverEvaluator)
from xib.training.task import (DecipherTask, ExtractTask, LMTask, MlmTask,
                               TransferTask)
from xib.training.trainer import DecipherTrainer, ExtractTrainer, LMTrainer

add_argument('task', default='lm', dtype=str, choices=['lm', 'decipher', 'search', 'extract'], msg='which task to run')


class LMManager:

    def __init__(self):
        self.model = LM()
        if has_gpus():
            self.model.cuda()

        task = LMTask()
        self.dl_reg = DataLoaderRegistry()
        self.dl_reg.register_data_loader(task, g.data_path)
        self.evaluator = LMEvaluator(self.model, self.dl_reg[task])
        self.trainer = LMTrainer(self.model, [task], [1.0], 'total_step',
                                 evaluator=self.evaluator,
                                 check_interval=g.check_interval,
                                 eval_interval=g.eval_interval)

    def run(self):
        self.trainer.train(self.dl_reg)


# class AdaptManager(Manager):

#     data_loader_cls = DenseIpaDataLoader
#     trainer_cls = AdaptLMTrainer

#     def _get_model(self):
#         return AdaptedLM()


class DecipherManager:

    add_argument('dev_data_path', dtype='path', msg='Path to dev data.')
    add_argument('aux_train_data_path', dtype='path', msg='Path to aux train data.')
    add_argument('in_domain_dev_data_path', dtype='path', msg='Path to in-domain dev data.')
    add_argument('saved_path', dtype='path')
    add_argument('saved_model_path', dtype='path', msg='Path to a saved model, skipping the local training phase.')
    add_argument('train_phi', dtype=bool, default=False,
                 msg='Flag to train phi score. Used only with supervised mode.')
    add_argument('fix_phi', dtype=bool, default=False, msg='Flag fix phi scorer.')
    # add_argument('use_mlm_loss', dtype=bool, default=False, msg='Flag to use mlm loss.')

    def __init__(self):
        self.model = DecipherModel()
        if has_gpus():
            self.model.cuda()

        train_task = DecipherTask('train')
        dev_task = DecipherTask('dev')
        self.dl_reg = DataLoaderRegistry()
        eval_tasks = [train_task, dev_task]
        if g.in_domain_dev_data_path:
            in_domain_dev_task = DecipherTask('in_domain_dev')
            self.dl_reg.register_data_loader(in_domain_dev_task, g.in_domain_dev_data_path)
            eval_tasks.append(in_domain_dev_task)
        train_tasks = [train_task]
        if g.aux_train_data_path:
            aux_train_task = DecipherTask('aux_train')
            self.dl_reg.register_data_loader(aux_train_task, g.aux_train_data_path)
            train_tasks.append(aux_train_task)

        self.dl_reg.register_data_loader(train_task, g.data_path)
        self.dl_reg.register_data_loader(dev_task, g.dev_data_path)
        self.evaluator = DecipherEvaluator(self.model, self.dl_reg, eval_tasks)

        self.trainer = DecipherTrainer(self.model, train_tasks, [1.0] * len(train_tasks), 'total_step',
                                       evaluator=self.evaluator,
                                       check_interval=g.check_interval,
                                       eval_interval=g.eval_interval)
        if g.train_phi:
            freeze(self.model.self_attn_layers)
            freeze(self.model.positional_embedding)
            freeze(self.model.emb_for_label)
            freeze(self.model.label_predictor)
        if g.saved_model_path:
            self.trainer.load(g.saved_model_path, load_phi_scorer=True)
            if g.fix_phi:
                freeze(self.model.phi_scorer)
        #     freeze(self.model.self_attn_layers)
        #     freeze(self.model.positional_embedding)
        #     freeze(self.model.emb_for_label)
        #     freeze(self.model.label_predictor)
            self.trainer.set_optimizer()

    def run(self):
        self.trainer.train(self.dl_reg)


class SearchSolverManager:
    """
    On tmp:
    P: 553 / 950 = 0.5821052631578948
    R: 553 / 978 = 0.565439672801636
    F: 0.5736514522821577

    From 50 samples:
    19 errors in total: 12 from vocab, 4 merged, 3 tricky/maybe solvable by counts.

    After fixing merged:
    P: 559 / 922 = 0.60629067245
    R: 559 / 978 = 0.57157464212
    F: 0.5884210526274193

    After matching prefixes:
    P: 673 / 922 = 0.72993492407
    R: 673 / 978 = 0.6881390593
    F: 0.708421052625276

    Removing #words in the objective #chars - #words only marginally brings down the numbers. 556/665 matches for exact/prefix out of 928 predictions
    compared with 559/673 out of 922. UPDATE: new run gives 567/680 out of 928, actually better without it.
    """

    def run(self):
        dl_reg = DataLoaderRegistry()
        dev_task = DecipherTask('dev')
        dl_reg.register_data_loader(dev_task, g.dev_data_path)
        dl = dl_reg[dev_task]

        with open(g.vocab_path, 'r', encoding='utf8') as fin:
            vocab = set(line.strip() for line in fin)

        solver = SearchSolver(vocab, g.max_num_words)

        evaluator = SearchSolverEvaluator(solver)
        prf_scores = evaluator.evaluate(dl)
        logging.info(prf_scores.get_table())


class ExtractManager:

    def __init__(self):
        self.model = ExtractModel()
        if has_gpus():
            self.model.cuda()

        task = ExtractTask()
        self.dl_reg = DataLoaderRegistry()
        self.dl_reg.register_data_loader(task, g.data_path)
        self.evaluator = ExtractEvaluator(self.model, self.dl_reg[task])

        self.trainer = ExtractTrainer(self.model, [task], [1.0], 'total_step',
                                      evaluator=self.evaluator,
                                      check_interval=g.check_interval,
                                      eval_interval=g.eval_interval)
        if g.saved_model_path:
            self.trainer.load(g.saved_model_path)
        self.trainer.set_optimizer(Adam, lr=g.learning_rate)

    def run(self):
        self.trainer.train(self.dl_reg)
