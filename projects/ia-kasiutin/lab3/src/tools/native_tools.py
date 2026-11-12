"""Локальные инструменты агента: поиск по RAG и сохранение заметок."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import PromptsConfig
from src.tools.rag_adapter import RagAdapter


def rag_search(adapter: RagAdapter, query: str, top_n: int | None = None) -> dict[str, Any]:
    """Ищет чанки в локальном dense-индексе. Ошибки возвращает в теле ответа."""
    if not query or not query.strip():
        return {"error": "Поисковый запрос query не может быть пустым", "chunks": [], "count": 0}
    try:
        chunks = adapter.search(query.strip(), top_n)
    except (OSError, ValueError, RuntimeError) as error:
        return {"error": str(error), "chunks": [], "count": 0}
    dumped = [chunk.model_dump() for chunk in chunks]
    return {"count": len(dumped), "chunks": dumped}


def save_research_note(
    path: Path,
    title: str,
    text: str,
    paper_ids: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Дописывает одну заметку в JSONL. Пустые title или text не записывает."""
    clean_title = title.strip()
    clean_text = text.strip()
    if not clean_title or not clean_text:
        return {"error": "Поля title и text не могут быть пустыми"}
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": clean_title,
        "text": clean_text,
        "paper_ids": list(paper_ids or []),
        "tags": list(tags or []),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"saved": True, "path": str(path), "title": clean_title}


def _function_tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def native_openai_tools(prompts: PromptsConfig) -> list[dict[str, Any]]:
    """Схемы rag_search и save_research_note для OpenAI function calling."""
    return [
        _function_tool(
            "rag_search",
            prompts.tool_description("rag_search"),
            {
                "query": {"type": "string", "description": "Текст поискового запроса"},
                "top_n": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Сколько чанков вернуть. По умолчанию значение из конфигурации.",
                },
            },
            ["query"],
        ),
        _function_tool(
            "save_research_note",
            prompts.tool_description("save_research_note"),
            {
                "title": {"type": "string", "description": "Заголовок заметки"},
                "text": {"type": "string", "description": "Текст выводов"},
                "paper_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Связанные идентификаторы публикаций или чанков",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Теги заметки",
                },
            },
            ["title", "text"],
        ),
    ]
