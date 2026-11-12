"""ToolCallingAgent на тех же инструментах и модели, что и LangGraph-агент.

Трейсы пишет ``SmolagentsInstrumentor`` (пакет ``openinference-instrumentation-smolagents``),
как описано в документации Langfuse. Второе ручное дерево не строится.
``ContextManager`` сюда не переносится.

Пробелы дерева. Пробный прогон с подменённой моделью и in-memory экспортёром
дал четыре span: ``ToolCallingAgent.run`` (kind agent), ``Step 1`` (kind chain),
``RagSearchTool`` (kind tool) и вложенный ``retrieve-chunks`` (type retriever).
Теги прогона: ``agent:smolagents``, ``agent_version:v1``, имя модели.
``dataset_run`` нет. Span ``compress-history`` нет.

- Rolling summary нет: ``ContextManager`` не подключён. ``reset=False`` только
  сохраняет память smolagents между репликами одного процесса.
- Инструментатор не выделяет retrieval. ``score`` лежит в JSON ответа tool-span
  ``RagSearchTool``. Span ``retrieve-chunks`` открывается вокруг уже посчитанного
  результата и содержит ``score`` в ``langfuse.observation.output``.
- Поколение модели в этом прогоне не появилось: инструментатор оборачивает
  ``generate`` только у классов, которые smolagents экспортирует. У живого
  ``OpenAIServerModel`` это span ``OpenAIModel.generate``, не ``call-model``.
- Остановка по ``max_steps`` есть: ``RunResult.state == "max_steps_error"``.
  На ещё открытом ``ToolCallingAgent.run`` выставляется
  ``langfuse.observation.level = WARNING``. Статус OpenTelemetry при этом
  остаётся OK: его выставляет инструментатор.
- Таймаут реплики возвращает управление сразу и ставит ``interrupt_switch``.
  Span закроется, когда заблокированный ``run`` завершится в фоновом потоке.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import uuid
from typing import Any, Protocol

from smolagents import Tool, ToolCallingAgent
from smolagents.memory import ActionStep
from smolagents.monitoring import LogLevel
from smolagents.utils import AgentMaxStepsError

from src.config import Config, PromptsConfig, load_config, load_prompts
from src.mcp_arxiv.client import ArxivMcpClient
from src.tools.native_tools import rag_search, save_research_note
from src.tracing import AGENT_VERSION, ENVIRONMENT

AGENT_NAME = "smolagents"
TRACE_NAME = "answer-question"
_MISSING_KEYS = "Langfuse включён, но ключи не заданы. Трейсы не пишутся."
_instrumented = False


class _McpClient(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...


class McpBackgroundSession:
    """Один MCP-сеанс в фоновом потоке. ``forward`` только отправляет туда корутину."""

    def __init__(self, client: _McpClient) -> None:
        self._client = client
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="arxiv-mcp", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise TimeoutError("MCP-сеанс не открылся за 30 с")
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._client.__aenter__())
        except Exception as exc:
            self._error = exc
            if not self._loop.is_closed():
                self._loop.close()
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        future = asyncio.run_coroutine_threadsafe(
            self._client.call_tool(name, arguments),
            self._loop,
        )
        return future.result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._client.__aexit__(None, None, None),
                self._loop,
            )
            try:
                future.result(timeout=10)
            finally:
                if self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._loop.stop)
        elif not self._loop.is_closed():
            self._loop.close()
        self._thread.join(timeout=5)


def _present(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if value is not None}


def _json(payload: dict[str, Any]) -> str:
    """Строка, а не dict: smolagents иначе импортирует torch и на macOS роняет процесс."""
    return json.dumps(payload, ensure_ascii=False)


def _call_mcp(session: McpBackgroundSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return session.call(name, _present(arguments))
    except Exception as error:
        return {"error": str(error)}


def _record_retrieval(client: Any, query: str, top_n: int | None, result: dict[str, Any]) -> None:
    """Span вокруг уже посчитанных чанков. Поиск повторно не выполняется."""
    chunks = list(result.get("chunks") or [])
    with client.start_as_current_observation(
        as_type="retriever",
        name="retrieve-chunks",
        input={"query": query, "top_n": top_n},
        metadata={"count": result.get("count", len(chunks))},
    ) as observation:
        observation.update(output={"count": result.get("count", len(chunks)), "chunks": chunks})


class ArxivSearchTool(Tool):
    name = "arxiv_search"
    description = "Поиск статей на arXiv."
    inputs = {
        "query": {"type": "string", "description": "Текстовый запрос"},
        "categories": {
            "type": "array",
            "description": "Категории arXiv, например cs.LG",
            "nullable": True,
        },
        "date_from": {
            "type": "string",
            "description": "Нижняя граница даты публикации, YYYY-MM-DD",
            "nullable": True,
        },
        "date_to": {
            "type": "string",
            "description": "Верхняя граница даты публикации, YYYY-MM-DD",
            "nullable": True,
        },
        "max_results": {
            "type": "integer",
            "description": "Сколько статей вернуть. По умолчанию значение сервера.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, session: McpBackgroundSession, description: str) -> None:
        super().__init__()
        self._session = session
        self.description = description

    def forward(
        self,
        query: str,
        categories: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        max_results: int | None = None,
    ) -> str:
        return _json(
            _call_mcp(
                self._session,
                self.name,
                {
                    "query": query,
                    "categories": categories,
                    "date_from": date_from,
                    "date_to": date_to,
                    "max_results": max_results,
                },
            )
        )


class ArxivDetailsTool(Tool):
    name = "arxiv_get_details"
    description = "Метаданные статьи arXiv."
    inputs = {
        "paper_id": {"type": "string", "description": "Идентификатор arXiv, например 2106.09685"},
    }
    output_type = "string"

    def __init__(self, session: McpBackgroundSession, description: str) -> None:
        super().__init__()
        self._session = session
        self.description = description

    def forward(self, paper_id: str) -> str:
        return _json(_call_mcp(self._session, self.name, {"paper_id": paper_id}))


class ArxivDownloadTool(Tool):
    name = "arxiv_download_paper"
    description = "Скачивание PDF статьи arXiv."
    inputs = {
        "paper_id": {"type": "string", "description": "Идентификатор arXiv, например 2106.09685"},
    }
    output_type = "string"

    def __init__(self, session: McpBackgroundSession, description: str) -> None:
        super().__init__()
        self._session = session
        self.description = description

    def forward(self, paper_id: str) -> str:
        return _json(_call_mcp(self._session, self.name, {"paper_id": paper_id}))


class RagSearchTool(Tool):
    name = "rag_search"
    description = "Поиск по локальному индексу."
    inputs = {
        "query": {"type": "string", "description": "Текст поискового запроса"},
        "top_n": {
            "type": "integer",
            "description": "Сколько чанков вернуть. По умолчанию значение из конфигурации.",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, adapter: Any, description: str, langfuse: Any) -> None:
        super().__init__()
        self._adapter = adapter
        self._langfuse = langfuse
        self.description = description

    def forward(self, query: str, top_n: int | None = None) -> str:
        result = rag_search(self._adapter, query, top_n)
        if self._langfuse is not None:
            _record_retrieval(self._langfuse, query, top_n, result)
        return _json(result)


class SaveNoteTool(Tool):
    name = "save_research_note"
    description = "Сохранение исследовательской заметки."
    inputs = {
        "title": {"type": "string", "description": "Заголовок заметки"},
        "text": {"type": "string", "description": "Текст выводов"},
        "paper_ids": {
            "type": "array",
            "description": "Связанные идентификаторы публикаций или чанков",
            "nullable": True,
        },
        "tags": {
            "type": "array",
            "description": "Теги заметки",
            "nullable": True,
        },
    }
    output_type = "string"

    def __init__(self, notes_path: Any, description: str) -> None:
        super().__init__()
        self._notes_path = notes_path
        self.description = description

    def forward(
        self,
        title: str,
        text: str,
        paper_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        return _json(save_research_note(self._notes_path, title, text, paper_ids, tags))


def _instrument() -> None:
    global _instrumented
    if _instrumented:
        return
    from openinference.instrumentation.smolagents import SmolagentsInstrumentor

    SmolagentsInstrumentor().instrument()
    _instrumented = True


def _open_langfuse(config: Config) -> tuple[Any | None, str | None]:
    """Клиент Langfuse на тех же ключе и хосте, что и ``src.tracing``. Без ``auth_check``."""
    if not config.langfuse.enabled:
        return None, None
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        return None, _MISSING_KEYS
    base_url = (
        os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or config.langfuse.host
    )
    from langfuse import Langfuse

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
        environment=ENVIRONMENT,
    )
    _instrument()
    return client, None


def _build_model(config: Config) -> Any:
    from smolagents import OpenAIServerModel

    return OpenAIServerModel(
        model_id=config.llm.resolve_model(),
        api_base=config.llm.resolve_base_url(),
        api_key=config.llm.resolve_api_key(),
        temperature=config.llm.temperature,
        max_tokens=config.llm.max_tokens,
        client_kwargs={"timeout": config.llm.timeout_seconds},
    )


def _tool_calls(steps: list[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for step in steps:
        for call in getattr(step, "tool_calls", None) or []:
            if call.name == "final_answer":
                continue
            calls.append({"name": call.name, "arguments": call.arguments})
    return calls


def _answer_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, default=str)


class SmolRunner:
    """Один агент и один ``session_id`` на процесс. Следующая реплика идёт с ``reset=False``."""

    def __init__(
        self,
        config: Config,
        prompts: PromptsConfig,
        *,
        mcp: _McpClient | None = None,
        adapter: Any | None = None,
        model: Any | None = None,
        langfuse_client: Any | None = None,
    ) -> None:
        self.config = config
        self.model_name = config.llm.resolve_model()
        self.session_id = str(uuid.uuid4())
        self.user_id = config.langfuse.user_id
        self.tags = [
            f"agent:{AGENT_NAME}",
            f"agent_version:{AGENT_VERSION}",
            f"model:{self.model_name}",
        ]
        self._timeout = config.agent.timeout_seconds
        self._max_steps = config.agent.max_steps
        self._instructions = prompts.system_prompt
        warning: str | None = None
        if langfuse_client is not None:
            self._langfuse = langfuse_client
            _instrument()
        else:
            self._langfuse, warning = _open_langfuse(config)
        self.tracing_warning = warning
        self._session = McpBackgroundSession(mcp if mcp is not None else ArxivMcpClient())
        if adapter is None:
            from src.tools.rag_adapter import load_adapter

            adapter = load_adapter(config)
        self.tools: list[Tool] = [
            ArxivSearchTool(self._session, prompts.tool_description("arxiv_search")),
            ArxivDetailsTool(self._session, prompts.tool_description("arxiv_get_details")),
            ArxivDownloadTool(self._session, prompts.tool_description("arxiv_download_paper")),
            RagSearchTool(adapter, prompts.tool_description("rag_search"), self._langfuse),
            SaveNoteTool(config.notes_path, prompts.tool_description("save_research_note")),
        ]
        self._model = model if model is not None else _build_model(config)
        self.agent = self._new_agent()
        self._inflight: threading.Thread | None = None

    def _new_agent(self) -> ToolCallingAgent:
        return ToolCallingAgent(
            tools=self.tools,
            model=self._model,
            instructions=self._instructions,
            max_steps=self._max_steps,
            verbosity_level=LogLevel.ERROR,
            return_full_result=True,
            step_callbacks=[self._on_action_step],
        )

    def _on_action_step(self, step: ActionStep, agent: Any = None) -> None:
        del agent
        if self._langfuse is None or not isinstance(getattr(step, "error", None), AgentMaxStepsError):
            return
        self._langfuse.update_current_span(
            level="WARNING",
            status_message="Остановка по max_steps",
        )

    def ask(self, question: str) -> dict[str, Any]:
        """Одна реплика. По таймауту возвращает управление и не ждёт зависший вызов модели."""
        if self._inflight is not None and self._inflight.is_alive():
            self.agent.interrupt_switch = True
            self.agent = self._new_agent()
        agent = self.agent
        holder: dict[str, Any] = {}

        def _target() -> None:
            try:
                holder["result"] = self._run_turn(agent, question)
            except Exception as exc:
                holder["error"] = exc

        thread = threading.Thread(target=_target, name="smol-turn", daemon=True)
        self._inflight = thread
        thread.start()
        thread.join(self._timeout)
        if thread.is_alive():
            agent.interrupt_switch = True
            raise TimeoutError(f"Реплика превысила таймаут {self._timeout:g} с")
        if "error" in holder:
            raise holder["error"]
        return holder["result"]

    def _run_turn(self, agent: ToolCallingAgent, question: str) -> dict[str, Any]:
        start = len(agent.memory.steps)
        result = self._execute(agent, question)
        output = result.output
        return {
            "answer": _answer_text(output),
            "tool_calls": _tool_calls(agent.memory.steps[start:]),
            "session_id": self.session_id,
            "step_limit": getattr(result, "state", None) == "max_steps_error",
            "model": self.model_name,
        }

    def _execute(self, agent: ToolCallingAgent, question: str) -> Any:
        if self._langfuse is None:
            return agent.run(question, reset=False, return_full_result=True)
        from langfuse import propagate_attributes

        with propagate_attributes(
            user_id=self.user_id,
            session_id=self.session_id,
            tags=self.tags,
            trace_name=TRACE_NAME,
            version=AGENT_VERSION,
            environment=ENVIRONMENT,
        ):
            return agent.run(question, reset=False, return_full_result=True)

    def close(self) -> None:
        self.agent.interrupt_switch = True
        try:
            self._session.close()
        finally:
            if self._langfuse is not None:
                try:
                    self._langfuse.flush()
                except Exception:
                    pass


def _print_result(result: dict[str, Any]) -> None:
    for call in result["tool_calls"]:
        arguments = json.dumps(call["arguments"], ensure_ascii=False, default=str)
        print(f"{call['name']} {arguments}")
    print(result["answer"])
    if result["step_limit"]:
        print("остановлено по лимиту шагов")


def run_cli(question: str, config_path: str | None, overrides: list[str]) -> None:
    config = load_config(config_path, overrides)
    prompts = load_prompts()
    runner = SmolRunner(config, prompts)
    if runner.tracing_warning:
        print(runner.tracing_warning)
    try:
        if question:
            _reply(runner, question)
            return
        print("Диалог с агентом. Пустая строка или exit завершает сессию.")
        while True:
            try:
                line = input("Вы: ").strip()
            except EOFError:
                print()
                return
            if not line or line.lower() in {"exit", "quit"}:
                return
            _reply(runner, line)
            print()
    finally:
        runner.close()


def _reply(runner: SmolRunner, question: str) -> None:
    try:
        _print_result(runner.ask(question))
    except TimeoutError as error:
        print(error)


def main() -> None:
    parser = argparse.ArgumentParser(description="Исследовательский агент на smolagents")
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
    run_cli(" ".join(args.question).strip(), args.config, args.overrides)


if __name__ == "__main__":
    main()
