"""Клиент к vLLM-серверу из лабы 1 (OpenAI-совместимый REST)."""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

from index.config import LlmConfig

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"


class LlmUnavailable(RuntimeError):
    """vLLM недоступен или не отдаёт ожидаемую модель."""


def resolve_base_url(config: LlmConfig) -> str:
    # Как в лабе 1: VLLM_BASE_URL важнее значения из конфига.
    return os.environ.get("VLLM_BASE_URL") or config.base_url or DEFAULT_BASE_URL


def resolve_api_key() -> str:
    return os.environ.get("VLLM_API_KEY", "EMPTY")


def list_model_ids(config: LlmConfig) -> list[str]:
    from openai import OpenAI

    base_url = resolve_base_url(config)
    client = OpenAI(base_url=base_url, api_key=resolve_api_key(), timeout=config.timeout_s, max_retries=0)
    try:
        return [model.id for model in client.models.list().data]
    except Exception as error:  # сеть, туннель, авторизация
        raise LlmUnavailable(
            f"vLLM недоступен по {base_url}: {error}. "
            "Проверьте SSH-туннель и переменную VLLM_BASE_URL."
        ) from error


def resolve_model(config: LlmConfig) -> str:
    available = list_model_ids(config)
    if not available:
        raise LlmUnavailable("vLLM не отдаёт ни одной модели (/v1/models пуст)")
    if config.model == "auto":
        return available[0]
    if config.model not in available:
        raise LlmUnavailable(f"Модель {config.model!r} не отдаётся сервером, доступны: {available}")
    return config.model


def extra_body_for(model_id: str, config: LlmConfig) -> dict:
    """Те же правила, что в лабе 1: шаблон Qwen3 без thinking и repetition_penalty через extra_body."""
    extra: dict = {"repetition_penalty": config.repetition_penalty}
    if "qwen3" in model_id.lower():
        extra["chat_template_kwargs"] = {"enable_thinking": False}
    return extra


def make_llm(config: LlmConfig, model_id: str | None = None) -> ChatOpenAI:
    model_id = model_id or resolve_model(config)
    return ChatOpenAI(
        model=model_id,
        base_url=resolve_base_url(config),
        api_key=resolve_api_key(),
        temperature=config.temperature,
        top_p=config.top_p,
        max_tokens=config.max_tokens,
        timeout=config.timeout_s,
        max_retries=1,
        extra_body=extra_body_for(model_id, config),
    )
