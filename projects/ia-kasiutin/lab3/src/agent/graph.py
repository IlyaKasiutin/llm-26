"""Граф диалогового агента: вызов LLM и выполнение инструментов."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, StateGraph

from src.agent.context import ContextManager
from src.agent.llm import OpenRouterClient, parse_tool_arguments
from src.config import Config, PromptsConfig
from src.mcp_arxiv.client import ArxivMcpClient
from src.tools.native_tools import native_openai_tools, rag_search, save_research_note
from src.tracing import AGENT_NAME, AGENT_VERSION, NullTracer


class _Llm(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> Any: ...


class _Retriever(Protocol):
    def search(self, query: str, top_n: int | None = None) -> Any: ...


class _Mcp(Protocol):
    async def list_tools(self) -> list[Any]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...


class AgentState(TypedDict):
    """Сообщения chat completions, число вызовов LLM в текущей реплике и след инструментов."""

    messages: list[dict[str, Any]]
    model_messages: list[dict[str, Any]]
    steps: int
    tool_trace: list[dict[str, Any]]
    model: str


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and not message.get("tool_calls"):
            content = message.get("content")
            return content.strip() if isinstance(content, str) else ""
    return ""


class ResearchAgent:
    """Один диалог: история сохраняется между репликами, лимит шагов считается заново на каждую."""

    def __init__(
        self,
        config: Config,
        prompts: PromptsConfig,
        llm: _Llm,
        mcp: _Mcp,
        adapter: _Retriever,
        openai_tools: list[dict[str, Any]],
        mcp_tool_names: set[str],
        notes_path: Path | None = None,
        tracer: Any | None = None,
    ) -> None:
        self.config = config
        self.prompts = prompts
        self.llm = llm
        self.mcp = mcp
        self.adapter = adapter
        self.openai_tools = openai_tools
        self.mcp_tool_names = mcp_tool_names
        self.notes_path = notes_path or config.notes_path
        self.tracer = tracer if tracer is not None else NullTracer(user_id=config.langfuse.user_id)
        self._hit_step_limit = False
        self._events: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.system_prompt}
        ]
        self.context = ContextManager(config.agent, prompts, llm, self.tracer)
        self.graph = self._compile()
        self._recursion_limit = config.agent.max_steps * 4 + 8

    @classmethod
    async def create(
        cls,
        config: Config,
        prompts: PromptsConfig,
        llm: OpenRouterClient | _Llm,
        mcp: ArxivMcpClient | _Mcp,
        adapter: _Retriever,
        notes_path: Path | None = None,
        tracer: Any | None = None,
    ) -> ResearchAgent:
        mcp_tools = await mcp.list_tools()
        openai_tools = [tool.to_openai_tool() for tool in mcp_tools] + native_openai_tools(prompts)
        return cls(
            config,
            prompts,
            llm,
            mcp,
            adapter,
            openai_tools,
            {tool.name for tool in mcp_tools},
            notes_path,
            tracer,
        )

    def _compile(self):
        builder = StateGraph(AgentState)
        builder.add_node("prepare_context", self._prepare_context)
        builder.add_node("llm_call", self._llm_call)
        builder.add_node("execute_tools", self._execute_tools)
        builder.add_node("final_answer", self._final_answer)
        builder.set_entry_point("prepare_context")
        builder.add_conditional_edges(
            "prepare_context",
            self._route_after_prepare,
            {"llm": "llm_call", "final": "final_answer"},
        )
        builder.add_conditional_edges(
            "llm_call",
            self._route_after_llm,
            {"tools": "execute_tools", "end": END},
        )
        builder.add_edge("execute_tools", "prepare_context")
        builder.add_edge("final_answer", END)
        return builder.compile()

    def _trace_tags(self) -> list[str]:
        return [
            f"agent:{AGENT_NAME}",
            f"agent_version:{AGENT_VERSION}",
            f"context_strategy:{self.config.agent.context_strategy}",
            f"model:{self.config.llm.resolve_model()}",
        ]

    def _model_parameters(self) -> dict[str, Any]:
        return {
            "temperature": self.config.llm.temperature,
            "max_tokens": self.config.llm.max_tokens,
        }

    async def _prepare_context(self, state: AgentState) -> dict[str, Any]:
        """Собирает вид для модели. Полный список сообщений не сокращает."""
        with self.tracer.span("prepare-context", as_type="chain") as span:
            previous_summary = self.context.summary
            view = await self.context.prepare(state["messages"], self.openai_tools)
            if view.summary and view.summary != previous_summary:
                self._events.append(
                    {
                        "role": "summary",
                        "text": view.summary,
                        "tokens_before": view.tokens_before,
                        "tokens_after": view.tokens_after,
                    }
                )
            span.update(
                input={"messages": len(state["messages"])},
                output={
                    "tokens": view.tokens_after,
                    "tokens_before": view.tokens_before,
                    "budget": self.context.budget,
                    "limit": self.config.agent.max_context_tokens,
                }
            )
        return {"model_messages": view.messages}

    async def _chat(self, state: AgentState, tools: list[dict[str, Any]] | None, name: str) -> Any:
        with self.tracer.generation(
            name=name,
            input=state["model_messages"],
            model=self.config.llm.resolve_model(),
            model_parameters=self._model_parameters(),
        ) as generation:
            response = await self.llm.chat(state["model_messages"], tools=tools)
            output = response.to_assistant_message()
            if response.reasoning:
                output = {**output, "reasoning": response.reasoning}
            generation.update(
                output=output,
                model=response.model,
                usage={
                    "input": response.prompt_tokens,
                    "output": response.completion_tokens,
                },
                cost=response.cost,
                metadata={"latency_s": response.latency_s},
            )
        return response

    async def _llm_call(self, state: AgentState) -> dict[str, Any]:
        response = await self._chat(state, self.openai_tools, "call-model")
        messages = list(state["messages"])
        messages.append(response.to_assistant_message())
        if response.tool_calls:
            self._events.append(
                {
                    "role": "model",
                    "step": state["steps"] + 1,
                    "content": response.content or "",
                    "reasoning": response.reasoning or "",
                    "tools": [call.name for call in response.tool_calls],
                    "latency_s": response.latency_s,
                }
            )
        return {
            "messages": messages,
            "steps": state["steps"] + 1,
            "model": response.model,
        }

    async def _final_answer(self, state: AgentState) -> dict[str, Any]:
        response = await self._chat(state, None, "write-answer")
        messages = list(state["messages"])
        messages.append(response.to_assistant_message())
        return {"messages": messages, "model": response.model}

    def _route_after_prepare(self, state: AgentState) -> str:
        if state["steps"] >= self.config.agent.max_steps:
            self._hit_step_limit = True
            return "final"
        return "llm"

    def _route_after_llm(self, state: AgentState) -> str:
        if state["messages"][-1].get("tool_calls"):
            return "tools"
        return "end"

    async def _execute_tools(self, state: AgentState) -> dict[str, Any]:
        messages = list(state["messages"])
        assistant = messages[-1]
        trace = list(state["tool_trace"])
        for call in assistant.get("tool_calls") or []:
            name = call.get("function", {}).get("name") or ""
            raw_arguments = call.get("function", {}).get("arguments")
            arguments, parse_error = parse_tool_arguments(raw_arguments)
            metadata = {"mcp_server": "arxiv"} if name in self.mcp_tool_names else {}
            started = time.perf_counter()
            with self.tracer.span(
                name=name or "tool",
                as_type="tool",
                input=arguments,
                metadata=metadata or None,
            ) as span:
                if parse_error:
                    output: dict[str, Any] = {"error": parse_error}
                else:
                    output = await self._call_tool(name, arguments)
                self._trace_retrieval(name, arguments, output)
                metadata["latency_s"] = time.perf_counter() - started
                span.update(
                    output=output,
                    metadata=metadata,
                    level="ERROR" if "error" in output else None,
                )
            trace.append(
                {
                    "step": state["steps"],
                    "tool": name,
                    "arguments": arguments,
                    "output": output,
                }
            )
            self._events.append(
                {
                    "role": "tool",
                    "name": name,
                    "arguments": arguments,
                    "output": output,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(output, ensure_ascii=False),
                }
            )
        return {"messages": messages, "tool_trace": trace}

    def _trace_retrieval(self, name: str, arguments: dict[str, Any], output: dict[str, Any]) -> None:
        if name != "rag_search" or not isinstance(output.get("chunks"), list):
            return
        chunks = [
            {
                "paper_id": item.get("paper_id"),
                "chunk_id": item.get("chunk_id"),
                "score": item.get("score"),
            }
            for item in output["chunks"]
            if isinstance(item, dict)
        ]
        with self.tracer.span(
            name="retrieve-chunks",
            as_type="retriever",
            input={"query": arguments.get("query"), "top_n": arguments.get("top_n")},
            metadata={"count": len(chunks), "chunks": chunks},
        ) as retrieval:
            retrieval.update(output={"count": len(chunks), "chunks": chunks})

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "rag_search":
                top_n = arguments.get("top_n")
                return rag_search(
                    self.adapter,
                    str(arguments.get("query") or ""),
                    top_n if isinstance(top_n, int) and not isinstance(top_n, bool) else None,
                )
            if name == "save_research_note":
                title = arguments.get("title")
                text = arguments.get("text")
                if not isinstance(title, str) or not isinstance(text, str):
                    return {"error": "Поля title и text должны быть строками"}
                paper_ids = arguments.get("paper_ids")
                tags = arguments.get("tags")
                return save_research_note(
                    self.notes_path,
                    title,
                    text,
                    paper_ids if isinstance(paper_ids, list) else [],
                    tags if isinstance(tags, list) else [],
                )
            if name in self.mcp_tool_names:
                return await self.mcp.call_tool(name, arguments)
            return {"error": f"Неизвестный инструмент {name}"}
        except Exception as error:  # инструмент не должен останавливать граф
            return {"error": str(error)}

    async def ask(self, user_text: str) -> dict[str, Any]:
        """Добавляет реплику пользователя и прогоняет граф в пределах таймаута."""
        messages = list(self.messages)
        messages.append({"role": "user", "content": user_text})
        state: dict[str, Any] = {
            "messages": messages,
            "model_messages": [],
            "steps": 0,
            "tool_trace": [],
            "model": "",
        }
        self._hit_step_limit = False
        self._events = []
        try:
            with self.tracer.trace(
                name="answer-question",
                input=user_text,
                tags=self._trace_tags(),
            ) as turn:
                try:
                    result = await asyncio.wait_for(
                        self.graph.ainvoke(state, {"recursion_limit": self._recursion_limit}),
                        timeout=self.config.agent.timeout_seconds,
                    )
                except TimeoutError as error:
                    message = f"Реплика превысила таймаут {self.config.agent.timeout_seconds} с"
                    turn.update(level="ERROR", output=message)
                    raise TimeoutError(message) from error
                self.messages = list(result["messages"])
                answer = _last_assistant_text(self.messages)
                turn.update(output=answer)
                if self._hit_step_limit:
                    turn.update(level="WARNING")
                return {
                    "answer": answer,
                    "steps": result["steps"],
                    "tool_calls": result["tool_trace"],
                    "events": list(self._events),
                    "step_limit": self._hit_step_limit,
                    "model": result.get("model") or "",
                }
        finally:
            self.tracer.flush()
