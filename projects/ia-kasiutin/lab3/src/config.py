"""Загрузка и валидация config.yaml и prompts.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

LAB3_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = LAB3_ROOT / "config.yaml"
DEFAULT_PROMPTS_PATH = LAB3_ROOT / "prompts.yaml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArxivConfig(_Section):
    api_url: str = "https://export.arxiv.org/api/query"
    pdf_base_url: str = "https://arxiv.org/pdf"
    papers_dir: str = "data/papers"
    max_results_limit: int = Field(default=20, ge=1, le=100)
    default_max_results: int = Field(default=5, ge=1)
    rate_limit_seconds: float = Field(default=3.0, ge=0.0)
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_retries: int = Field(default=3, ge=0)
    user_agent: str = "llm26-research-assistant/0.1"

    @model_validator(mode="after")
    def _check_defaults(self) -> ArxivConfig:
        if self.default_max_results > self.max_results_limit:
            raise ValueError("default_max_results не может превышать max_results_limit")
        return self


class QueryEmbeddingConfig(_Section):
    """Параметры кодирования запроса. Параметры сборки индекса в них не входят."""

    model: str = "ai-forever/FRIDA"
    device: Literal["auto", "cpu", "mps", "cuda"] = "auto"
    batch_size: int = Field(default=16, gt=0)
    max_seq_length: int = Field(default=512, gt=0)
    normalize: bool = True
    query_prefix: str = "search_query: "


class RagConfig(_Section):
    index_dir: str = "../lab2/artifacts/faiss/dense-frida-cs400-ov50-aed511d7__hnsw-M32-efc200"
    top_n: int = Field(default=5, ge=1)
    ef_search: int = Field(default=64, ge=1)
    notes_path: str = "data/notes.jsonl"
    embedding: QueryEmbeddingConfig = Field(default_factory=QueryEmbeddingConfig)


class AgentConfig(_Section):
    max_steps: int = Field(default=10, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0.0)
    context_strategy: Literal["rolling_summary"] = "rolling_summary"
    max_context_tokens: int = Field(default=3000, ge=256)
    reserve_output_tokens: int = Field(default=700, ge=64)


class LlmConfig(_Section):
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "openai/gpt-4o-mini"
    api_key_env: str = "OPENROUTER_API_KEY"
    temperature: float = Field(default=0.1, ge=0.0)
    max_tokens: int = Field(default=700, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0.0)

    def resolve_base_url(self) -> str:
        return os.environ.get("OPENROUTER_BASE_URL") or self.base_url

    def resolve_model(self) -> str:
        return os.environ.get("OPENROUTER_MODEL") or self.model

    def resolve_api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(
                f"Не задана переменная окружения {self.api_key_env}. "
                f"Укажите её в .env или экспортируйте в терминале."
            )
        return key


class LangfuseConfig(_Section):
    enabled: bool = True
    host: str = "http://127.0.0.1:3000"
    user_id: str = "local"


class Config(_Section):
    arxiv: ArxivConfig = Field(default_factory=ArxivConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    root: Path = Field(default=LAB3_ROOT, exclude=True)

    def resolve(self, relative: str | Path) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else (self.root / path).resolve()

    @property
    def papers_dir(self) -> Path:
        return self.resolve(self.arxiv.papers_dir)

    @property
    def notes_path(self) -> Path:
        return self.resolve(self.rag.notes_path)

    @property
    def index_dir(self) -> Path:
        return self.resolve(self.rag.index_dir)


class ToolPromptSpec(_Section):
    description: str = Field(min_length=1)


class PromptsConfig(_Section):
    system_prompt: str = Field(min_length=1)
    context_summary_prompt: str = Field(min_length=1)
    tools: dict[str, ToolPromptSpec] = Field(default_factory=dict)

    def tool_description(self, name: str) -> str:
        spec = self.tools.get(name)
        if spec is None:
            raise KeyError(f"Описание инструмента {name!r} не найдено в prompts.yaml")
        return spec.description


def _set_override(data: dict[str, Any], dotted: str, raw: str) -> None:
    keys = dotted.split(".")
    node = data
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = yaml.safe_load(raw)


def load_config(path: str | Path | None = None, overrides: list[str] | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    load_dotenv(cfg_path.resolve().parent / ".env", override=False)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    for item in overrides or []:
        if "=" not in item:
            raise ValueError(f"Переопределение должно быть вида a.b=value: {item!r}")
        dotted, raw = item.split("=", 1)
        _set_override(data, dotted.strip(), raw)
    config = Config.model_validate(data)
    config.root = cfg_path.resolve().parent
    return config


def load_prompts(path: str | Path | None = None) -> PromptsConfig:
    prm_path = Path(path) if path else DEFAULT_PROMPTS_PATH
    data = yaml.safe_load(prm_path.read_text(encoding="utf-8")) or {}
    return PromptsConfig.model_validate(data)
