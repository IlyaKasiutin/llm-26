"""Самостоятельный MCP-сервер (FastMCP) для поиска и загрузки статей с arXiv."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from src.config import ArxivConfig, Config, PromptsConfig, load_config, load_prompts

logger = logging.getLogger("mcp_arxiv")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

_NEW_ID_RE = re.compile(r"^(?P<base>\d{4}\.\d{4,5})(?P<ver>v\d+)?$")
_OLD_ID_RE = re.compile(r"^(?P<base>[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?P<ver>v\d+)?$", re.IGNORECASE)
_URL_PREFIX_RE = re.compile(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"^[A-Za-z\-]+(?:\.[A-Za-z\-]+)?$")
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ArxivValidationError(ValueError):
    """Ошибка валидации входных параметров запроса к arXiv."""


class ArxivNetworkError(RuntimeError):
    """Сетевая ошибка или ошибка ответа arXiv API."""


class ArxivPaperSummary(BaseModel):
    arxiv_id: str
    version: str | None = None
    title: str
    authors: list[str]
    published: str
    updated: str
    primary_category: str | None = None
    categories: list[str]
    summary: str
    abs_url: str
    pdf_url: str


class ArxivPaperDetails(ArxivPaperSummary):
    abstract: str
    comment: str | None = None
    journal_ref: str | None = None
    doi: str | None = None


def normalize_arxiv_id(raw_id: str) -> tuple[str, str]:
    """Очищает и валидирует идентификатор статьи arXiv.

    Возвращает кортеж `(canonical_id, versioned_id)`:
    - `canonical_id` — базовый ID без суффикса версии `vN` (для дедупликации);
    - `versioned_id` — ID с сохранённой версией (если она была указана).
    """
    cleaned = raw_id.strip()
    if not cleaned:
        raise ArxivValidationError("Пустой идентификатор arXiv ID")

    cleaned = _URL_PREFIX_RE.sub("", cleaned)
    if cleaned.lower().startswith("arxiv:"):
        cleaned = cleaned[6:].strip()
    if cleaned.lower().endswith(".pdf"):
        cleaned = cleaned[:-4].strip()
    cleaned = cleaned.strip("/")

    match = _NEW_ID_RE.match(cleaned) or _OLD_ID_RE.match(cleaned)
    if not match:
        raise ArxivValidationError(
            f"Некорректный формат arXiv ID: {raw_id!r}. "
            "Ожидается формат YYMM.NNNNN (например, '2106.09685' или '2106.09685v2') "
            "или старый формат archive/YYMMNNN (например, 'cs/0112017')."
        )
    base = match.group("base")
    ver = match.group("ver") or ""
    return base, f"{base}{ver}"


def _parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ArxivValidationError(
            f"Некорректная дата в {field_name}={value!r}: ожидается YYYY-MM-DD"
        ) from exc


def build_search_query(
    query: str,
    categories: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """Формирует поисковое выражение `search_query` для arXiv Atom API."""
    cleaned_query = " ".join(query.strip().split())
    if not cleaned_query:
        raise ArxivValidationError("Поисковый запрос query не может быть пустым")

    parts: list[str] = [f"all:({cleaned_query})"]

    if categories:
        valid_cats: list[str] = []
        for raw_cat in categories:
            cat = raw_cat.strip()
            if not cat:
                continue
            if not _CATEGORY_RE.match(cat):
                raise ArxivValidationError(f"Некорректная категория arXiv: {raw_cat!r}")
            valid_cats.append(f"cat:{cat}")
        if valid_cats:
            cat_expr = " OR ".join(valid_cats)
            parts.append(f"({cat_expr})" if len(valid_cats) > 1 else cat_expr)

    if date_from or date_to:
        start_date = _parse_iso_date(date_from, "date_from") if date_from else date(1991, 1, 1)
        end_date = _parse_iso_date(date_to, "date_to") if date_to else date.today()
        if start_date > end_date:
            raise ArxivValidationError(
                f"date_from ({start_date.isoformat()}) не может быть позже date_to ({end_date.isoformat()})"
            )
        start_stamp = start_date.strftime("%Y%m%d") + "0000"
        end_stamp = end_date.strftime("%Y%m%d") + "2359"
        parts.append(f"submittedDate:[{start_stamp} TO {end_stamp}]")

    return " AND ".join(parts)


def _clean_xml_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def parse_atom_feed(xml_text: str) -> list[ArxivPaperDetails]:
    """Парсит Atom XML от arXiv API в список `ArxivPaperDetails`."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ArxivNetworkError(f"Не удалось распарсить XML ответ arXiv: {exc}") from exc

    papers: list[ArxivPaperDetails] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        id_elem = entry.find("atom:id", _ATOM_NS)
        raw_entry_id = _clean_xml_text(id_elem.text if id_elem is not None else None)
        if not raw_entry_id:
            continue

        title_elem = entry.find("atom:title", _ATOM_NS)
        title = _clean_xml_text(title_elem.text if title_elem is not None else None)
        if title.lower() == "error":
            summary_elem = entry.find("atom:summary", _ATOM_NS)
            err_msg = _clean_xml_text(summary_elem.text if summary_elem is not None else None)
            raise ArxivNetworkError(f"Ошибка arXiv API: {err_msg or 'неизвестная ошибка'}")

        try:
            canonical_id, versioned_id = normalize_arxiv_id(raw_entry_id)
        except ArxivValidationError:
            continue

        version = versioned_id[len(canonical_id) :] or None
        summary_elem = entry.find("atom:summary", _ATOM_NS)
        abstract = _clean_xml_text(summary_elem.text if summary_elem is not None else None)
        short_summary = abstract if len(abstract) <= 600 else abstract[:597].rstrip() + "..."

        published_elem = entry.find("atom:published", _ATOM_NS)
        updated_elem = entry.find("atom:updated", _ATOM_NS)
        published = _clean_xml_text(published_elem.text if published_elem is not None else None)
        updated = _clean_xml_text(updated_elem.text if updated_elem is not None else None)

        authors = [
            _clean_xml_text(name_el.text)
            for author_el in entry.findall("atom:author", _ATOM_NS)
            if (name_el := author_el.find("atom:name", _ATOM_NS)) is not None
            and _clean_xml_text(name_el.text)
        ]

        primary_el = entry.find("arxiv:primary_category", _ATOM_NS)
        primary_category = primary_el.attrib.get("term") if primary_el is not None else None

        categories: list[str] = []
        for cat_el in entry.findall("atom:category", _ATOM_NS):
            term = cat_el.attrib.get("term")
            if term and term not in categories:
                categories.append(term)

        pdf_url = f"https://arxiv.org/pdf/{versioned_id}.pdf"
        abs_url = f"https://arxiv.org/abs/{versioned_id}"
        for link_el in entry.findall("atom:link", _ATOM_NS):
            href = link_el.attrib.get("href", "")
            link_title = link_el.attrib.get("title", "")
            rel = link_el.attrib.get("rel", "")
            if link_title == "pdf" and href:
                pdf_url = href
            elif rel == "alternate" and href:
                abs_url = href

        comment_el = entry.find("arxiv:comment", _ATOM_NS)
        journal_el = entry.find("arxiv:journal_ref", _ATOM_NS)
        doi_el = entry.find("arxiv:doi", _ATOM_NS)

        papers.append(
            ArxivPaperDetails(
                arxiv_id=canonical_id,
                version=version,
                title=title,
                authors=authors,
                published=published,
                updated=updated,
                primary_category=primary_category,
                categories=categories,
                summary=short_summary,
                abstract=abstract,
                abs_url=abs_url,
                pdf_url=pdf_url,
                comment=_clean_xml_text(comment_el.text if comment_el is not None else None) or None,
                journal_ref=_clean_xml_text(journal_el.text if journal_el is not None else None) or None,
                doi=_clean_xml_text(doi_el.text if doi_el is not None else None) or None,
            )
        )
    return papers


