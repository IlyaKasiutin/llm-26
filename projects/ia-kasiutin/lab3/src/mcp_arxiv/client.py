"""MCP-клиент по транспорту stdio для подключения агентов к серверу arXiv."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.config import LAB3_ROOT


@dataclass(frozen=True)
class McpToolSpec:
    """Описание инструмента, полученное от MCP-сервера."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai_tool(self) -> dict[str, Any]:
        """Преобразует схему MCP-инструмента в формат OpenAI function calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ArxivMcpClient:
    """Асинхронный MCP-клиент, поднимающий `src.mcp_arxiv.server` по `stdio`."""

    def __init__(
        self,
        python_executable: str | None = None,
        server_module: str = "src.mcp_arxiv.server",
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.server_module = server_module
        self.cwd = cwd or LAB3_ROOT
        self.env = env
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> ArxivMcpClient:
        merged_env = {**os.environ, "PYTHONPATH": str(self.cwd)}
        if self.env:
            merged_env.update(self.env)

        params = StdioServerParameters(
            command=self.python_executable,
            args=["-m", self.server_module],
            env=merged_env,
            cwd=str(self.cwd),
        )
        stack = AsyncExitStack()
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        self._stack = stack
        self._session = session
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP-сессия не открыта: используйте 'async with ArxivMcpClient()'")
        return self._session

    async def list_tools(self) -> list[McpToolSpec]:
        """Возвращает список доступных инструментов от MCP-сервера."""
        session = self._require_session()
        response = await session.list_tools()
        return [
            McpToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema) if tool.inputSchema else {"type": "object", "properties": {}},
            )
            for tool in response.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Вызывает инструмент на MCP-сервере и возвращает распакованный словарь ответа."""
        session = self._require_session()
        result = await session.call_tool(name, arguments=arguments or {})

        texts: list[str] = []
        for block in result.content:
            text_val = getattr(block, "text", None)
            if isinstance(text_val, str):
                texts.append(text_val)

        combined = "\n".join(texts).strip()
        if not combined:
            payload: dict[str, Any] = {}
        else:
            try:
                parsed = json.loads(combined)
                payload = parsed if isinstance(parsed, dict) else {"result": parsed}
            except json.JSONDecodeError:
                payload = {"text": combined}

        if getattr(result, "isError", False) and "error" not in payload:
            payload["error"] = combined or f"Ошибка вызова MCP-инструмента {name!r}"

        return payload

    def make_async_caller(self, tool_name: str) -> Callable[..., Any]:
        """Возвращает корутину-обёртку для вызова конкретного MCP-инструмента по имени."""

        async def _invoke(**kwargs: Any) -> dict[str, Any]:
            cleaned = {k: v for k, v in kwargs.items() if v is not None}
            return await self.call_tool(tool_name, cleaned)

        _invoke.__name__ = tool_name
        return _invoke

    def make_sync_caller(self, tool_name: str) -> Callable[..., dict[str, Any]]:
        """Возвращает синхронную обёртку для фреймворков с синхронным вызовом (например, smolagents)."""

        def _invoke(**kwargs: Any) -> dict[str, Any]:
            cleaned = {k: v for k, v in kwargs.items() if v is not None}
            return asyncio.run(self._call_in_ephemeral_session(tool_name, cleaned))

        _invoke.__name__ = tool_name
        return _invoke

    async def _call_in_ephemeral_session(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if self._session is not None:
            return await self.call_tool(tool_name, arguments)
        async with ArxivMcpClient(
            python_executable=self.python_executable,
            server_module=self.server_module,
            cwd=self.cwd,
            env=self.env,
        ) as temp_client:
            return await temp_client.call_tool(tool_name, arguments)
