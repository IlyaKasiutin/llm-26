"""Проверки покрытия цитат и формул Recall/Precision/MRR/RRF."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(_EVAL.parent / "src"))
sys.path.insert(0, str(_EVAL))

from evaluate import (
    RUN_IDS,
    covers,
    evidence_hits,
    gold_errors,
    load_chunk_texts,
    load_questions,
    macro_average,
    ranking_metrics,
    rrf_fuse,
    top_overlap,
)

ROOT = _EVAL.parent


class CoverTests(unittest.TestCase):
    def test_substring_ignores_whitespace_and_yo(self) -> None:
        self.assertTrue(covers("Учёт  данных", "перед учет\nданных после"))

    def test_empty_quote_is_not_covered(self) -> None:
        self.assertFalse(covers("", "текст"))
        self.assertFalse(covers("текст", ""))

    def test_contiguous_window_accepts_a_split_tail(self) -> None:
        quote = "один два три четыре пять шесть семь восемь девять десять"
        self.assertTrue(covers(quote, "хвост пять шесть семь восемь девять десять"))
        self.assertFalse(covers(quote, "один два три четыре пять"))

    def test_bag_of_words_does_not_count(self) -> None:
        quote = "альфа бета гамма дельта эпсилон зета эта тета"
        shuffled = "тета эта зета эпсилон дельта гамма бета альфа"
        self.assertFalse(covers(quote, shuffled))
        self.assertTrue(covers(quote, "префикс альфа бета гамма дельта эпсилон хвост"))


class MetricTests(unittest.TestCase):
    def test_two_passages_in_one_chunk(self) -> None:
        metrics = ranking_metrics([0, 1, 2, 3, 4], [{0}, {0}])
        self.assertEqual(metrics["recall@5"], 1.0)
        self.assertEqual(metrics["precision@5"], 0.2)
        self.assertEqual(metrics["mrr"], 1.0)

    def test_partial_recall_and_reciprocal_rank(self) -> None:
        metrics = ranking_metrics([0, 1, 2, 3, 4, 5], [{1}, {5}])
        self.assertEqual(metrics["recall@5"], 0.5)
        self.assertEqual(metrics["recall@10"], 1.0)
        self.assertEqual(metrics["precision@5"], 0.2)
        self.assertEqual(metrics["precision@10"], 0.2)
        self.assertAlmostEqual(metrics["mrr"], 0.5)

    def test_miss_is_zero(self) -> None:
        metrics = ranking_metrics([0, 1], [{3}])
        self.assertEqual(metrics["mrr"], 0.0)
        self.assertEqual(metrics["recall@5"], 0.0)
        self.assertEqual(metrics["precision@5"], 0.0)

    def test_macro_average(self) -> None:
        averaged = macro_average([
            {"recall@5": 1.0, "recall@10": 1.0, "precision@5": 0.2, "precision@10": 0.1, "mrr": 1.0},
            {"recall@5": 0.0, "recall@10": 0.5, "precision@5": 0.0, "precision@10": 0.1, "mrr": 0.0},
        ])
        self.assertAlmostEqual(averaged["recall@5"], 0.5)
        self.assertAlmostEqual(averaged["mrr"], 0.5)

    def test_rrf_prefers_the_shared_top(self) -> None:
        self.assertEqual(rrf_fuse([[2, 0], [2]]), [2, 0])

    def test_rrf_tie_breaks_by_index(self) -> None:
        self.assertEqual(rrf_fuse([[0], [1]]), [0, 1])

    def test_top_overlap(self) -> None:
        score = top_overlap([[0, 1, 2]], [[0, 2, 3]], k=2)
        self.assertAlmostEqual(score, 0.5)

    def test_evidence_hits_follow_covers(self) -> None:
        hits = evidence_hits(["альфа бета", "гамма"], [{"quote": "альфа бета"}])
        self.assertEqual(hits, [{0}])


class GoldFileTests(unittest.TestCase):
    def test_quotes_are_in_diploma_and_both_chunk_sets(self) -> None:
        questions, source = load_questions(ROOT / "eval" / "questions.json")
        self.assertGreaterEqual(len(questions), 10)
        self.assertLessEqual(len(questions), 20)
        diploma = (ROOT / source).read_text(encoding="utf-8")
        chunk_sets = {}
        for run_id in RUN_IDS:
            path = ROOT / "artifacts" / run_id / "chunks.jsonl"
            if not path.is_file():
                self.skipTest(f"нет {path}")
            chunk_sets[run_id] = load_chunk_texts(path)
        self.assertEqual(gold_errors(questions, diploma, chunk_sets), [])


if __name__ == "__main__":
    unittest.main()