def deduplicate_papers(papers: list[ArxivPaperDetails], limit: int) -> list[ArxivPaperDetails]:
    """Удаляет дубликаты по базовому `arxiv_id` с сохранением исходного порядка."""
    unique: list[ArxivPaperDetails] = []
    seen: set[str] = set()
    for paper in papers:
        if paper.arxiv_id in seen:
            continue
        seen.add(paper.arxiv_id)
        unique.append(paper)
        if len(unique) >= limit:
            break
    return unique


class ArxivApiClient:
    """Асинхронный клиент к arXiv API с rate-limiting, ретраями и кэшированием PDF."""

    def __init__(
        self,
        config: ArxivConfig,
        papers_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.papers_dir = papers_dir
        self._transport = transport
        self._lock = asyncio.Lock()
        self._last_request_ts: float = 0.0

    async def _throttle(self) -> None:
        if self.config.rate_limit_seconds <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_ts
            wait_for = self.config.rate_limit_seconds - elapsed
            if self._last_request_ts > 0 and wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_ts = time.monotonic()

    async def _request_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        headers = {"User-Agent": self.config.user_agent}
        timeout = httpx.Timeout(self.config.timeout_seconds)
        last_error: Exception | None = None

        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            for attempt in range(self.config.max_retries + 1):
                await self._throttle()
                try:
                    response = await client.get(url, params=params)
                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        raise ArxivNetworkError(
                            f"Временная ошибка HTTP {response.status_code} от {url}"
                        )
                    response.raise_for_status()
                    return response.content
                except (httpx.TimeoutException, httpx.TransportError, ArxivNetworkError) as exc:
                    last_error = exc
                    if attempt >= self.config.max_retries:
                        break
                    backoff = min(2.0**attempt, 8.0)
                    logger.warning(
                        "Повтор запроса к %s (попытка %d/%d) после ошибки: %s",
                        url,
                        attempt + 1,
                        self.config.max_retries,
                        exc,
                    )
                    await asyncio.sleep(backoff)
                except httpx.HTTPStatusError as exc:
                    raise ArxivNetworkError(
                        f"HTTP ошибка {exc.response.status_code} при обращении к {url}"
                    ) from exc

        raise ArxivNetworkError(f"Запрос к {url} не удался после повторов: {last_error}")

    async def search_papers(
        self,
        query: str,
        categories: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        requested = max_results if max_results is not None else self.config.default_max_results
        if requested < 1:
            raise ArxivValidationError("Параметр max_results должен быть >= 1")
        effective_limit = min(requested, self.config.max_results_limit)

        search_query = build_search_query(
            query=query,
            categories=categories,
            date_from=date_from,
            date_to=date_to,
        )
        params = {
            "search_query": search_query,
            "start": 0,
            "max_results": effective_limit,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        raw_bytes = await self._request_bytes(self.config.api_url, params=params)
        parsed = parse_atom_feed(raw_bytes.decode("utf-8", errors="replace"))
        deduped = deduplicate_papers(parsed, limit=effective_limit)
        summary_fields = set(ArxivPaperSummary.model_fields)
        papers_payload = [item.model_dump(include=summary_fields) for item in deduped]
        return {
            "query": search_query,
            "count": len(papers_payload),
            "papers": papers_payload,
        }

    async def get_paper_details(self, paper_id: str) -> dict[str, Any]:
        _, versioned_id = normalize_arxiv_id(paper_id)
        params = {"id_list": versioned_id, "max_results": 1}
        raw_bytes = await self._request_bytes(self.config.api_url, params=params)
        papers = parse_atom_feed(raw_bytes.decode("utf-8", errors="replace"))
        if not papers:
            raise ArxivValidationError(f"Публикация с arXiv ID {versioned_id!r} не найдена")
        return papers[0].model_dump()

    async def download_paper(self, paper_id: str) -> dict[str, Any]:
        canonical_id, versioned_id = normalize_arxiv_id(paper_id)
        safe_filename = versioned_id.replace("/", "_") + ".pdf"
        self.papers_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.papers_dir / safe_filename

        if target_path.is_file() and target_path.stat().st_size > 4:
            header = target_path.read_bytes()[:4]
            if header == b"%PDF":
                return {
                    "arxiv_id": canonical_id,
                    "versioned_id": versioned_id,
                    "file_path": str(target_path),
                    "size_bytes": target_path.stat().st_size,
                    "cached": True,
                }

        pdf_url = f"{self.config.pdf_base_url.rstrip('/')}/{versioned_id}.pdf"
        content = await self._request_bytes(pdf_url)
        if not content.startswith(b"%PDF"):
            raise ArxivNetworkError(
                f"Ответ от {pdf_url} не является валидным PDF-документом"
            )

        part_path = target_path.with_suffix(".pdf.part")
        part_path.write_bytes(content)
        part_path.replace(target_path)

        return {
            "arxiv_id": canonical_id,
            "versioned_id": versioned_id,
            "file_path": str(target_path),
            "size_bytes": len(content),
            "cached": False,
        }


def create_arxiv_mcp_server(
    config: Config | None = None,
    prompts: PromptsConfig | None = None,
    api_client: ArxivApiClient | None = None,
) -> FastMCP:
    """Создаёт и конфигурирует экземпляр `FastMCP` с тремя инструментами arXiv."""
    cfg = config or load_config()
    prm = prompts or load_prompts()
    client = api_client or ArxivApiClient(cfg.arxiv, cfg.papers_dir)

    mcp = FastMCP("arxiv-mcp-server")

    @mcp.tool(name="arxiv_search", description=prm.tool_description("arxiv_search"))
    async def arxiv_search(
        query: str,
        categories: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        try:
            return await client.search_papers(
                query=query,
                categories=categories,
                date_from=date_from,
                date_to=date_to,
                max_results=max_results,
            )
        except (ArxivValidationError, ArxivNetworkError) as exc:
            return {"error": str(exc), "papers": [], "count": 0}

    @mcp.tool(name="arxiv_get_details", description=prm.tool_description("arxiv_get_details"))
    async def arxiv_get_details(paper_id: str) -> dict[str, Any]:
        try:
            return await client.get_paper_details(paper_id=paper_id)
        except (ArxivValidationError, ArxivNetworkError) as exc:
            return {"error": str(exc), "paper_id": paper_id}

    @mcp.tool(name="arxiv_download_paper", description=prm.tool_description("arxiv_download_paper"))
    async def arxiv_download_paper(paper_id: str) -> dict[str, Any]:
        try:
            return await client.download_paper(paper_id=paper_id)
        except (ArxivValidationError, ArxivNetworkError) as exc:
            return {"error": str(exc), "paper_id": paper_id}

    return mcp


def main() -> None:
    server = create_arxiv_mcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
