from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
import torch

from dev_misc import add_argument, g
from dev_misc.arglib import g
from dev_misc.devlib import get_range
from dev_misc.devlib.named_tensor import NoName, get_named_range
from dev_misc.trainlib import Metric, Metrics
from dev_misc.trainlib.tracker.trackable import BaseTrackable
from dev_misc.trainlib.tracker.tracker import Tracker
from dev_misc.utils import deprecated, pbar
from xib.data_loader import (ContinuousTextDataLoader, ContinuousTextIpaBatch,
                             DataLoaderRegistry)
from xib.ipa.process import Segmentation, Span
from xib.model.decipher_model import (DecipherModel, DecipherModelReturn,
                                      Segmentation, Span)
from xib.model.extract_model import ExtractModel, ExtractModelReturn
from xib.search.search_solver import SearchSolver
from xib.training.analyzer import DecipherAnalyzer, ExtractAnalyzer, LMAnalyzer
from xib.training.task import DecipherTask


class BaseEvaluator(ABC):

    def __init__(self, model, data_loader):
        self.model = model
        self.data_loader = data_loader

    @abstractmethod
    def evaluate(self): ...


class LMEvaluator(BaseEvaluator):
    """An evaluator class for LMs. Note that this is done over the entire training corpus, instead of a separate split."""

    def __init__(self, model, data_loader):
        super().__init__(model, data_loader)
        self.analyzer = LMAnalyzer()

    def evaluate(self, *args) -> Metrics:  # HACK(j_luo) *args is used just comply with BaseTrainer function signature.
        with torch.no_grad():
            self.model.eval()
            all_metrics = Metrics()
            total_num_samples = 0
            for batch in self.data_loader:
                if g.eval_max_num_samples and total_num_samples + batch.batch_size > g.eval_max_num_samples:
                    logging.imp(
                        f'Stopping at {total_num_samples} < {g.eval_max_num_samples} evaluated examples from.')
                    break

                scores = self.model.score(batch)
                try:
                    metrics = self.analyzer.analyze(scores.distr)
                except AttributeError:
                    metrics = self.analyzer.analyze(scores)
                all_metrics += metrics

                total_num_samples += batch.batch_size
        return all_metrics


# @dataclass
# class PrfScores:
#     exact_matches: int
#     prefix_matches: int
#     total_correct: int
#     total_pred: int

#     @property
#     def precision(self):
#         return self.exact_matches / (self.total_pred + 1e-8)

#     @property
#     def recall(self):
#         return self.exact_matches / (self.total_correct + 1e-8)

#     @property
#     def f1(self):
#         return 2 * self.precision * self.recall / (self.precision + self.recall + 1e-8)

#     def __add__(self, other: PrfScores) -> PrfScores:
#         return PrfScores(self.exact_matches + other.exact_matches, self.total_correct + other.total_correct, self.total_pred + other.total_pred)


def get_matching_stats(predictions: List[Segmentation], ground_truths: List[Segmentation], match_words: bool = False) -> Metrics:
    exact_span_matches = 0
    prefix_span_matches = 0
    if match_words:
        exact_word_matches = 0
        prefix_word_matches = 0
    for pred, gt in zip(predictions, ground_truths):
        for p in pred:
            for g in gt:
                if p.is_same_span(g):
                    exact_span_matches += 1
                    prefix_span_matches += 1
                    if match_words and p.is_same_word(g):
                        exact_word_matches += 1
                        prefix_word_matches += 1
                elif p.is_prefix_span_of(g) or g.is_prefix_span_of(p):
                    prefix_span_matches += 1
                    if match_words and (p.is_prefix_word_of(g) or g.is_prefix_word_of(p)):
                        prefix_word_matches += 1

    total_correct = sum(map(len, ground_truths))
    total_pred = sum(map(len, predictions))
    exact_span_matches = Metric(f'prf_exact_span_matches', exact_span_matches, 1.0, report_mean=False)
    prefix_span_matches = Metric(f'prf_prefix_span_matches', prefix_span_matches, 1.0, report_mean=False)
    total_correct = Metric(f'prf_total_correct', total_correct, 1.0, report_mean=False)
    total_pred = Metric(f'prf_total_pred', total_pred, 1.0, report_mean=False)
    metrics = Metrics(exact_span_matches, prefix_span_matches, total_correct, total_pred)

    if match_words:
        exact_word_matches = Metric(f'prf_exact_word_matches', exact_word_matches, 1.0, report_mean=False)
        prefix_word_matches = Metric(f'prf_prefix_word_matches', prefix_word_matches, 1.0, report_mean=False)
        metrics += Metrics(exact_word_matches, prefix_word_matches)
    return metrics


