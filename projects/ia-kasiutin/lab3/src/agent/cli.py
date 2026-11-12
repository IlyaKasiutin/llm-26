"""Многоходовый CLI исследовательского агента."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from src.agent.graph import ResearchAgent
from src.agent.llm import OpenRouterClient
from src.config import load_config, load_prompts
from src.mcp_arxiv.client import ArxivMcpClient
from src.tools.rag_adapter import load_adapter
from src.tracing import build_tracer

_PREVIEW = 280


def _paint(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _clip(text: str, limit: int = _PREVIEW) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _arguments_line(arguments: dict[str, Any]) -> str:
    if not arguments:
        return ""
    parts = [f"{key}={_clip(str(value), 120)}" for key, value in arguments.items()]
    return _clip(", ".join(parts), 360)


def _tool_result(output: dict[str, Any]) -> str:
    error = output.get("error")
    if isinstance(error, str) and error:
        return _clip(error)
    papers = output.get("papers")
    if isinstance(papers, list):
        titles = []
        for paper in papers[:3]:
            if isinstance(paper, dict):
                title = paper.get("title") or paper.get("arxiv_id") or ""
                if title:
                    titles.append(str(title))
        count = output.get("count", len(papers))
        suffix = f": {', '.join(titles)}" if titles else ""
        return f"статей: {count}{suffix}"
    chunks = output.get("chunks")
    if isinstance(chunks, list):
        bits = []
        for chunk in chunks[:3]:
            if isinstance(chunk, dict):
                bits.append(f"{chunk.get('paper_id')} {chunk.get('score')}")
        count = output.get("count", len(chunks))
        suffix = f": {', '.join(bits)}" if bits else ""
        return f"фрагментов: {count}{suffix}"
    if output.get("saved"):
        return f"заметка сохранена: {output.get('title') or output.get('path')}"
    return _clip(json.dumps(output, ensure_ascii=False))


def _block(label: str, body: str, color: str) -> None:
    print(_paint(label, color))
    text = body.strip() or "—"
    for line in text.splitlines():
        print(f"  {line}")
    print()


def _model_body(event: dict[str, Any]) -> str:
    lines: list[str] = []
    reasoning = str(event.get("reasoning") or "").strip()
    content = str(event.get("content") or "").strip()
    if reasoning:
        lines.append(_clip(reasoning))
    if content:
        lines.append(_clip(content))
    tools = event.get("tools") or []
    if tools:
        lines.append("инструменты: " + ", ".join(str(name) for name in tools))
    return "\n".join(lines)


def _print_turn(user_text: str | None, result: dict[str, Any]) -> None:
    if user_text:
        _block("Вы", user_text, "1;36")
    for event in result.get("events") or []:
        role = event.get("role")
        if role == "summary":
            label = (
                "модель · сжатие истории · "
                f"{event.get('tokens_before')} → {event.get('tokens_after')} токенов"
            )
            _block(label, _clip(str(event.get("text") or ""), 700), "33")
        elif role == "model":
            latency = event.get("latency_s")
            timing = f" · {latency} с" if isinstance(latency, (int, float)) and latency else ""
            _block(f"модель · шаг {event.get('step')}{timing}", _model_body(event), "33")
        elif role == "tool":
            output = event.get("output") if isinstance(event.get("output"), dict) else {}
            failed = "error" in output
            arguments = _arguments_line(event.get("arguments") or {})
            body = arguments
            result_line = _tool_result(output)
            body = f"{arguments}\n→ {result_line}" if arguments else f"→ {result_line}"
            color = "31" if failed else "35"
            _block(f"инструмент · {event.get('name')}", body, color)
    note = " · остановлено по лимиту шагов" if result.get("step_limit") else ""
    _block("Ассистент", result.get("answer") or "", "1;32")
    print(
        _paint(
            f"{result.get('model') or 'модель'} · шагов {result.get('steps')} · "
            f"инструментов {len(result.get('tool_calls') or [])}{note}",
            "2",
        )
    )


async def run_cli(question: str, config_path: str | None, overrides: list[str]) -> None:
    config = load_config(config_path, overrides)
    prompts = load_prompts()
    tracer, warning = build_tracer(config)
    if warning:
        print(warning)
    if tracer.enabled:
        print(f"Сессия Langfuse: {tracer.session_id}")
    llm = OpenRouterClient(config.llm)
    adapter = load_adapter(config)
    try:
        async with ArxivMcpClient() as mcp:
            agent = await ResearchAgent.create(config, prompts, llm, mcp, adapter, tracer=tracer)
            if question:
                _print_turn(question, await agent.ask(question))
                return
            print("Диалог с агентом. Пустая строка или exit завершает сессию.")
            prompt = _paint("Вы", "1;36") + ": "
            while True:
                try:
                    line = input(prompt).strip()
                except EOFError:
                    print()
                    return
                if not line or line.lower() in {"exit", "quit"}:
                    return
                _print_turn(None, await agent.ask(line))
                print()
    finally:
        await llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Диалоговый исследовательский агент")
    parser.add_argument("question", nargs="*", help="Один вопрос. Без аргумента открывается диалог.")
    parser.add_argument("--config", default=None, help="Путь к config.yaml")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Переопределение параметров config.yaml",
    )
    args = parser.parse_args()
    question = " ".join(args.question).strip()
    asyncio.run(run_cli(question, args.config, args.overrides))


if __name__ == "__main__":
    main()
