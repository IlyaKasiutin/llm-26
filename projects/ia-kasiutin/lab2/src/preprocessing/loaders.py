"""LangChain-загрузчики документов поверх Docling-парсера из parse_pdf.py."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

try:
    from . import parse_pdf
except ImportError:  # запуск как скрипта: python loaders.py
    import parse_pdf

OUTPUT_DIR = parse_pdf.OUTPUT_PATH.parent
SOURCES_DIR = parse_pdf.LAB2_ROOT / "sources"
CACHE_VERSION = 1


def _relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(parse_pdf.LAB2_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _modified_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat()


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"version": CACHE_VERSION, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


class DoclingPageLoader(BaseLoader):
    """PDF -> по одному Document на страницу (markdown + метаданные).

    Результат кэшируется в `cache_dir/<stem>.docs.jsonl`, а читаемый markdown
    пишется в `cache_dir/<stem>.md`. Кэш инвалидируется по mtime/size PDF.
    """

    def __init__(
        self,
        file_path: str | Path,
        cache_dir: str | Path = OUTPUT_DIR,
        reparse: bool = False,
    ) -> None:
        self.file_path = Path(file_path)
        self.cache_dir = Path(cache_dir)
        self.reparse = reparse

    @property
    def _cache_path(self) -> Path:
        return self.cache_dir / f"{self.file_path.stem}.docs.jsonl"

    @property
    def _markdown_path(self) -> Path:
        return self.cache_dir / f"{self.file_path.stem}.md"

    def lazy_load(self) -> Iterator[Document]:
        if not self.reparse:
            cached = self._read_cache()
            if cached is not None:
                yield from cached
                return
        yield from self._parse_and_cache()

    def _read_cache(self) -> list[Document] | None:
        if not self._cache_path.is_file():
            return None
        with self._cache_path.open(encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        if not lines or json.loads(lines[0]) != _fingerprint(self.file_path):
            return None
        return [Document(**json.loads(line)) for line in lines[1:]]

    def _parse_and_cache(self) -> list[Document]:
        document = parse_pdf.convert(self.file_path)
        base = {
            "source": self.file_path.name,
            "path": _relative_path(self.file_path),
            "modified": parse_pdf.pdf_modified_date(self.file_path),
            "format": "pdf",
            "parser": "docling",
        }
        docs = [
            Document(page_content=body, metadata={**base, "page": page, "section": section})
            for page, section, body in parse_pdf.iter_pages(document)
            if body
        ]

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._markdown_path.write_text(
            parse_pdf.render(document, self.file_path), encoding="utf-8"
        )
        with self._cache_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(_fingerprint(self.file_path)) + "\n")
            for doc in docs:
                record = {"page_content": doc.page_content, "metadata": doc.metadata}
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return docs


class MarkdownFileLoader(BaseLoader):
    """MD -> один Document на файл."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def lazy_load(self) -> Iterator[Document]:
        yield Document(
            page_content=self.file_path.read_text(encoding="utf-8"),
            metadata={
                "source": self.file_path.name,
                "path": _relative_path(self.file_path),
                "modified": _modified_date(self.file_path),
                "format": "md",
                "parser": "plain",
            },
        )


class DoclingGenericLoader(BaseLoader):
    """DOCX/PPTX/HTML -> один Document на файл через Docling (без страниц)."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def lazy_load(self) -> Iterator[Document]:
        from docling.document_converter import DocumentConverter

        document = DocumentConverter().convert(self.file_path).document
        yield Document(
            page_content=document.export_to_markdown(image_placeholder=""),
            metadata={
                "source": self.file_path.name,
                "path": _relative_path(self.file_path),
                "modified": _modified_date(self.file_path),
                "format": self.file_path.suffix.lstrip(".").lower(),
                "parser": "docling",
            },
        )


def loader_for(path: Path, reparse: bool = False) -> BaseLoader:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return DoclingPageLoader(path, reparse=reparse)
    if suffix in {".md", ".markdown"}:
        return MarkdownFileLoader(path)
    if suffix in {".docx", ".pptx", ".html", ".htm"}:
        return DoclingGenericLoader(path)
    raise ValueError(f"Неподдерживаемый формат: {path}")


def load_documents(
    sources_dir: str | Path = SOURCES_DIR, reparse: bool = False
) -> list[Document]:
    supported = {".pdf", ".md", ".markdown", ".docx", ".pptx", ".html", ".htm"}
    documents: list[Document] = []
    for path in sorted(Path(sources_dir).rglob("*")):
        if path.is_file() and path.suffix.lower() in supported:
            documents.extend(loader_for(path, reparse=reparse).load())
    return documents


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсинг источников в LangChain Documents")
    parser.add_argument("--sources", default=str(SOURCES_DIR))
    parser.add_argument("--reparse", action="store_true", help="игнорировать кэш")
    args = parser.parse_args()

    docs = load_documents(args.sources, reparse=args.reparse)
    print(f"Documents: {len(docs)}")
    if docs:
        print("Пример metadata:", docs[0].metadata)
    print("Формулы MTTF (LaTeX):", sum(r"\mathrm{MTTF}" in d.page_content for d in docs))


if __name__ == "__main__":
    main()