def get_prf_scores(metrics: Metrics) -> Metrics:
    prf_scores = Metrics()

    def _get_f1(p, r):
        return 2 * p * r / (p + r + 1e-8)

    exact_span_matches = getattr(metrics, f'prf_exact_span_matches').total
    prefix_span_matches = getattr(metrics, f'prf_prefix_span_matches').total
    total_pred = getattr(metrics, f'prf_total_pred').total
    total_correct = getattr(metrics, f'prf_total_correct').total
    exact_span_precision = exact_span_matches / (total_pred + 1e-8)
    exact_span_recall = exact_span_matches / (total_correct + 1e-8)
    exact_span_f1 = _get_f1(exact_span_precision, exact_span_recall)
    prefix_span_precision = prefix_span_matches / (total_pred + 1e-8)
    prefix_span_recall = prefix_span_matches / (total_correct + 1e-8)
    prefix_span_f1 = _get_f1(prefix_span_precision, prefix_span_recall)
    prf_scores += Metric(f'prf_exact_span_precision', exact_span_precision, report_mean=False)
    prf_scores += Metric(f'prf_exact_span_recall', exact_span_recall, 1.0, report_mean=False)
    prf_scores += Metric(f'prf_exact_span_f1', exact_span_f1, 1.0, report_mean=False)
    prf_scores += Metric(f'prf_prefix_span_precision', prefix_span_precision, report_mean=False)
    prf_scores += Metric(f'prf_prefix_span_recall', prefix_span_recall, 1.0, report_mean=False)
    prf_scores += Metric(f'prf_prefix_span_f1', prefix_span_f1, 1.0, report_mean=False)
    return prf_scores

# @deprecated


class DecipherEvaluator(BaseEvaluator):

    add_argument('eval_max_num_samples', default=0, dtype=int, msg='Max number of samples to evaluate on.')

    def __init__(self, model: DecipherModel, dl_reg: DataLoaderRegistry, tasks: Sequence[DecipherTask]):
        self.model = model
        self.dl_reg = dl_reg
        self.tasks = tasks
        self.analyzer = DecipherAnalyzer()

    def evaluate(self, tracker: Tracker) -> Metrics:
        metrics = Metrics()
        with torch.no_grad():
            self.model.eval()
            for task in self.tasks:
                dl = self.dl_reg[task]
                task_metrics = self._evaluate_one_data_loader(dl, tracker)
                metrics += task_metrics.with_prefix_(task)
        return metrics

    def _evaluate_one_data_loader(self, dl: ContinuousTextDataLoader, tracker: Tracker) -> Metrics:
        task = dl.task
        accum_metrics = Metrics()

        # Get all metrics from batches.
        dfs = list()
        total_num_samples = 0
        for batch in dl:
            if g.eval_max_num_samples and total_num_samples + batch.batch_size > g.eval_max_num_samples:
                logging.imp(
                    f'Stopping at {total_num_samples} < {g.eval_max_num_samples} evaluated examples from {task}.')
                break

            model_ret = self.model(batch)

            batch_metrics, batch_df = self.predict(model_ret, batch)
            accum_metrics += batch_metrics
            # accum_metrics += self.analyzer.analyze(model_ret, batch)
            total_num_samples += batch.batch_size
            dfs.append(batch_df)

        df = pd.concat(dfs, axis=0)
        # Write the predictions to file.
        out_path = g.log_dir / 'predictions' / f'{task}.{tracker.total_step}.tsv'
        out_path.parent.mkdir(exist_ok=True, parents=True)
        df.to_csv(out_path, index=None, sep='\t')

        # Compute P/R/F scores.
        accum_metrics += get_prf_scores(accum_metrics)
        return accum_metrics

    def _get_predictions(self, model_ret: DecipherModelReturn, batch: ContinuousTextIpaBatch) -> List[Segmentation]:
        label_log_probs = model_ret.probs.label_log_probs.align_to('batch', 'length', 'label')
        _, tag_seqs = label_log_probs.max(dim='label')
        tag_seqs = tag_seqs.align_to('batch', 'sample', 'length').int()
        lengths = batch.lengths.align_to('batch', 'sample').int()
        segment_list = None
        if self.model.vocab is not None:
            segment_list = [segment.segment_list for segment in batch.segments]
        packed_words = self.model.pack(
            tag_seqs, lengths, batch.feat_matrix, batch.segments, segment_list=segment_list)
        segments_by_batch = packed_words.sampled_segments_by_batch
        # Only take the first (and only) sample.
        predictions = [segments[0] for segments in segments_by_batch]
        return predictions

    def predict(self, model_ret: DecipherModelReturn, batch: ContinuousTextIpaBatch) -> Tuple[Metrics, pd.DataFrame]:
        metrics = Metrics()
        predictions = self._get_predictions(model_ret, batch)
        ground_truths = [segment.to_segmentation() for segment in batch.segments]
        matching_stats = get_matching_stats(predictions, ground_truths)
        metrics += matching_stats

        df = _get_df(batch.segments, ground_truths, predictions)

        return metrics, df


