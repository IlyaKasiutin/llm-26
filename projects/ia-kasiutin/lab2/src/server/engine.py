"""RAG-движок: поиск по FAISS, сборка промпта, вызов vLLM, цитаты."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from index.config import Config

from .llm import LlmUnavailable, make_llm
from .prompts import NO_ANSWER, build_messages

_MARKER = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")


def parse_markers(answer: str, max_n: int) -> list[int]:
    """Номера фрагментов, на которые ссылается ответ, в порядке появления."""
    found: list[int] = []
    for match in _MARKER.finditer(answer):
        for part in re.split(r"[,;]", match.group(1)):
            n = int(part)
            if 1 <= n <= max_n and n not in found:
                found.append(n)
    return found


def _snippet(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _usage(message: Any) -> dict[str, int | None]:
    meta = getattr(message, "usage_metadata", None) or {}
    return {
        "prompt_tokens": meta.get("input_tokens"),
        "completion_tokens": meta.get("output_tokens"),
    }


class RagEngine:
    def __init__(
        self,
        vector_store,
        config: Config,
        llm=None,
        llm_factory: Callable[[], Any] | None = None,
        model_id: str | None = None,
        count_tokens: Callable[[str], int] | None = None,
    ) -> None:
        self.store = vector_store
        self.config = config
        self.llm = llm
        self.llm_factory = llm_factory
        self.model_id = model_id
        self.count_tokens = count_tokens or (lambda text: len(text.split()))

    # 1. поиск
    def retrieve(self, question: str, top_n: int | None = None) -> list[dict]:
        rag = self.config.rag
        hits = self.store.similarity_search_with_score(question, k=top_n or rag.top_n)
        blocks: list[dict] = []
        used = 0
        for doc, score in hits:
            score = float(score)
            if rag.min_score is not None and score < rag.min_score:
                continue
            tokens = self.count_tokens(doc.page_content)
            if blocks and used + tokens > rag.max_context_tokens:
                break  # бюджет контекста исчерпан; первый чанк берём всегда
            used += tokens
            blocks.append(
                {
                    "n": len(blocks) + 1,
                    "text": doc.page_content,
                    "source": doc.metadata.get("source"),
                    "page": doc.metadata.get("page"),
                    "section": doc.metadata.get("section"),
                    "chunk_id": doc.metadata.get("chunk_id"),
                    "score": round(score, 4),
                }
            )
        return blocks

    # 2-3. промпт без вызова LLM (для --show-prompt)
    def prepare(self, question: str, top_n: int | None = None) -> tuple[list[dict], list[dict]]:
        blocks = self.retrieve(question, top_n)
        return build_messages(question, blocks), blocks

    def _ensure_llm(self):
        if self.llm is None:
            if self.llm_factory is None:
                raise LlmUnavailable("LLM не настроен")
            self.llm = self.llm_factory()
        return self.llm

    def _citations(self, answer: str, blocks: list[dict]) -> list[dict]:
        limit = self.config.rag.snippet_chars
        cited = parse_markers(answer, len(blocks))
        chosen = cited or [b["n"] for b in blocks]
        return [
            {
                "n": block["n"],
                "source": block["source"],
                "page": block["page"],
                "section": block["section"],
                "score": block["score"],
                "snippet": _snippet(block["text"], limit),
                "cited": bool(cited),
            }
            for block in blocks
            if block["n"] in chosen
        ]

    def ask(self, question: str, top_n: int | None = None) -> dict:
        started = time.perf_counter()
        messages, blocks = self.prepare(question, top_n)
        retrieval_s = time.perf_counter() - started

        if not blocks:
            return {
                "answer": NO_ANSWER,
                "citations": [],
                "model": self.model_id,
                "usage": {"prompt_tokens": None, "completion_tokens": None},
                "latency_s": {"retrieval": round(retrieval_s, 3), "llm": 0.0},
            }

        llm = self._ensure_llm()
        llm_started = time.perf_counter()
        try:
            message = llm.invoke(messages)
        except Exception as error:
            raise LlmUnavailable(f"Ошибка запроса к vLLM: {error}") from error
        llm_s = time.perf_counter() - llm_started

        content = message.content
        answer = content if isinstance(content, str) else str(content)
        answer = answer.strip()
        return {
            "answer": answer,
            "citations": self._citations(answer, blocks),
            "model": self.model_id,
            "usage": _usage(message),
            "latency_s": {"retrieval": round(retrieval_s, 3), "llm": round(llm_s, 3)},
        }


def build_engine(config: Config, with_llm: bool = True) -> RagEngine:
    """Загружает FAISS и эмбеддер (локально); LLM подключается лениво."""
    from index.load_to_vector_store import load_vector_store
    from index.splitters import token_counter

    store = load_vector_store(config, collection=config.rag.collection)
    engine = RagEngine(
        store, config, count_tokens=token_counter(config.embedding.model)
    )
    if with_llm:
        from .llm import resolve_model

        def factory():
            model_id = resolve_model(config.llm)
            engine.model_id = model_id
            return make_llm(config.llm, model_id)

        engine.llm_factory = factory
        try:
            engine._ensure_llm()
        except LlmUnavailable as error:
            print(f"Предупреждение: {error}")
    return engine
