from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc.document import DoclingDocument

LAB2_ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = LAB2_ROOT / "sources" / "diploma.pdf"
OUTPUT_PATH = Path(__file__).resolve().parent / "output" / "diploma.md"

# Дробь MTTF на странице 41 в текстовом слое PDF приходит удвоенными глифами.
_BROKEN_MTTF = re.compile(
    r"MTTF\s*=\s*1?\s*𝑛+\s*[×x]\s*∑\s*\(\s*𝑡+\s*сбой\s*,\s*𝑖+\s*[−–-]\s*𝑡+\s*нач\.работы\s*,\s*𝑖+\s*\)\s*𝑛+\s*𝑖+\s*=\s*1",
    re.IGNORECASE,
)
_LATEX_MTTF = (
    "$$\n"
    r"\mathrm{MTTF} = \frac{1}{n} \sum_{i=1}^{n}"
    r" (t_{\text{сбой},i} - t_{\text{нач.работы},i})"
    "\n$$"
)
# Та же дробь после formula enrichment: кириллица в индексах распознана латиницей.
_DOCLING_MTTF = re.compile(
    r"\$\$\s*M\s*T\s*T\s*F\s*=\s*\\frac\s*\{\s*1\s*\}\s*\{\s*n\s*\}[^$]*\$\$"
)


def pdf_modified_date(path: Path) -> str:
    match = re.search(br"/ModDate\s*\(D:(\d{8})", path.read_bytes())
    if match is None:
        return ""
    raw = match.group(1).decode()
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def front_matter(pdf_path: Path) -> str:
    relative = pdf_path.relative_to(LAB2_ROOT).as_posix()
    return (
        "---\n"
        f"source: {pdf_path.name}\n"
        f"path: {relative}\n"
        f"modified: {pdf_modified_date(pdf_path)}\n"
        "---\n"
    )


def pipeline_options() -> PdfPipelineOptions:
    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_formula_enrichment = True
    options.generate_picture_images = False
    options.generate_page_images = False
    return options


def convert(pdf_path: Path) -> DoclingDocument:
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options())
        }
    )
    return converter.convert(pdf_path).document


def is_section_header(item: object) -> bool:
    label = getattr(item, "label", None)
    if label is None:
        return False
    return str(getattr(label, "value", label)) == "section_header"


def item_page(item: object) -> int | None:
    provenance = getattr(item, "prov", None) or []
    if not provenance:
        return None
    page_no = getattr(provenance[0], "page_no", None)
    if page_no is None:
        return None
    return int(page_no)


def sections_by_page(document: DoclingDocument, page_count: int) -> dict[int, str]:
    """Последний заголовок раздела, который действует на странице."""
    current = ""
    seen: dict[int, str] = {}
    for item, _level in document.iterate_items():
        if is_section_header(item):
            text = getattr(item, "text", "") or ""
            cleaned = " ".join(text.split())
            if cleaned:
                current = cleaned
        page_no = item_page(item)
        if page_no is not None:
            seen[page_no] = current

    resolved: dict[int, str] = {}
    carried = ""
    for page in range(1, page_count + 1):
        if page in seen:
            carried = seen[page]
        resolved[page] = carried
    return resolved


def page_count(document: DoclingDocument) -> int:
    if not document.pages:
        return 0
    return max(int(page) for page in document.pages)


def page_marker(source_name: str, page: int, section: str) -> str:
    clean = " ".join(section.split()).replace("--", "—")
    return f'<!-- source: {source_name} page: {page} section: "{clean}" -->'


def page_body(document: DoclingDocument, page: int) -> str:
    body = document.export_to_markdown(page_no=page, image_placeholder="")
    return body.strip()


def replace_broken_mttf(markdown: str) -> str:
    # Подстановка функцией, чтобы обратные слэши LaTeX не читались как escape regex.
    markdown = _BROKEN_MTTF.sub(lambda _match: _LATEX_MTTF, markdown)
    return _DOCLING_MTTF.sub(lambda _match: _LATEX_MTTF, markdown)


def iter_pages(document: DoclingDocument) -> Iterator[tuple[int, str, str]]:
    """Возвращает (страница, раздел, markdown страницы); тело может быть пустым."""
    total = page_count(document)
    sections = sections_by_page(document, total)
    for page in range(1, total + 1):
        yield page, sections[page], replace_broken_mttf(page_body(document, page))


def render(document: DoclingDocument, pdf_path: Path) -> str:
    parts = [front_matter(pdf_path).rstrip()]
    for page, section, body in iter_pages(document):
        parts.append(page_marker(pdf_path.name, page, section))
        if body:
            parts.append(body)
    return "\n\n".join(parts) + "\n"


def main() -> None:
    if not PDF_PATH.is_file():
        raise SystemExit(f"Не найден PDF: {PDF_PATH}")
    document = convert(PDF_PATH)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render(document, PDF_PATH), encoding="utf-8")
    print(f"Записано {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
