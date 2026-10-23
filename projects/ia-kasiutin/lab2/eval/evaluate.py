"""Метрики retrieval (п.5): эталонные цитаты, сетка FAISS, Recall/Precision/MRR.

Запуск из lab2/eval:
    python evaluate.py
Эталон: questions.json рядом со скриптом.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import re
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import faiss
import numpy as np

from index.config import Config, load_config
from index.embedders import bm25_tokenize
from index.load_to_vector_store import (
    ACTIVE_FILE,
    INDEX_FILE,
    build_collection,
    set_ef_search,
)

KS = (5, 10)
COVERAGE = 0.6
RRF_K = 60
EF_SEARCH = (16, 64, 128)
RUN_IDS = (
    "dense-frida-markdown_header-cs400-ov50-dcc2c0ee",
    "hybrid-frida-hybrid-cs400-ov50-992cdc63",
)
# M=32 и ef_construction=200 у flat не используются, но проходят валидацию конфига.
BUILDS: tuple[dict[str, Any], ...] = (
    {"type": "flat", "M": 32, "ef_construction": 200, "quantization": "none"},
    {"type": "hnsw", "M": 16, "ef_construction": 64, "quantization": "none"},
    {"type": "hnsw", "M": 16, "ef_construction": 200, "quantization": "none"},
    {"type": "hnsw", "M": 32, "ef_construction": 200, "quantization": "none"},
    {"type": "hnsw", "M": 32, "ef_construction": 200, "quantization": "sq8"},
    {"type": "hnsw", "M": 32, "ef_construction": 200, "quantization": "fp16"},
)
METRIC_KEYS = ("recall@5", "recall@10", "precision@5", "precision@10", "mrr")
QUALITY_KEYS = ("mrr", "recall@10", "recall@5", "precision@10", "precision@5")

_TOKEN = re.compile(r"\w+", re.UNICODE)


def normalize_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def covers(quote: str, chunk: str, threshold: float = COVERAGE) -> bool:
    """Цитата покрыта целиком или непрерывным фрагментом не короче threshold.

    Фрагмент идёт подряд: так чанк остаётся релевантным, когда граница разрезала цитату.
    """
    quote_norm = normalize_text(quote)
    chunk_norm = normalize_text(chunk)
    if not quote_norm:
        return False
    if quote_norm in chunk_norm:
        return True
    quote_tokens = tokens(quote_norm)
    if not quote_tokens:
        return False
    need = math.ceil(threshold * len(quote_tokens) - 1e-12)
    if need <= 0:
        return False
    haystack = " ".join(tokens(chunk_norm))
    last = len(quote_tokens) - need + 1
    for start in range(last):
        window = " ".join(quote_tokens[start:start + need])
        if window in haystack:
            return True
    return False


def evidence_hits(chunks: list[str], evidence: list[dict[str, Any]]) -> list[set[int]]:
    return [
        {i for i, text in enumerate(chunks) if covers(item["quote"], text)}
        for item in evidence
    ]


def ranking_metrics(
    ranking: list[int],
    hits: list[set[int]],
    ks: tuple[int, ...] = KS,
) -> dict[str, float]:
    """Recall — доля пассажей, Precision — доля чанков top-k, MRR — по всему списку."""
    flags = [any(idx in passage for passage in hits) for idx in ranking]
    n_passages = len(hits)
    metrics: dict[str, float] = {}
    for k in ks:
        if k <= 0:
            raise ValueError("k должен быть > 0")
        top = set(ranking[:k])
        found = sum(1 for passage in hits if passage & top)
        metrics[f"recall@{k}"] = found / n_passages if n_passages else 0.0
        metrics[f"precision@{k}"] = sum(flags[:k]) / k
    first = next((rank for rank, flag in enumerate(flags, start=1) if flag), None)
    metrics["mrr"] = 0.0 if first is None else 1.0 / first
    return metrics


def macro_average(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        raise ValueError("Нет строк для усреднения")
    return {key: sum(row[key] for row in rows) / len(rows) for key in METRIC_KEYS}


def rrf_fuse(rankings: list[list[int]], k: int = RRF_K) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda idx: (-scores[idx], idx))


def top_overlap(rankings: list[list[int]], reference: list[list[int]], k: int = 10) -> float:
    if k <= 0:
        raise ValueError("k должен быть > 0")
    if len(rankings) != len(reference) or not rankings:
        raise ValueError("Списки ранжирований разной длины")
    total = 0.0
    for got, exact in zip(rankings, reference):
        total += len(set(got[:k]) & set(exact[:k])) / k
    return total / len(rankings)


def dense_depth(index_type: str, ef_search: int | None, ntotal: int) -> int:
    """Глубина поиска FAISS.

    HNSW поднимает efSearch до k. Запрос всех чанков приравнял бы efSearch к размеру
    корпуса и стёр различия 16/64/128, поэтому глубина равна ef_search и не меньше 10.
    """
    if index_type == "flat" or ef_search is None:
        return ntotal
    return min(ntotal, max(KS[-1], ef_search))


def gold_errors(
    questions: list[dict[str, Any]],
    diploma: str,
    chunk_sets: dict[str, list[str]],
) -> list[str]:
    diploma_norm = normalize_text(diploma)
    errors: list[str] = []
    for question in questions:
        for item in question["evidence"]:
            quote = item["quote"]
            if normalize_text(quote) not in diploma_norm:
                errors.append(f"{question['id']}: цитаты нет в diploma.md: {quote[:80]}")
                continue
            for run_id, chunks in chunk_sets.items():
                if not any(covers(quote, text) for text in chunks):
                    errors.append(f"{question['id']}: цитату не покрывает {run_id}: {quote[:80]}")
    return errors


def load_questions(path: Path) -> tuple[list[dict[str, Any]], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise SystemExit(f"В {path} нет вопросов")
    for question in questions:
        if not question.get("id") or not question.get("question") or not question.get("evidence"):
            raise SystemExit(f"Неполный вопрос: {question.get('id')}")
        for item in question["evidence"]:
            if not item.get("quote"):
                raise SystemExit(f"{question['id']}: пустая цитата")
    source = payload.get("source", "src/preprocessing/output/diploma.md")
    return questions, source


def load_chunk_texts(path: Path) -> list[str]:
    texts = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                texts.append(json.loads(line)["text"])
    if not texts:
        raise SystemExit(f"Пустой файл чанков: {path}")
    return texts


def load_bm25(path: Path) -> Any:
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    return payload["bm25"]


def index_overrides(build: dict[str, Any], ef_search: int) -> list[str]:
    return [
        f"index.type={build['type']}",
        f"index.quantization={build['quantization']}",
        f"index.hnsw.M={build['M']}",
        f"index.hnsw.ef_construction={build['ef_construction']}",
        f"index.hnsw.ef_search={ef_search}",
    ]


def dense_rankings(index: faiss.Index, queries: np.ndarray, k: int) -> list[list[int]]:
    _, ids = index.search(np.ascontiguousarray(queries, dtype=np.float32), k)
    rankings: list[list[int]] = []
    for row in ids:
        ranking: list[int] = []
        seen: set[int] = set()
        for idx in row.tolist():
            if idx < 0 or idx in seen:
                continue
            seen.add(idx)
            ranking.append(idx)
        rankings.append(ranking)
    return rankings


def bm25_rankings(bm25: Any, questions: list[str], n_chunks: int) -> list[list[int]]:
    rankings = []
    for text in questions:
        scores = np.asarray(bm25.get_scores(bm25_tokenize(text)), dtype=np.float64)
        if len(scores) != n_chunks:
            raise SystemExit(f"BM25 вернул {len(scores)} оценок на {n_chunks} чанков")
        rankings.append(np.argsort(-scores, kind="mergesort").tolist())
    return rankings


def fuse_rankings(
    retrieval: str,
    dense: list[list[int]],
    sparse: list[list[int]] | None,
) -> list[list[int]]:
    if retrieval == "dense":
        return dense
    if sparse is None:
        raise ValueError("Для hybrid нужен ранжир BM25")
    return [rrf_fuse([left, right]) for left, right in zip(dense, sparse)]


def round_metrics(metrics: dict[str, float]) -> dict[str, float]:
    return {key: round(metrics[key], 6) for key in METRIC_KEYS}


def _variant_title(row: dict[str, Any]) -> str:
    mode = "dense+BM25" if row["retrieval"] == "hybrid" else "dense"
    return f"{row['splitter']}, {mode}"


def _index_title(row: dict[str, Any]) -> str:
    if row["index_type"] == "flat":
        return "flat"
    quant = "" if row["quantization"] == "none" else f", {row['quantization']}"
    return f"HNSW M={row['M']}, efc={row['ef_construction']}, ef={row['ef_search']}{quant}"


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def _span_label(members: list[dict[str, Any]]) -> str:
    has_flat = any(row["index_type"] == "flat" for row in members)
    ef_values = sorted({row["ef_search"] for row in members if row["ef_search"] is not None})
    bits = []
    if has_flat:
        bits.append("flat")
    if ef_values:
        bits.append("HNSW ef_search=" + "/".join(str(value) for value in ef_values))
    return " и ".join(bits)


def _spread(rows: list[dict[str, Any]], key: str) -> tuple[float, float]:
    values = [row[key] for row in rows]
    return min(values), max(values)


def _same_quality(rows: list[dict[str, Any]]) -> bool:
    first = rows[0]
    return all(
        all(abs(row[key] - first[key]) < 1e-9 for key in METRIC_KEYS)
        for row in rows
    )


def render_report(
    rows: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    query_embed_seconds: float,
) -> str:
    lines = [
        "# Пункт 5. Метрики retrieval",
        "",
        f"Эталон: {len(questions)} вопросов с дословными цитатами из `src/preprocessing/output/diploma.md`. "
        "Чанк релевантен пассажу, если нормализованная цитата входит в чанк или в нём есть непрерывный "
        f"фрагмент не короче {COVERAGE:.0%} токенов цитаты. "
        "Recall@k — доля таких пассажей, которые задеты top-k. "
        "Precision@k — доля чанков top-k, которые задевают хотя бы один пассаж. "
        "MRR = 1 / ранг первого релевантного чанка.",
        "",
        "Сравниваются два готовых прогона. "
        "`markdown_header, dense` ищет только FAISS. "
        "`hybrid, dense+BM25` сливает FAISS и BM25 через RRF "
        f"(1/({RRF_K}+rank)). В плотное плечо HNSW попадают соседи глубины ef_search "
        "(не меньше 10): полный обход корпуса поднял бы efSearch до числа чанков. "
        "У flat в RRF входит полное точное ранжирование, BM25 всегда ранжирует все чанки.",
        "",
        f"Векторы вопросов посчитаны один раз за {query_embed_seconds:.1f} с. "
        "Колонка «мс» — время батча из всех вопросов, делённое на число вопросов, без эмбеддинга.",
        "",
        "| Вариант | Индекс | Recall@5 | Recall@10 | Precision@5 | Precision@10 | MRR | Совпадение с flat@10 | КБ | мс |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {variant} | {index} | {r5} | {r10} | {p5} | {p10} | {mrr} | {flat} | {kb} | {ms} |".format(
                variant=_variant_title(row),
                index=_index_title(row),
                r5=_fmt(row["recall@5"]),
                r10=_fmt(row["recall@10"]),
                p5=_fmt(row["precision@5"]),
                p10=_fmt(row["precision@10"]),
                mrr=_fmt(row["mrr"]),
                flat=_fmt(row["flat_recall@10"]),
                kb=f"{row['index_bytes'] / 1024:.0f}",
                ms=f"{row['search_ms']:.2f}",
            )
        )
    lines.extend(["", "## Выводы", ""])
    lines.append("\n\n".join(conclusion_lines(rows, questions)))
    lines.append("")
    return "\n".join(lines)


def _quality_key(row: dict[str, Any]) -> tuple[float, ...]:
    return tuple(row[key] for key in QUALITY_KEYS)


def conclusion_lines(rows: list[dict[str, Any]], questions: list[dict[str, Any]]) -> list[str]:
    by_id = {question["id"]: question["question"] for question in questions}
    paragraphs: list[str] = []
    flat_rows = [row for row in rows if row["index_type"] == "flat"]
    flat_bits = []
    for row in flat_rows:
        flat_bits.append(
            f"{_variant_title(row)} ({row['n_chunks']} чанков): "
            f"Recall@5={_fmt(row['recall@5'])}, Recall@10={_fmt(row['recall@10'])}, "
            f"Precision@5={_fmt(row['precision@5'])}, Precision@10={_fmt(row['precision@10'])}, "
            f"MRR={_fmt(row['mrr'])}"
        )
    paragraphs.append(
        "Точный flat-поиск отделяет чанкирование и способ слияния от параметров HNSW. "
        + ". ".join(flat_bits)
        + "."
    )
    paragraphs.append(
        "Precision ниже Recall: на вопрос приходится один или два эталонных пассажа, "
        "поэтому в top-10 большинство чанков лишние."
    )
    if len(flat_rows) == 2 and _same_quality(flat_rows):
        paragraphs.append("На flat метрики двух вариантов совпадают.")
    elif len(flat_rows) == 2:
        better = max(flat_rows, key=_quality_key)
        paragraphs.append(f"На flat выше вариант {_variant_title(better)}.")

    for run_id in RUN_IDS:
        group = [row for row in rows if row["run_id"] == run_id]
        if not group:
            continue
        title = _variant_title(group[0])
        if _same_quality(group):
            flat_lo, flat_hi = _spread(group, "flat_recall@10")
            kb_lo, kb_hi = _spread(group, "index_bytes")
            paragraphs.append(
                f"Для «{title}» Recall@5, Recall@10, Precision@5, Precision@10 и MRR совпадают "
                "у всех конфигураций этого варианта. "
                f"Совпадение top-10 с flat от {_fmt(flat_lo)} до {_fmt(flat_hi)}. "
                f"Размер индекса от {kb_lo / 1024:.0f} до {kb_hi / 1024:.0f} КБ."
            )
        else:
            clusters: dict[tuple[float, float], list[dict[str, Any]]] = {}
            for row in group:
                clusters.setdefault((row["mrr"], row["recall@10"]), []).append(row)
            parts = []
            for key in sorted(clusters, reverse=True):
                parts.append(
                    f"{_span_label(clusters[key])}: MRR={_fmt(key[0])}, Recall@10={_fmt(key[1])}"
                )
            paragraphs.append(f"Для «{title}» " + "; ".join(parts) + ".")

    best = max(rows, key=_quality_key)
    leaders = [row for row in rows if _quality_key(row) == _quality_key(best)]
    near = [
        row for row in rows
        if best["mrr"] - row["mrr"] <= 0.01 and best["recall@10"] - row["recall@10"] <= 0.01
    ]
    cheap = min(near, key=lambda row: (row["index_bytes"], row["search_ms"]))
    if _same_quality(rows):
        paragraphs.append(
            "Качество на всей сетке одно и то же. "
            f"Меньший индекс у «{_variant_title(cheap)}», {_index_title(cheap)}: "
            f"{cheap['index_bytes'] / 1024:.0f} КБ, поиск {cheap['search_ms']:.2f} мс, "
            f"MRR={_fmt(cheap['mrr'])}, Recall@10={_fmt(cheap['recall@10'])}."
        )
    elif len(leaders) > 1:
        smallest = min(leaders, key=lambda row: (row["index_bytes"], row["search_ms"]))
        paragraphs.append(
            f"Максимум качества совпадает у {len(leaders)} конфигураций: "
            f"MRR={_fmt(best['mrr'])}, Recall@10={_fmt(best['recall@10'])}. "
            f"Меньший индекс среди них у «{_variant_title(smallest)}», {_index_title(smallest)}: "
            f"{smallest['index_bytes'] / 1024:.0f} КБ."
        )
    else:
        paragraphs.append(
            f"Максимум качества у «{_variant_title(best)}», {_index_title(best)}: "
            f"MRR={_fmt(best['mrr'])}, Recall@10={_fmt(best['recall@10'])}, "
            f"индекс {best['index_bytes'] / 1024:.0f} КБ."
        )
        if cheap is not best:
            paragraphs.append(
                "В пределах 0.01 по MRR и Recall@10 меньший индекс у "
                f"«{_variant_title(cheap)}», {_index_title(cheap)}: "
                f"{cheap['index_bytes'] / 1024:.0f} КБ, "
                f"MRR={_fmt(cheap['mrr'])}, Recall@10={_fmt(cheap['recall@10'])}."
            )

    missed = []
    for question in questions:
        bits = []
        for row in flat_rows:
            per = next(item for item in row["per_question"] if item["id"] == question["id"])
            if per["recall@10"] < 1 or per["mrr"] == 0:
                bits.append(
                    f"{_variant_title(row)} Recall@10={_fmt(per['recall@10'])}, MRR={_fmt(per['mrr'])}"
                )
        if bits:
            missed.append(f"- {question['id']} {by_id[question['id']]}: " + "; ".join(bits))
    if missed:
        paragraphs.append("Вопросы с Recall@10 ниже 1 или MRR = 0 на flat:\n" + "\n".join(missed))
    else:
        paragraphs.append("На обоих flat-индексах у всех вопросов Recall@10 = 1.")
    return paragraphs


def snapshot_active(store_dir: Path) -> str | None:
    path = store_dir / ACTIVE_FILE
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def restore_active(store_dir: Path, previous: str | None) -> None:
    path = store_dir / ACTIVE_FILE
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(previous, encoding="utf-8")


def build_indexes(config_path: str | None) -> list[dict[str, Any]]:
    specs = []
    for run_id in RUN_IDS:
        for build in BUILDS:
            config = load_config(config_path, index_overrides(build, EF_SEARCH[1]))
            path = build_collection(config, run_id)
            specs.append({"run_id": run_id, "build": build, "path": path, "config": config})
    return specs


def embed_questions(config: Config, questions: list[dict[str, Any]]) -> tuple[np.ndarray, float]:
    from index.embedders import PrefixedEmbeddings

    print(f"Считаю эмбеддинги {len(questions)} вопросов, модель {config.embedding.model}")
    started = time.perf_counter()
    embedder = PrefixedEmbeddings(config.embedding)
    vectors = embedder.embed_queries([question["question"] for question in questions])
    elapsed = time.perf_counter() - started
    print(f"Запросы: {vectors.shape}, device={embedder.device}, {elapsed:.1f} с")
    return np.ascontiguousarray(vectors, dtype=np.float32), elapsed


def run_manifest(config: Config, run_id: str) -> dict[str, Any]:
    path = config.artifacts_dir / run_id / "manifest.json"
    if not path.is_file():
        raise SystemExit(f"Нет манифеста прогона: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_specs(
    config: Config,
    questions: list[dict[str, Any]],
    queries: np.ndarray,
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prepared: dict[str, dict[str, Any]] = {}
    for run_id in RUN_IDS:
        manifest = run_manifest(config, run_id)
        vectorization = manifest["config"]["embedding"]["vectorization"]
        splitter = manifest["config"]["splitter"]["type"]
        retrieval = "hybrid" if vectorization == "hybrid" else "dense"
        if vectorization not in {"dense", "hybrid"}:
            raise SystemExit(f"{run_id}: vectorization={vectorization}, для FAISS нужен dense или hybrid")
        chunks = load_chunk_texts(config.artifacts_dir / run_id / "chunks.jsonl")
        sparse = None
        if retrieval == "hybrid":
            bm25_path = config.artifacts_dir / run_id / "bm25.pkl"
            if not bm25_path.is_file():
                raise SystemExit(f"Нет {bm25_path}")
            sparse = bm25_rankings(
                load_bm25(bm25_path),
                [question["question"] for question in questions],
                len(chunks),
            )
        prepared[run_id] = {
            "splitter": splitter,
            "retrieval": retrieval,
            "chunks": chunks,
            "hits": [evidence_hits(chunks, question["evidence"]) for question in questions],
            "sparse": sparse,
        }

    rows: list[dict[str, Any]] = []
    for run_id in RUN_IDS:
        info = prepared[run_id]
        run_specs = [spec for spec in specs if spec["run_id"] == run_id]
        run_specs.sort(key=lambda spec: spec["build"]["type"] != "flat")
        flat_rankings: list[list[int]] | None = None
        for spec in run_specs:
            build = spec["build"]
            index = faiss.read_index(str(spec["path"] / INDEX_FILE))
            ef_values: tuple[int | None, ...] = (None,) if build["type"] == "flat" else EF_SEARCH
            for ef_search in ef_values:
                if ef_search is not None:
                    spec["config"].index.hnsw.ef_search = ef_search
                    set_ef_search(index, spec["config"])
                depth = dense_depth(build["type"], ef_search, len(info["chunks"]))
                dense_rankings(index, queries, depth)  # прогрев: первый вызов FAISS заметно медленнее
                started = time.perf_counter()
                dense = dense_rankings(index, queries, depth)
                rankings = fuse_rankings(info["retrieval"], dense, info["sparse"])
                search_ms = (time.perf_counter() - started) * 1000 / len(questions)
                if build["type"] == "flat":
                    flat_rankings = rankings
                if flat_rankings is None:
                    raise SystemExit(f"Для {run_id} нет flat-ранжирования")
                per_question = []
                for question, hits, ranking in zip(questions, info["hits"], rankings):
                    metrics = round_metrics(ranking_metrics(ranking, hits))
                    per_question.append({"id": question["id"], **metrics})
                summary = round_metrics(macro_average(per_question))
                rows.append({
                    "run_id": run_id,
                    "splitter": info["splitter"],
                    "retrieval": info["retrieval"],
                    "n_chunks": len(info["chunks"]),
                    "index_type": build["type"],
                    "M": build["M"],
                    "ef_construction": build["ef_construction"],
                    "ef_search": ef_search,
                    "quantization": build["quantization"],
                    "index_bytes": (spec["path"] / INDEX_FILE).stat().st_size,
                    "search_ms": round(search_ms, 3),
                    "flat_recall@10": round(top_overlap(rankings, flat_rankings), 6),
                    **summary,
                    "per_question": per_question,
                })
                print(
                    f"{info['splitter']}/{info['retrieval']} {_index_title(rows[-1])}: "
                    f"R@10={summary['recall@10']:.3f} MRR={summary['mrr']:.3f}"
                )
    return rows


def evaluate(config_path: str | None = None, questions_path: str | None = None) -> None:
    config = load_config(config_path)
    questions_file = Path(questions_path) if questions_path else config.root / "eval" / "questions.json"
    questions, source = load_questions(questions_file)
    diploma_path = config.root / source
    diploma = diploma_path.read_text(encoding="utf-8")
    chunk_sets = {
        run_id: load_chunk_texts(config.artifacts_dir / run_id / "chunks.jsonl")
        for run_id in RUN_IDS
    }
    errors = gold_errors(questions, diploma, chunk_sets)
    if errors:
        raise SystemExit("Эталон не покрыт чанками:\n" + "\n".join(errors))
    print(f"Эталон: {len(questions)} вопросов, цитаты покрыты обоими прогонами")

    previous = snapshot_active(config.faiss_dir)
    try:
        specs = build_indexes(config_path)
    finally:
        restore_active(config.faiss_dir, previous)
        print("active.json восстановлен")

    queries, embed_seconds = embed_questions(config, questions)
    rows = evaluate_specs(config, questions, queries, specs)
    out_dir = config.root / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "questions": questions_file.name,
        "n_questions": len(questions),
        "k": list(KS),
        "coverage_threshold": COVERAGE,
        "rrf_k": RRF_K,
        "query_embed_seconds": round(embed_seconds, 3),
        "rows": rows,
    }
    metrics_path = out_dir / "metrics.json"
    report_path = out_dir / "report.md"
    metrics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(
        render_report(rows, questions, embed_seconds),
        encoding="utf-8",
    )
    print(f"Метрики: {metrics_path}")
    print(f"Отчёт: {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Метрики retrieval (п.5)")
    parser.add_argument("--config", default=None, help="путь к config.yaml")
    parser.add_argument("--questions", default=None, help="путь к eval/questions.json")
    args = parser.parse_args()
    evaluate(args.config, args.questions)


if __name__ == "__main__":
    main()
