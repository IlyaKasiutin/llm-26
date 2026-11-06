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
from langchain_core.documents import Document

from config import Config
from index.splitters import split_documents

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


class SplitterBudgetTests(unittest.TestCase):
    @staticmethod
    def _word_len(text: str) -> int:
        return len(text.split())

    def _embed_len(self, config: Config):
        prefix = config.embedding.document_prefix
        return lambda text: len((prefix + text).split()) + 2  # +2 спецтокена (BOS/EOS)

    def test_nested_headings_prepended_without_duplicate_markdown_header(self) -> None:
        config = Config.model_validate(
            {"splitter": {"type": "markdown_header", "chunk_size": 50, "chunk_overlap": 5}}
        )
        page = Document(
            page_content="# Глава 1\n## Раздел 1.1\n### Подраздел 1.1.1\nПервый абзац текста.",
            metadata={"source": "doc.pdf", "page": 1},
        )
        chunks = split_documents(
            [page], config, length_fn=self._word_len, embed_length_fn=self._embed_len(config)
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(
            chunks[0].page_content,
            "Глава 1 > Раздел 1.1 > Подраздел 1.1.1\n\nПервый абзац текста.",
        )
        self.assertNotIn("###", chunks[0].page_content)

    def test_header_only_page_is_preserved(self) -> None:
        config = Config.model_validate(
            {"splitter": {"type": "markdown_header", "chunk_size": 50, "chunk_overlap": 5}}
        )
        page = Document(
            page_content="## Титульный лист\n### Университет ИТМО",
            metadata={"source": "doc.pdf", "page": 1, "section": "Титульный лист"},
        )
        chunks = split_documents(
            [page], config, length_fn=self._word_len, embed_length_fn=self._embed_len(config)
        )
        self.assertEqual(len(chunks), 1)
        self.assertIn("Университет ИТМО", chunks[0].page_content)

    def test_chunks_strictly_fit_chunk_size_and_embedder_window(self) -> None:
        config = Config.model_validate(
            {
                "splitter": {
                    "type": "markdown_header",
                    "chunk_size": 20,
                    "chunk_overlap": 3,
                    "include_headings": "both",
                },
                "embedding": {"max_seq_length": 22, "document_prefix": "search_document: "},
            }
        )
        body = " ".join(f"слово{i}" for i in range(60))
        page = Document(
            page_content=f"# Глава первая\n## Раздел второй\n### Подраздел третий\n{body}",
            metadata={"source": "doc.pdf", "page": 2},
        )
        chunks = split_documents(
            [page], config, length_fn=self._word_len, embed_length_fn=self._embed_len(config)
        )
        self.assertGreater(len(chunks), 3)
        for chunk in chunks:
            self.assertTrue(
                chunk.page_content.startswith("Глава первая > Раздел второй > Подраздел третий\n\n")
            )
            self.assertLessEqual(chunk.metadata["n_tokens"], config.splitter.chunk_size)
            self.assertLessEqual(chunk.metadata["embed_tokens"], config.embedding.max_seq_length)

    def test_h1_carries_across_pages_to_h2_subsections(self) -> None:
        config = Config.model_validate(
            {"splitter": {"type": "markdown_header", "chunk_size": 50, "chunk_overlap": 5}}
        )
        pages = [
            Document(
                page_content="# ГЛАВА 2 Сравнительный анализ\nВводный текст главы.\n## 2.1 Показатели\nТекст 2.1.",
                metadata={"source": "doc.pdf", "page": 32},
            ),
            Document(
                page_content="Продолжение 2.1.\n## 2.2 Модели\nТекст 2.2.",
                metadata={"source": "doc.pdf", "page": 33},
            ),
        ]
        chunks = split_documents(
            pages, config, length_fn=self._word_len, embed_length_fn=self._embed_len(config)
        )
        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[2].metadata["h1"], "ГЛАВА 2 Сравнительный анализ")
        self.assertEqual(chunks[2].metadata["h2"], "2.1 Показатели")
        self.assertTrue(
            chunks[3].page_content.startswith("ГЛАВА 2 Сравнительный анализ > 2.2 Модели\n\n")
        )
        self.assertEqual(chunks[3].metadata["h1"], "ГЛАВА 2 Сравнительный анализ")
        self.assertEqual(chunks[3].metadata["h2"], "2.2 Модели")


if __name__ == "__main__":
    unittest.main()