def _get_df(*seqs: Sequence, columns=('segment', 'ground_truth', 'prediction')):
    data = map(lambda x: map(str, x), zip(*seqs))
    df = pd.DataFrame(data, columns=columns)
    return df


class SearchSolverEvaluator(BaseEvaluator):

    def __init__(self, solver: SearchSolver):
        self.solver = solver

    def evaluate(self, dl: ContinuousTextDataLoader) -> Metrics:
        segments = list()
        ground_truths = list()
        predictions = list()
        for batch in pbar(dl, desc='eval_batch'):
            for segment in batch.segments:
                segments.append(segment)
                ground_truth = segment.to_segmentation()
                ground_truths.append(ground_truth)

                best_value, best_state = self.solver.find_best(segment)
                prediction = Segmentation(best_state.spans)
                predictions.append(prediction)

        df = _get_df(segments, ground_truths, predictions)
        out_path = g.log_dir / 'predictions' / 'search_solver.tsv'
        out_path.parent.mkdir(exist_ok=True, parents=True)
        df.to_csv(out_path, index=None, sep='\t')
        matching_stats = get_matching_stats(predictions, ground_truths)
        prf_scores = get_prf_scores(matching_stats)
        return matching_stats + prf_scores


class ExtractEvaluator(BaseEvaluator):

    add_argument('matched_threshold', default=0.99, dtype=float,
                 msg='Value of threshold to determine whether two words are matched.')

    def __init__(self, model: ExtractModel, dl: ContinuousTextDataLoader):
        self.model = model
        self.dl = dl
        self.analyzer = ExtractAnalyzer()

    def evaluate(self, tracker: Tracker) -> Metrics:
        segments = list()
        predictions = list()
        ground_truths = list()
        matched_segments = list()
        total_num_samples = 0
        for batch in pbar(self.dl, desc='eval_batch'):

            if g.eval_max_num_samples and total_num_samples + batch.batch_size > g.eval_max_num_samples:
                logging.imp(
                    f'Stopping at {total_num_samples} < {g.eval_max_num_samples} evaluated examples.')
                break

            ret = self.model(batch)
            segments.extend(list(batch.segments))
            segmentations, _matched_segments = self._get_segmentations(ret, batch)
            predictions.extend(segmentations)
            matched_segments.extend(_matched_segments)
            ground_truths.extend([segment.to_segmentation() for segment in batch.segments])
            total_num_samples += batch.batch_size

        df = _get_df(segments, ground_truths, predictions, matched_segments,
                     columns=('segment', 'ground_truth', 'prediction', 'matched_segment'))
        out_path = g.log_dir / 'predictions' / f'extract.{tracker.total_step}.tsv'
        out_path.parent.mkdir(exist_ok=True, parents=True)
        df.to_csv(out_path, index=None, sep='\t')
        matching_stats = get_matching_stats(predictions, ground_truths)
        prf_scores = get_prf_scores(matching_stats)
        return matching_stats + prf_scores

    def _get_segmentations(self, model_ret: ExtractModelReturn, batch: ContinuousTextIpaBatch) -> Tuple[List[Segmentation], np.ndarray]:
        matches = model_ret.extracted.matches
        ed_dist = matches.ed_dist
        # Get the best matched ed_dist scores.
        bs = ed_dist.size('batch')
        bi = get_named_range(bs, 'batch')
        start = model_ret.start
        end = model_ret.end
        bmv = model_ret.best_matched_vocab
        with NoName(bi, start, end, bmv, ed_dist):
            bmed = ed_dist[bi, start, end - start - g.min_word_length + 1, bmv]  # Best matched edit distance
        bmed.rename_('batch')
        matched = bmed < g.matched_threshold

        start = start.cpu().numpy()
        end = end.cpu().numpy()
        bmv = bmv.cpu().numpy()
        bmw = self.model.vocab[bmv]  # Best matched word

        segmentations = list()
        matched_segments = list()
        for segment, s, e, m, w in zip(batch.segments, start, end, matched, bmw):
            spans = list()
            if m:
                span = [segment[i] for i in range(s, e + 1)]
                span = Span('-'.join(span), s, e)
                spans.append(span)
                matched_segments.append(w)
            segmentations.append(Segmentation(spans))
        return segmentations, matched_segments
