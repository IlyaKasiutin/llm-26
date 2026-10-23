"""Загрузка и валидация общего config.yaml."""

from __future__ import annotations

import hashlib
import json
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

LAB2_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = LAB2_ROOT / "config.yaml"

SPLITTER_TYPES = {"markdown_header", "recursive", "hybrid"}
HEADING_MODES = {"none", "metadata", "text", "both"}
VECTORIZATIONS = {"dense", "sparse", "hybrid"}
DEVICES = {"auto", "cpu", "mps", "cuda"}
INDEX_TYPES = {"hnsw", "flat"}
QUANTIZATIONS = {"none", "fp16", "sq8"}
METRICS = {"cosine"}


@dataclass
class SourcesConfig:
    dir: str = "sources"
    reparse: bool = False


@dataclass
class SplitterConfig:
    type: str = "markdown_header"
    chunk_size: int = 400
    chunk_overlap: int = 50
    header_levels: int = 3
    include_headings: str = "both"


@dataclass
class SparseConfig:
    k1: float = 1.5
    b: float = 0.75


@dataclass
class EmbeddingConfig:
    vectorization: str = "dense"
    model: str = "ai-forever/FRIDA"
    device: str = "auto"
    batch_size: int = 16
    max_seq_length: int = 512
    normalize: bool = True
    query_prefix: str = "search_query: "
    document_prefix: str = "search_document: "
    sparse: SparseConfig = field(default_factory=SparseConfig)


@dataclass
class HnswConfig:
    M: int = 32
    ef_construction: int = 200
    ef_search: int = 64


@dataclass
class IndexConfig:
    type: str = "hnsw"
    metric: str = "cosine"
    hnsw: HnswConfig = field(default_factory=HnswConfig)
    quantization: str = "none"
    dir: str = "faiss"


@dataclass
class RagConfig:
    collection: str | None = None
    top_n: int = 5
    min_score: float | None = None
    max_context_tokens: int = 3000
    snippet_chars: int = 240


@dataclass
class LlmConfig:
    base_url: str | None = None
    model: str = "auto"
    temperature: float = 0.1
    top_p: float = 0.9
    max_tokens: int = 700
    repetition_penalty: float = 1.15
    timeout_s: float = 120


@dataclass
class ArtifactsConfig:
    dir: str = "artifacts"


@dataclass
class Config:
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    splitter: SplitterConfig = field(default_factory=SplitterConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    index: IndexConfig = field(default_factory=IndexConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)
    root: Path = LAB2_ROOT  # каталог, от которого считаются относительные пути

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else self.root / path

    @property
    def sources_dir(self) -> Path:
        return self.resolve(self.sources.dir)

    @property
    def artifacts_dir(self) -> Path:
        return self.resolve(self.artifacts.dir)

    @property
    def faiss_dir(self) -> Path:
        return self.artifacts_dir / self.index.dir

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("root")
        return data


def _build(cls: type, data: dict[str, Any] | None, path: str) -> Any:
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидается секция, получено {data!r}")
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Неизвестные ключи в {path or 'корне'}: {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        factory = known[name].default_factory
        sub = factory() if factory is not MISSING else None
        if sub is not None and is_dataclass(sub):
            kwargs[name] = _build(type(sub), value, f"{path}.{name}".lstrip("."))
        else:
            kwargs[name] = value
    return cls(**kwargs)


def _set_override(data: dict[str, Any], dotted: str, raw: str) -> None:
    keys = dotted.split(".")
    node = data
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = yaml.safe_load(raw)  # 300 -> int, true -> bool, "x" -> str


def validate(config: Config) -> None:
    s, e = config.splitter, config.embedding
    if s.type not in SPLITTER_TYPES:
        raise ValueError(f"splitter.type должен быть одним из {sorted(SPLITTER_TYPES)}")
    if s.include_headings not in HEADING_MODES:
        raise ValueError(f"splitter.include_headings: {sorted(HEADING_MODES)}")
    if e.vectorization not in VECTORIZATIONS:
        raise ValueError(f"embedding.vectorization: {sorted(VECTORIZATIONS)}")
    if e.device not in DEVICES:
        raise ValueError(f"embedding.device: {sorted(DEVICES)}")
    if not 1 <= s.header_levels <= 6:
        raise ValueError("splitter.header_levels должен быть в диапазоне 1..6")
    if s.chunk_size <= 0 or s.chunk_overlap < 0 or s.chunk_overlap >= s.chunk_size:
        raise ValueError("Нужно 0 <= chunk_overlap < chunk_size")
    if s.chunk_size > e.max_seq_length:
        raise ValueError(
            f"chunk_size={s.chunk_size} больше embedding.max_seq_length={e.max_seq_length}: "
            "модель обрежет вход"
        )
    if e.batch_size <= 0:
        raise ValueError("embedding.batch_size должен быть > 0")
    i = config.index
    if i.type not in INDEX_TYPES:
        raise ValueError(f"index.type: {sorted(INDEX_TYPES)}")
    if i.metric not in METRICS:
        raise ValueError(f"index.metric: {sorted(METRICS)}")
    if i.quantization not in QUANTIZATIONS:
        raise ValueError(f"index.quantization: {sorted(QUANTIZATIONS)}")
    if i.type == "flat" and i.quantization != "none":
        raise ValueError("index.quantization поддерживается только для type=hnsw")
    if i.hnsw.M < 4 or i.hnsw.ef_construction < i.hnsw.M or i.hnsw.ef_search < 1:
        raise ValueError("Нужно hnsw.M >= 4, ef_construction >= M, ef_search >= 1")
    r, m = config.rag, config.llm
    if r.top_n < 1 or r.max_context_tokens < 1 or r.snippet_chars < 1:
        raise ValueError("rag.top_n, max_context_tokens и snippet_chars должны быть >= 1")
    if m.max_tokens < 1 or m.timeout_s <= 0 or m.temperature < 0 or not 0 < m.top_p <= 1:
        raise ValueError("Некорректные llm.max_tokens/timeout_s/temperature/top_p")


def load_config(path: str | Path | None = None, overrides: list[str] | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for item in overrides or []:
        if "=" not in item:
            raise ValueError(f"Переопределение должно быть вида a.b=value: {item!r}")
        dotted, raw = item.split("=", 1)
        _set_override(data, dotted.strip(), raw)
    config = _build(Config, data, "")
    config.root = path.resolve().parent
    validate(config)
    return config


def sources_fingerprint(directory: Path) -> list[list[Any]]:
    items = []
    for item in sorted(directory.rglob("*")):
        if item.is_file():
            stat = item.stat()
            items.append([item.relative_to(directory).as_posix(), stat.st_size, stat.st_mtime_ns])
    return items


def config_hash(config: Config) -> str:
    """Хэш того, что влияет на содержимое артефактов (device и batch_size не входят)."""
    embedding = config.to_dict()["embedding"]
    embedding.pop("device")
    embedding.pop("batch_size")
    if config.embedding.vectorization == "dense":
        embedding.pop("sparse")
    payload = {
        "splitter": config.to_dict()["splitter"],
        "embedding": embedding,
        "sources": sources_fingerprint(config.sources_dir),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
