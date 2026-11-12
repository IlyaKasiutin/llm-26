"""Клиент к LLM через OpenRouter API (OpenAI-совместимый)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import AsyncOpenAI

from src.config import LlmConfig


@dataclass(frozen=True)
class ToolCallRequest:
    """Вызов инструмента, запрошенный моделью."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str
    parse_error: str | None = None


@dataclass(frozen=True)
class LlmResponse:
    """Результат одного обращения к Chat Completions API."""

    content: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    model: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: float | None = None
    latency_s: float = 0.0
    reasoning: str | None = None

    def to_assistant_message(self) -> dict[str, Any]:
        """Формирует сообщение ассистента для добавления в историю `messages`."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return message


def _message_reasoning(message: Any) -> str | None:
    """Достаёт рассуждение модели, если провайдер его вернул отдельным полем."""
    for key in ("reasoning", "reasoning_content"):
        value = getattr(message, key, None)
        if isinstance(value, str) and value.strip():
            return value
    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        for key in ("reasoning", "reasoning_content"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def parse_tool_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """Разбирает JSON-аргументы tool call. Ошибку возвращает текстом, не бросает её."""
    if not raw or not raw.strip():
        return {}, None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as error:
        return {}, f"Невалидный JSON аргументов инструмента: {error}"
    if not isinstance(loaded, dict):
        return {}, f"Аргументы инструмента должны быть JSON-объектом, получено: {type(loaded).__name__}"
    return loaded, None


class OpenRouterClient:
    """Асинхронный клиент к OpenRouter или любому OpenAI-совместимому серверу."""

    def __init__(
        self,
        config: LlmConfig,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.base_url = config.resolve_base_url()
        self.model = config.resolve_model()
        resolved_key = api_key if api_key is not None else config.resolve_api_key()
        self._client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=resolved_key,
            timeout=config.timeout_seconds,
            max_retries=2,
            http_client=http_client,
        )

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LlmResponse:
        """Выполняет запрос `chat.completions.create` с поддержкой `tools`."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice

        started = time.perf_counter()
        completion = await self._client.chat.completions.create(**kwargs)
        latency_s = round(time.perf_counter() - started, 3)

        choice = completion.choices[0]
        message = choice.message
        content = message.content or ""
        reasoning = _message_reasoning(message)

        parsed_calls: list[ToolCallRequest] = []
        for raw_call in message.tool_calls or []:
            function = raw_call.function
            raw_args = function.arguments or "{}"
            args_dict, parse_error = parse_tool_arguments(raw_args)
            parsed_calls.append(
                ToolCallRequest(
                    id=raw_call.id,
                    name=function.name,
                    arguments=args_dict,
                    raw_arguments=raw_args,
                    parse_error=parse_error,
                )
            )

        usage = completion.usage
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        cost = getattr(usage, "cost", None) if usage else None

        return LlmResponse(
            content=content,
            tool_calls=parsed_calls,
            model=completion.model or self.model,
            finish_reason=choice.finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=float(cost) if isinstance(cost, (int, float)) else None,
            latency_s=latency_s,
            reasoning=reasoning,
        )
