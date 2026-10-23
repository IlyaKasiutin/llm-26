"""Разбиение Documents на чанки; длина считается в токенах токенайзера эмбеддера."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from functools import lru_cache

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from .config import Config

HEADER_KEYS = ["h1", "h2", "h3", "h4", "h5", "h6"]
SEPARATORS = ["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""]


@lru_cache(maxsize=4)
def load_tokenizer(model: str):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(model)


def token_counter(model: str) -> Callable[[str], int]:
    tokenizer = load_tokenizer(model)

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False))

    return count


def _recursive(config: Config, length: Callable[[str], int]) -> RecursiveCharacterTextSplitter:
    s = config.splitter
    return RecursiveCharacterTextSplitter(
        chunk_size=s.chunk_size,
        chunk_overlap=s.chunk_overlap,
        length_function=length,
        separators=SEPARATORS,
    )


def _header_sections(page: Document, levels: int) -> list[Document]:
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#" * (i + 1), HEADER_KEYS[i]) for i in range(levels)],
        strip_headers=False,
    )
    sections = splitter.split_text(page.page_content)
    return [
        Document(page_content=sec.page_content, metadata={**page.metadata, **sec.metadata})
        for sec in sections
        if sec.page_content.strip()
    ]


def _merge_small_sections(
    sections: list[Document], chunk_size: int, length: Callable[[str], int]
) -> list[Document]:
    """Короткие секции (меньше четверти chunk_size) склеиваются со следующей, если влезают."""
    threshold = chunk_size // 4
    merged: list[Document] = []
    carry: Document | None = None
    for sec in sections:
        if carry is not None:
            combined = carry.page_content + "\n\n" + sec.page_content
            if length(combined) <= chunk_size:
                sec = Document(page_content=combined, metadata=carry.metadata)
            else:
                merged.append(carry)
            carry = None
        if length(sec.page_content) < threshold:
            carry = sec
        else:
            merged.append(sec)
    if carry is not None:
        merged.append(carry)
    return merged


def _heading_path(metadata: dict) -> list[str]:
    path = [metadata[key] for key in HEADER_KEYS if metadata.get(key)]
    if not path and metadata.get("section"):
        path = [metadata["section"]]
    return path


def split_documents(documents: list[Document], config: Config) -> list[Document]:
    s = config.splitter
    length = token_counter(config.embedding.model)
    recursive = _recursive(config, length)
    chunks: list[Document] = []

    for page in documents:
        if s.type == "recursive":
            pieces = [
                Document(page_content=text, metadata=dict(page.metadata))
                for text in recursive.split_text(page.page_content)
            ]
        else:
            sections = _header_sections(page, s.header_levels)
            if s.type == "hybrid":
                sections = _merge_small_sections(sections, s.chunk_size, length)
            pieces = []
            for sec in sections:
                if length(sec.page_content) <= s.chunk_size and s.type == "markdown_header":
                    pieces.append(sec)
                else:
                    pieces.extend(
                        Document(page_content=text, metadata=dict(sec.metadata))
                        for text in recursive.split_text(sec.page_content)
                    )

        for piece in pieces:
            body = piece.page_content.strip()
            if not body:
                continue
            metadata = dict(piece.metadata)
            path = _heading_path(metadata)
            text = body
            if s.include_headings in {"text", "both"} and path:
                text = f"{' > '.join(path)}\n\n{body}"
            if s.include_headings in {"none", "text"}:
                for key in HEADER_KEYS:
                    metadata.pop(key, None)
            metadata["chunk_index"] = len(chunks)
            metadata["n_tokens"] = length(text)
            key = f"{metadata.get('source')}|{metadata.get('page')}|{len(chunks)}|{text}"
            metadata["chunk_id"] = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            chunks.append(Document(page_content=text, metadata=metadata))
    return chunks
