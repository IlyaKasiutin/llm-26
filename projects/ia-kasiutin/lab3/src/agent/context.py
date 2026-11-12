"""Сжатие истории диалога: сводка старой части и хвост последних сообщений."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import tiktoken

from src.config import AgentConfig, PromptsConfig
from src.tracing import NullTracer

# o200k_base — токенайзер gpt-4o-mini. Для другой модели OpenRouter это общая оценка.
_ENCODER = tiktoken.get_encoding("o200k_base")
_MESSAGE_OVERHEAD = 4
SUMMARY_PREFIX = "Сводка предыдущего диалога:\n"
TRUNCATION_MARK = "\n…сокращено"


class _Llm(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> Any: ...


@dataclass(frozen=True)
class ContextView:
    """Вид истории, который уходит в модель, и оценка размера до и после сжатия."""

    messages: list[dict[str, Any]]
    tokens_before: int
    tokens_after: int
    summary: str
    dropped: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Block:
    start: int
    messages: list[dict[str, Any]]


def count_text(text: str) -> int:
    """Считает токены строки кодировкой o200k_base."""
    if not text:
        return 0
    return len(_ENCODER.encode(text))


def count_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Оценивает запрос: текст сообщений, вызовы инструментов и JSON их схем."""
    total = 0
    for message in messages:
        total += _MESSAGE_OVERHEAD
        content = message.get("content")
        if isinstance(content, str):
            total += count_text(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += count_text(json.dumps(tool_calls, ensure_ascii=False))
    if tools:
        total += count_text(json.dumps(tools, ensure_ascii=False))
    return total


def _split_blocks(history: list[dict[str, Any]], pinned: int) -> list[_Block]:
    """Режет историю на блоки. Вызов инструмента и его ответы остаются в одном блоке."""
    blocks: list[_Block] = []
    index = pinned
    while index < len(history):
        message = history[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            start = index
            group = [message]
            expected = {
                call.get("id")
                for call in message["tool_calls"]
                if isinstance(call, dict) and call.get("id")
            }
            index += 1
            while index < len(history) and history[index].get("role") == "tool":
                tool_id = history[index].get("tool_call_id")
                if expected and tool_id not in expected:
                    break
                group.append(history[index])
                index += 1
            blocks.append(_Block(start, group))
            continue
        if message.get("role") == "tool" and blocks:
            blocks[-1].messages.append(message)
            index += 1
            continue
        blocks.append(_Block(index, [message]))
        index += 1
    return blocks


def _format_message(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown")
    name = message.get("name")
    if isinstance(name, str) and name:
        role = f"{role} {name}"
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    calls = message.get("tool_calls") or []
    rendered: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        rendered.append(f"{function.get('name')}({function.get('arguments')})")
    suffix = f" вызовы: {'; '.join(rendered)}" if rendered else ""
    return f"{role}: {text}{suffix}"


def _summary_payload(previous: str, dropped: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    if previous:
        parts.append("Текущая сводка:\n" + previous)
    body = "\n".join(_format_message(message) for message in dropped)
    parts.append("Новые сообщения:\n" + body)
    return "\n\n".join(parts)


def _shrink_longest_text(messages: list[dict[str, Any]]) -> bool:
    """Укорачивает самый длинный текст в копии сообщений. Исходную историю не меняет."""
    longest_at: int | None = None
    longest = 0
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if content.endswith(TRUNCATION_MARK):
            content = content[: -len(TRUNCATION_MARK)]
        if len(content) > longest:
            longest = len(content)
            longest_at = index
    if longest_at is None or longest <= 40:
        return False
    content = messages[longest_at]["content"]
    if isinstance(content, str) and content.endswith(TRUNCATION_MARK):
        content = content[: -len(TRUNCATION_MARK)]
    kept = content[: max(40, len(content) // 2)].rstrip()
    messages[longest_at] = {**messages[longest_at], "content": kept + TRUNCATION_MARK}
    return True


def _truncate_tool_contents(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    budget: int,
) -> list[dict[str, Any]]:
    """Обрезает самые длинные ответы инструментов, пока вид не войдёт в бюджет."""
    view = list(messages)
    for _ in range(24):
        if count_messages(view, tools) <= budget:
            return view
        longest_at: int | None = None
        longest = 0
        for index, message in enumerate(view):
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            size = len(message["content"])
            if size > longest:
                longest = size
                longest_at = index
        if longest_at is None or longest <= len(TRUNCATION_MARK) + 20:
            return view
        content = view[longest_at]["content"]
        if content.endswith(TRUNCATION_MARK):
            content = content[: -len(TRUNCATION_MARK)]
        kept = content[: max(20, len(content) // 2)].rstrip()
        view[longest_at] = {**view[longest_at], "content": kept + TRUNCATION_MARK}
    return view


def _preview(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "unknown")
    content = message.get("content")
    text = content.replace("\n", " ").strip() if isinstance(content, str) else ""
    if not text and message.get("tool_calls"):
        names: list[str] = []
        for call in message["tool_calls"]:
            if isinstance(call, dict):
                function = call.get("function") or {}
                names.append(str(function.get("name") or ""))
        text = ", ".join(name for name in names if name)
    return f"{role}: {text[:80]}"


def _dropped_previews(
    history: list[dict[str, Any]],
    view_messages: list[dict[str, Any]],
) -> tuple[str, ...]:
    kept = {id(message) for message in view_messages}
    previews: list[str] = []
    for message in history:
        if id(message) in kept:
            continue
        previews.append(_preview(message))
        if len(previews) == 20:
            break
    return tuple(previews)


def _model_name(llm: Any) -> str | None:
    config = getattr(llm, "config", None)
    resolve = getattr(config, "resolve_model", None)
    if resolve is None:
        return None
    model = resolve()
    return model if isinstance(model, str) else None


def _model_parameters(llm: Any) -> dict[str, Any] | None:
    config = getattr(llm, "config", None)
    if config is None:
        return None
    parameters: dict[str, Any] = {}
    temperature = getattr(config, "temperature", None)
    max_tokens = getattr(config, "max_tokens", None)
    if temperature is not None:
        parameters["temperature"] = float(temperature)
    if max_tokens is not None:
        parameters["max_tokens"] = int(max_tokens)
    return parameters or None


class ContextManager:
    """Готовит запрос модели: полная история остаётся у агента, в модель уходит сжатый вид."""

    def __init__(
        self,
        config: AgentConfig,
        prompts: PromptsConfig,
        llm: _Llm,
        tracer: Any | None = None,
    ) -> None:
        self.budget = max(1, config.max_context_tokens - config.reserve_output_tokens)
        self.prompts = prompts
        self.llm = llm
        self.tracer = tracer if tracer is not None else NullTracer()
        self.summary = ""
        self.summarized_until = 0
        self.last_view: ContextView | None = None
        self._calls = 0

    async def prepare(
        self,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextView:
        """Собирает вид для модели. Список `history` не изменяется."""
        self._calls = 0
        tokens_before = count_messages(history, tools)
        if tokens_before <= self.budget:
            view = ContextView(list(history), tokens_before, tokens_before, self.summary, ())
            self.last_view = view
            return view

        with self.tracer.span("compress-history", input={"tokens_before": tokens_before}) as compression:
            pinned = 1 if history and history[0].get("role") == "system" else 0
            if self.summarized_until < pinned:
                self.summarized_until = pinned
            blocks = _split_blocks(history, pinned)
            start = self._select_start(history, blocks, self.summary, tools)
            if self.summary:
                start = max(start, self.summarized_until)
            dropped = history[self.summarized_until : start]
            if dropped:
                updated = await self._summarize(self.summary, dropped)
                if updated:
                    self.summary = updated
                    self.summarized_until = start

            view_messages = self._render(history, start)
            if count_messages(view_messages, tools) > self.budget and self._calls < 2:
                later = self._select_start(history, blocks, self.summary, tools)
                if self.summary:
                    later = max(later, self.summarized_until)
                extra = history[self.summarized_until : later]
                if extra:
                    updated = await self._summarize(self.summary, extra)
                    if updated:
                        self.summary = updated
                        self.summarized_until = later
                        start = later
                        view_messages = self._render(history, start)
            if count_messages(view_messages, tools) > self.budget:
                view_messages = _truncate_tool_contents(view_messages, tools, self.budget)

            view = ContextView(
                view_messages,
                tokens_before,
                count_messages(view_messages, tools),
                self.summary,
                _dropped_previews(history, view_messages),
            )
            self.last_view = view
            compression.update(
                output={
                    "tokens_before": view.tokens_before,
                    "tokens_after": view.tokens_after,
                    "strategy": "rolling_summary",
                    "dropped": list(view.dropped),
                    "summary": self.summary,
                }
            )
            return view

    def _select_start(
        self,
        history: list[dict[str, Any]],
        blocks: list[_Block],
        summary: str,
        tools: list[dict[str, Any]] | None,
    ) -> int:
        if not blocks:
            return len(history)
        start = blocks[-1].start
        for block in reversed(blocks):
            view = self._render(history, block.start, summary)
            if count_messages(view, tools) <= self.budget:
                start = block.start
            else:
                break
        return start

    def _render(
        self,
        history: list[dict[str, Any]],
        start: int,
        summary: str | None = None,
    ) -> list[dict[str, Any]]:
        text = self.summary if summary is None else summary
        view: list[dict[str, Any]] = []
        if history and history[0].get("role") == "system":
            view.append(history[0])
            start = max(start, 1)
        if text:
            view.append({"role": "system", "content": SUMMARY_PREFIX + text})
        if start < len(history):
            view.extend(history[start:])
        return view

    async def _summarize(self, previous: str, dropped: list[dict[str, Any]]) -> str | None:
        if self._calls >= 2 or not dropped:
            return None
        self._calls += 1
        copied = [dict(message) for message in dropped]
        previous_text = previous
        payload = _summary_payload(previous_text, copied)
        request = [
            {"role": "system", "content": self.prompts.context_summary_prompt},
            {"role": "user", "content": payload},
        ]
        for _ in range(12):
            if count_messages(request, None) <= self.budget:
                break
            if _shrink_longest_text(copied):
                payload = _summary_payload(previous_text, copied)
                request[1]["content"] = payload
                continue
            if len(previous_text) <= 40:
                break
            previous_text = previous_text[: len(previous_text) // 2].rstrip()
            payload = _summary_payload(previous_text, copied)
            request[1]["content"] = payload
        try:
            with self.tracer.generation(
                name="summarize-context",
                input=request,
                model=_model_name(self.llm),
                model_parameters=_model_parameters(self.llm),
            ) as generation:
                response = await self.llm.chat(request, tools=None)
                content = getattr(response, "content", None)
                generation.update(
                    output={"role": "assistant", "content": content},
                    model=getattr(response, "model", None),
                    usage={
                        "input": getattr(response, "prompt_tokens", None),
                        "output": getattr(response, "completion_tokens", None),
                    },
                    cost=getattr(response, "cost", None),
                    metadata={"latency_s": getattr(response, "latency_s", None)},
                )
        except Exception:
            # Сводка не должна обрывать диалог: остаётся прежняя сводка и хвост.
            return None
        text = getattr(response, "content", "")
        if not isinstance(text, str):
            return None
        cleaned = text.strip()
        return cleaned or None


