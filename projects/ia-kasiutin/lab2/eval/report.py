"""Форматирование Markdown-отчёта и автоматических выводов для п.5."""

from __future__ import annotations

from typing import Any

METRIC_KEYS = ("recall@5", "recall@10", "precision@5", "precision@10", "mrr")
QUALITY_KEYS = ("mrr", "recall@10", "recall@5", "precision@10", "precision@5")


def variant_title(row: dict[str, Any]) -> str:
    mode = "dense+BM25" if row["retrieval"] == "hybrid" else "dense"
    return f"{row['splitter']}, {mode}"


def index_title(row: dict[str, Any]) -> str:
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


def _quality_key(row: dict[str, Any]) -> tuple[float, ...]:
    return tuple(row[key] for key in QUALITY_KEYS)


def conclusion_lines(
    rows: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    run_ids: tuple[str, ...],
) -> list[str]:
    by_id = {question["id"]: question["question"] for question in questions}
    paragraphs: list[str] = []
    flat_rows = [row for row in rows if row["index_type"] == "flat"]
    flat_bits = [
        f"{variant_title(row)} ({row['n_chunks']} чанков): "
        f"Recall@5={_fmt(row['recall@5'])}, Recall@10={_fmt(row['recall@10'])}, "
        f"Precision@5={_fmt(row['precision@5'])}, Precision@10={_fmt(row['precision@10'])}, "
        f"MRR={_fmt(row['mrr'])}"
        for row in flat_rows
    ]
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
        paragraphs.append(f"На flat выше вариант {variant_title(better)}.")

    for run_id in run_ids:
        group = [row for row in rows if row["run_id"] == run_id]
        if not group:
            continue
        title = variant_title(group[0])
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
            parts = [
                f"{_span_label(clusters[key])}: MRR={_fmt(key[0])}, Recall@10={_fmt(key[1])}"
                for key in sorted(clusters, reverse=True)
            ]
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
            f"Меньший индекс у «{variant_title(cheap)}», {index_title(cheap)}: "
            f"{cheap['index_bytes'] / 1024:.0f} КБ, поиск {cheap['search_ms']:.2f} мс, "
            f"MRR={_fmt(cheap['mrr'])}, Recall@10={_fmt(cheap['recall@10'])}."
        )
    elif len(leaders) > 1:
        smallest = min(leaders, key=lambda row: (row["index_bytes"], row["search_ms"]))
        paragraphs.append(
            f"Максимум качества совпадает у {len(leaders)} конфигураций: "
            f"MRR={_fmt(best['mrr'])}, Recall@10={_fmt(best['recall@10'])}. "
            f"Меньший индекс среди них у «{variant_title(smallest)}», {index_title(smallest)}: "
            f"{smallest['index_bytes'] / 1024:.0f} КБ."
        )
    else:
        paragraphs.append(
            f"Максимум качества у «{variant_title(best)}», {index_title(best)}: "
            f"MRR={_fmt(best['mrr'])}, Recall@10={_fmt(best['recall@10'])}, "
            f"индекс {best['index_bytes'] / 1024:.0f} КБ."
        )
        if cheap is not best:
            paragraphs.append(
                "В пределах 0.01 по MRR и Recall@10 меньший индекс у "
                f"«{variant_title(cheap)}», {index_title(cheap)}: "
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
                    f"{variant_title(row)} Recall@10={_fmt(per['recall@10'])}, MRR={_fmt(per['mrr'])}"
                )
        if bits:
            missed.append(f"- {question['id']} {by_id[question['id']]}: " + "; ".join(bits))
    if missed:
        paragraphs.append("Вопросы с Recall@10 ниже 1 или MRR = 0 на flat:\n" + "\n".join(missed))
    else:
        paragraphs.append("На обоих flat-индексах у всех вопросов Recall@10 = 1.")
    return paragraphs


def render_report(
    rows: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    query_embed_seconds: float,
    coverage: float,
    rrf_k: int,
    run_ids: tuple[str, ...],
) -> str:
    lines = [
        "# Пункт 5. Метрики retrieval",
        "",
        f"Эталон: {len(questions)} вопросов с дословными цитатами из `src/preprocessing/output/diploma.md`. "
        "Чанк релевантен пассажу, если нормализованная цитата входит в чанк или в нём есть непрерывный "
        f"фрагмент не короче {coverage:.0%} токенов цитаты. "
        "Recall@k — доля таких пассажей, которые задеты top-k. "
        "Precision@k — доля чанков top-k, которые задевают хотя бы один пассаж. "
        "MRR = 1 / ранг первого релевантного чанка.",
        "",
        "Сравниваются два готовых прогона. "
        "`markdown_header, dense` ищет только FAISS. "
        "`hybrid, dense+BM25` сливает FAISS и BM25 через RRF "
        f"(1/({rrf_k}+rank)). В плотное плечо HNSW попадают соседи глубины ef_search "
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
                variant=variant_title(row),
                index=index_title(row),
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
    lines.append("\n\n".join(conclusion_lines(rows, questions, run_ids)))
    lines.append("")
    return "\n".join(lines)
