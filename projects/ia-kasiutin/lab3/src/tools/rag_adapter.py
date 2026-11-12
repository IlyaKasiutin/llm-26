"""Dense-поиск по готовому FAISS-индексу. Индекс не пересобирается."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import faiss
import numpy as np
from pydantic import BaseModel, ConfigDict

from src.config import Config, QueryEmbeddingConfig

INDEX_FILE = "index.faiss"
DOCSTORE_FILE = "docstore.jsonl"


class RetrievedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    chunk_id: str
    title: str | None = None
    section: str | None = None
    page: int | None = None
    text: str
    score: float


class QueryEncoderProtocol(Protocol):
    def encode_query(self, text: str) -> np.ndarray:
        """Возвращает матрицу формы (1, dim)."""


def resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class QueryEncoder:
    """Кодирует запрос той же моделью и префиксом, которыми собран индекс."""

    def __init__(self, config: QueryEmbeddingConfig) -> None:
        from sentence_transformers import SentenceTransformer

        self.config = config
        self.model = SentenceTransformer(config.model, device=resolve_device(config.device))
        self.model.max_seq_length = config.max_seq_length

    def encode_query(self, text: str) -> np.ndarray:
        vector = self.model.encode(
            [self.config.query_prefix + text],
            batch_size=self.config.batch_size,
            normalize_embeddings=self.config.normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.ascontiguousarray(vector, dtype=np.float32)


class ChunkRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    faiss_id: int
    text: str
    chunk_id: str | None = None
    metadata: dict = {}


def _optional_str(metadata: dict, *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def to_chunk(record: ChunkRecord, score: float) -> RetrievedChunk:
    metadata = record.metadata
    page = metadata.get("page")
    chunk_id = record.chunk_id or metadata.get("chunk_id") or str(record.faiss_id)
    return RetrievedChunk(
        paper_id=str(metadata.get("source") or ""),
        chunk_id=str(chunk_id),
        title=_optional_str(metadata, "h1", "section"),
        section=_optional_str(metadata, "section"),
        page=page if isinstance(page, int) and not isinstance(page, bool) else None,
        text=record.text,
        score=round(float(score), 4),
    )


def _set_ef_search(index: faiss.Index, ef_search: int) -> None:
    try:
        hnsw = faiss.downcast_index(index).hnsw
    except AttributeError:
        return
    hnsw.efSearch = ef_search


def _load_docstore(path: Path) -> list[ChunkRecord]:
    records: dict[int, ChunkRecord] = {}
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = ChunkRecord.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValueError) as error:
                raise ValueError(f"Строка {line_no} в {path} не является чанком: {error}") from error
            records[record.faiss_id] = record
    if not records:
        raise ValueError(f"В {path} нет чанков")
    expected = list(range(len(records)))
    if sorted(records) != expected:
        raise ValueError(f"faiss_id в {path} должны идти подряд от 0 до {len(records) - 1}")
    return [records[i] for i in expected]


class DenseIndex:
    """Готовый FAISS-индекс и тексты чанков. Поиск — inner product, без BM25."""

    def __init__(self, index: faiss.Index, chunks: list[ChunkRecord]) -> None:
        if index.ntotal != len(chunks):
            raise ValueError(f"В индексе {index.ntotal} векторов, в docstore {len(chunks)} чанков")
        self.index = index
        self.chunks = chunks

    @classmethod
    def load(cls, directory: Path, ef_search: int) -> DenseIndex:
        index_path = directory / INDEX_FILE
        docstore_path = directory / DOCSTORE_FILE
        if not index_path.is_file():
            raise FileNotFoundError(f"Нет файла индекса: {index_path}")
        if not docstore_path.is_file():
            raise FileNotFoundError(f"Нет docstore: {docstore_path}")
        index = faiss.read_index(str(index_path))
        _set_ef_search(index, ef_search)
        return cls(index, _load_docstore(docstore_path))

    def search(self, query: np.ndarray, k: int) -> list[RetrievedChunk]:
        if k < 1:
            raise ValueError("k должен быть >= 1")
        vector = np.ascontiguousarray(query, dtype=np.float32)
        if vector.ndim == 1:
            vector = vector.reshape(1, -1)
        scores, ids = self.index.search(vector, min(k, self.index.ntotal))
        hits: list[RetrievedChunk] = []
        for score, idx in zip(scores[0].tolist(), ids[0].tolist()):
            if idx < 0 or idx >= len(self.chunks):
                continue
            hits.append(to_chunk(self.chunks[idx], score))
            if len(hits) >= k:
                break
        return hits


class RagAdapter:
    """Поиск по локальному корпусу: эмбеддинг запроса и top-n из FAISS."""

    def __init__(self, index: DenseIndex, encoder: QueryEncoderProtocol, top_n: int) -> None:
        self.index = index
        self.encoder = encoder
        self.top_n = top_n

    def search(self, query: str, top_n: int | None = None) -> list[RetrievedChunk]:
        limit = self.top_n if top_n is None else top_n
        if limit < 1:
            raise ValueError("top_n должен быть >= 1")
        return self.index.search(self.encoder.encode_query(query), limit)


def load_adapter(config: Config, encoder: QueryEncoderProtocol | None = None) -> RagAdapter:
    index = DenseIndex.load(config.index_dir, config.rag.ef_search)
    return RagAdapter(
        index,
        encoder if encoder is not None else QueryEncoder(config.rag.embedding),
        config.rag.top_n,
    )
