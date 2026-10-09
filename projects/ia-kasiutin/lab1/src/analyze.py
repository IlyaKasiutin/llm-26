import json
import re
from collections import defaultdict
from itertools import combinations

from config import RUNS_PATH, SUMMARY_PATH
from prompts import PROMPTS

PROMPTS_BY_ID = {prompt["id"]: prompt for prompt in PROMPTS}


def load_runs(path) -> list[dict]:
    if not path.exists():
        raise SystemExit(
            f"Нет файла {path}. Сначала запустите src/run_experiments.py."
        )
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if not rows:
        raise SystemExit(f"{path} пуст. Сначала выполните прогоны.")
    return rows


def extract_json(text: str):
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    start_positions = [pos for pos in (candidate.find("["), candidate.find("{")) if pos >= 0]
    if not start_positions:
        return None
    blob = candidate[min(start_positions):]
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(blob)
    except json.JSONDecodeError:
        return None
    return value


def normalize(value) -> str:
    text = str(value).lower().replace("ё", "е")
    return re.sub(r"\s+", "", text)


def score_p1(text: str, expect: dict) -> dict:
    missing = [item for item in expect["contains"] if item not in text]
    percents = [float(item.replace(",", ".")) for item in re.findall(r"(\d+(?:[.,]\d+)?)\s*%", text)]
    extra_percent = any(percent != 10 for percent in percents)
    codes = set(re.findall(r"\b[A-Z][A-Z0-9]{3,}\b", text))
    extra_codes = sorted(codes - {"SORRY10"})
    return {
        "ok": not missing and not extra_percent and not extra_codes,
        "missing": missing,
        "extra_percent": extra_percent,
        "extra_codes": extra_codes,
        "detail": _p1_detail(missing, extra_percent, extra_codes),
    }


def _p1_detail(missing, extra_percent, extra_codes) -> str:
    parts = []
    if missing:
        parts.append("нет: " + ", ".join(missing))
    if extra_percent:
        parts.append("другая скидка в процентах")
    if extra_codes:
        parts.append("другие коды: " + ", ".join(extra_codes))
    return "; ".join(parts) if parts else "факты на месте"


def score_p2(text: str, expect: dict) -> dict:
    parsed = extract_json(text)
    labels = parsed if isinstance(parsed, list) else None
    expected = expect["labels"]
    ok = labels == expected
    return {
        "ok": ok,
        "parsed": labels,
        "detail": "совпало" if ok else f"получено {labels!r}, ожидалось {expected!r}",
    }


def score_p3(text: str, expect: dict) -> dict:
    parsed = extract_json(text)
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "matched": 0,
            "total": len(expect["fields"]),
            "has_summary": False,
            "detail": "JSON-объект не найден",
        }
    matched = []
    missed = []
    for key, expected in expect["fields"].items():
        actual = parsed.get(key, "")
        if normalize(expected) in normalize(actual):
            matched.append(key)
        else:
            missed.append(key)
    summary = parsed.get("summary")
    has_summary = isinstance(summary, str) and summary.strip() != ""
    total = len(expect["fields"])
    return {
        "ok": len(matched) == total and has_summary,
        "matched": len(matched),
        "total": total,
        "has_summary": has_summary,
        "detail": (
            f"поля {len(matched)}/{total}"
            + ("" if not missed else ", нет: " + ", ".join(missed))
            + ("" if has_summary else "; нет summary")
        ),
    }


def score_row(row: dict) -> dict:
    prompt = PROMPTS_BY_ID.get(row["prompt_id"])
    if prompt is None:
        return {"ok": False, "detail": "неизвестный промпт"}
    text = row.get("response_text") or ""
    if prompt["id"] == "P1":
        return score_p1(text, prompt["expect"])
    if prompt["id"] == "P2":
        return score_p2(text, prompt["expect"])
    if prompt["id"] == "P3":
        return score_p3(text, prompt["expect"])
    return {"ok": False, "detail": "нет проверки"}


def fmt_num(value, digits: int) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def stability(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["prompt_id"], row["mode"])].append(row["response_text"])
    report = []
    for key in sorted(groups):
        texts = groups[key]
        pair_count = 0
        match_count = 0
        for left, right in combinations(texts, 2):
            pair_count += 1
            if left == right:
                match_count += 1
        report.append(
            {
                "model": key[0],
                "prompt_id": key[1],
                "mode": key[2],
                "repeats": len(texts),
                "pairs": pair_count,
                "matches": match_count,
            }
        )
    return report


def render(rows: list[dict]) -> str:
    scored = [(row, score_row(row)) for row in rows]
    lines = ["# Сводка прогонов", ""]
    lines.append(
        "| Модель | Промпт | Режим | Повтор | Ввод | Вывод | Секунды | Ток/с | Символы | finish | Проверка |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row, score in scored:
        usage = row.get("usage") or {}
        lines.append(
            "| {model} | {prompt_id} | {mode} | {repeat} | {prompt_tokens} | {completion_tokens} | {latency} | {tps} | {chars} | {finish} | {detail} |".format(
                model=row["model"],
                prompt_id=row["prompt_id"],
                mode=row["mode"],
                repeat=row["repeat"],
                prompt_tokens=usage.get("prompt_tokens", "—"),
                completion_tokens=usage.get("completion_tokens", "—"),
                latency=fmt_num(row.get("latency_s"), 2),
                tps=fmt_num(row.get("tokens_per_second"), 1),
                chars=len(row.get("response_text") or ""),
                finish=row.get("finish_reason") or "—",
                detail=score["detail"].replace("|", "/"),
            )
        )

    lines.extend(["", "## Точность по задачам", ""])
    by_task = defaultdict(list)
    for row, score in scored:
        by_task[(row["model"], row["prompt_id"], row["mode"])].append(score)

    lines.append("| Модель | Промпт | Режим | Успешных прогонов |")
    lines.append("| --- | --- | --- | --- |")
    for key in sorted(by_task):
        scores = by_task[key]
        ok_count = sum(1 for item in scores if item["ok"])
        lines.append(
            f"| {key[0]} | {key[1]} | {key[2]} | {ok_count}/{len(scores)} |"
        )

    lines.extend(["", "## Стабильность повторов", ""])
    lines.append("Доля пар повторов с дословно одинаковым текстом внутри одного режима.")
    lines.append("")
    lines.append("| Модель | Промпт | Режим | Совпавшие пары |")
    lines.append("| --- | --- | --- | --- |")
    for item in stability(rows):
        lines.append(
            f"| {item['model']} | {item['prompt_id']} | {item['mode']} | {item['matches']}/{item['pairs']} |"
        )

    lines.extend(["", "## Ответы", ""])
    for row in rows:
        lines.append(
            f"### {row['model']} / {row['prompt_id']} / {row['mode']} / #{row['repeat']}"
        )
        lines.append("")
        lines.append(row.get("response_text") or "")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    rows = load_runs(RUNS_PATH)
    summary = render(rows)
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(summary, encoding="utf-8")
    print(summary)
    print(f"Сохранено в {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
