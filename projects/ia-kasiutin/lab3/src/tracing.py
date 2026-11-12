"""Трейсы диалога в Langfuse: сессия, реплика, модель, инструменты и сжатие контекста."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from src.config import Config

AGENT_NAME = "langgraph"
AGENT_VERSION = "v1"
ENVIRONMENT = "development"

_USAGE_RENAME = {"prompt_tokens": "input", "completion_tokens": "output"}
_USAGE_DROP = {"total", "total_tokens"}


class _NullHandle:
    def update(self, **kwargs: Any) -> None:
        return None


class NullTracer:
    """Ничего не отправляет. Нужен тестам и запуску без ключей Langfuse."""

    enabled = False

    def __init__(self, session_id: str | None = None, user_id: str = "local") -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.user_id = user_id

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        input: Any = None,
        tags: list[str] | None = None,
        as_type: str = "agent",
    ) -> Iterator[_NullHandle]:
        del name, input, tags, as_type
        yield _NullHandle()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        as_type: str = "span",
    ) -> Iterator[_NullHandle]:
        del name, input, metadata, as_type
        yield _NullHandle()

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        input: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
    ) -> Iterator[_NullHandle]:
        del name, input, model, model_parameters
        yield _NullHandle()

    def flush(self) -> None:
        return None


class _LangfuseHandle:
    def __init__(self, observation: Any, metadata: dict[str, Any] | None = None) -> None:
        self._observation = observation
        self._metadata = dict(metadata or {})

    def update(self, **kwargs: Any) -> None:
        payload: dict[str, Any] = {}
        if "output" in kwargs:
            payload["output"] = kwargs["output"]
        if "input" in kwargs:
            payload["input"] = kwargs["input"]
        if kwargs.get("level"):
            payload["level"] = kwargs["level"]
        if kwargs.get("model"):
            payload["model"] = kwargs["model"]
        metadata = kwargs.get("metadata")
        if metadata:
            self._metadata.update(metadata)
            payload["metadata"] = dict(self._metadata)
        details = _usage_details(kwargs.get("usage") or {})
        if details:
            payload["usage_details"] = details
        cost = kwargs.get("cost")
        if isinstance(cost, (int, float)):
            payload["cost_details"] = {"total": float(cost)}
        if payload:
            self._observation.update(**payload)


class LangfuseTracer:
    """Пишет наблюдения через SDK v5. Вложенность держит текущий контекст OpenTelemetry."""

    enabled = True

    def __init__(self, *, public_key: str, secret_key: str, base_url: str, user_id: str) -> None:
        from langfuse import Langfuse

        self.user_id = user_id
        self.session_id = str(uuid.uuid4())
        self._client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            environment=ENVIRONMENT,
        )

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        input: Any = None,
        tags: list[str] | None = None,
        as_type: str = "agent",
    ) -> Iterator[_LangfuseHandle]:
        from langfuse import propagate_attributes

        with propagate_attributes(
            user_id=self.user_id,
            session_id=self.session_id,
            tags=tags,
            trace_name=name,
            version=AGENT_VERSION,
            environment=ENVIRONMENT,
        ):
            with self._client.start_as_current_observation(
                as_type=as_type,
                name=name,
                input=input,
            ) as observation:
                yield _LangfuseHandle(observation)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        as_type: str = "span",
    ) -> Iterator[_LangfuseHandle]:
        with self._client.start_as_current_observation(
            as_type=as_type,
            name=name,
            input=input,
            metadata=metadata,
        ) as observation:
            yield _LangfuseHandle(observation, metadata)

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        input: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
    ) -> Iterator[_LangfuseHandle]:
        with self._client.start_as_current_observation(
            as_type="generation",
            name=name,
            input=input,
            model=model,
            model_parameters=model_parameters,
        ) as observation:
            yield _LangfuseHandle(observation)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            return None


def _usage_details(usage: dict[str, Any]) -> dict[str, int]:
    """Оставляет непересекающиеся корзины input/output. total Langfuse считает сам."""
    details: dict[str, int] = {}
    for key, value in usage.items():
        if not isinstance(value, int) or key in _USAGE_DROP:
            continue
        details[_USAGE_RENAME.get(key, key)] = value
    return details


def build_tracer(config: Config) -> tuple[NullTracer | LangfuseTracer, str | None]:
    """Возвращает трейсер и предупреждение, если ключи не заданы."""
    if not config.langfuse.enabled:
        return NullTracer(user_id=config.langfuse.user_id), None
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        return (
            NullTracer(user_id=config.langfuse.user_id),
            "Langfuse включён, но ключи не заданы. Трейсы не пишутся.",
        )
    base_url = (
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or config.langfuse.host
    )
    return (
        LangfuseTracer(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            user_id=config.langfuse.user_id,
        ),
        None,
    )
