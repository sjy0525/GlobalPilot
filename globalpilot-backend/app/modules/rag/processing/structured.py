"""Section-aware retrieval units with stable links to original document items."""

import copy
import math
import re

from langchain_core.documents import Document

from app.core.config import settings
from app.modules.rag.processing.splitter import _overflow_splitter


def text_structure(text: str) -> dict:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return {
        "schema_version": 1, "parser": "plain-text", "parser_version": "1",
        "page_count": 0, "pages": [],
        "contents": [
            {"content_id": f"text-{index:06d}", "kind": "text", "text": part,
             "markdown": part, "section_path": [], "page_no": None, "provenance": []}
            for index, part in enumerate(paragraphs)
        ],
    }


def validate_structure(structured: dict, assets: list[dict]) -> None:
    if structured.get("schema_version") != 1 or not isinstance(structured.get("contents"), list):
        raise ValueError("Invalid structured document schema")
    is_pdf = structured.get("parser") == "docling"
    page_count = structured.get("page_count")
    if is_pdf and (type(page_count) is not int or page_count < 1):
        raise ValueError("PDF page count is invalid")
    asset_ids = set()
    for asset in assets:
        identifier = asset.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", identifier) or identifier in asset_ids:
            raise ValueError("Invalid or duplicate document asset ID")
        if not isinstance(asset.get("data"), bytes) or not asset["data"]:
            raise ValueError("Document asset is empty")
        asset_ids.add(identifier)
    seen = set()
    for item in structured["contents"]:
        identifier = item.get("content_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", identifier) or identifier in seen:
            raise ValueError("Invalid or duplicate document content ID")
        seen.add(identifier)
        if item.get("kind") not in {"heading", "text", "table", "picture"}:
            raise ValueError("Invalid document content kind")
        if item.get("asset_id") and item["asset_id"] not in asset_ids:
            raise ValueError("Document picture asset is missing")
        if is_pdf:
            page_no = item.get("page_no")
            if type(page_no) is not int or not 1 <= page_no <= page_count:
                raise ValueError("PDF content must have an original page number")
            if not item.get("provenance"):
                raise ValueError("PDF content must retain its original location")
        for location in item.get("provenance", []):
            page_no = location.get("page_no")
            if is_pdf and (type(page_no) is not int or not 1 <= page_no <= page_count):
                raise ValueError("PDF provenance page number is invalid")
            if location.get("bbox") and location.get("coord_origin") not in {"TOPLEFT", "BOTTOMLEFT"}:
                raise ValueError("A bounding box must identify its coordinate origin")
            if location.get("bbox"):
                values = [location["bbox"].get(key) for key in ("l", "t", "r", "b")]
                dimensions = [location.get("page_width"), location.get("page_height")]
                if any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in values + dimensions) or any(value <= 0 for value in dimensions):
                    raise ValueError("PDF coordinates and page dimensions must be finite and valid")
    for item in structured["contents"]:
        if any(identifier not in seen for identifier in item.get("related_content_ids", [])):
            raise ValueError("Related table/image context must link to original content")


def _table_parts(markdown: str) -> list[str]:
    """Keep rows intact and repeat the header, never character-split table cells."""
    lines = markdown.strip().splitlines()
    start = next((index for index, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    if start is None or len(lines) - start < 3:
        return [markdown] if markdown.strip() else []
    # Docling can prepend a table title/units before its pipe-table grid.
    # Retain that original preamble with each complete group of rows.
    header = lines[:start + 2]
    parts, rows = [], []
    for row in lines[start + 2:]:
        if rows and len("\n".join(header + rows + [row])) > settings.rag_chunk_size:
            parts.append("\n".join(header + rows))
            rows = []
        rows.append(row)
    if rows:
        parts.append("\n".join(header + rows))
    return parts


def structured_documents(structured: dict, metadata: dict) -> tuple[dict, list[Document]]:
    """Never cross section/page boundaries; tables and pictures are independent units."""
    result = copy.deepcopy(structured)
    by_id = {item["content_id"]: item for item in result["contents"]}
    units: list[tuple[str, list[dict]]] = []
    pending: list[dict] = []
    pending_text = ""

    def flush():
        nonlocal pending, pending_text
        if pending_text.strip():
            units.append((pending_text.strip(), pending))
        pending, pending_text = [], ""

    for item in result["contents"]:
        if result.get("parser") == "plain-text":
            # Preserve the existing TXT paragraph boundaries; PDF paragraphs
            # may be grouped within an explicitly detected section and page.
            flush()
        kind = item["kind"]
        if kind == "picture" and item.get("retrieval_ready") is False:
            flush()
            continue
        text = (item.get("markdown") if kind == "table" else item.get("text")) or ""
        if kind == "picture":
            # Keep image OCR/captions beside generated descriptions: descriptions
            # alone can omit exactly the labels/numbers a user needs to retrieve.
            text = text or item.get("description") or ""
        text = text.strip()
        if not text:
            continue
        group = (item.get("section_path", []), item.get("page_no"))
        previous = (pending[0].get("section_path", []), pending[0].get("page_no")) if pending else None
        if kind in {"table", "picture"}:
            flush()
            parts = _table_parts(text) if kind == "table" else _overflow_splitter.split_text(text)
            related = [by_id[identifier] for identifier in item.get("related_content_ids", [])
                       if identifier in by_id and identifier != item["content_id"]]
            units.extend((part, [item, *related]) for part in parts)
        else:
            if previous != group or len(pending_text) + len(text) + 2 > settings.rag_chunk_size:
                flush()
            if len(text) > settings.rag_chunk_size:
                flush()
                units.extend((part, [item]) for part in _overflow_splitter.split_text(text))
            else:
                pending.append(item)
                pending_text += ("\n\n" if pending_text else "") + text
    flush()

    documents = []
    canonical_units = []
    for index, (text, items) in enumerate(units):
        ids = list(dict.fromkeys(item["content_id"] for item in items))
        pages = sorted({p["page_no"] for item in items for p in item.get("provenance", [])
                        if type(p.get("page_no")) is int}
                       | {item["page_no"] for item in items if type(item.get("page_no")) is int})
        unit_metadata = {
            **metadata, "chunk_index": index,
            "content_id": ids[0], "content_ids": ids,
            "kind": items[0]["kind"] if items[0]["kind"] in {"table", "picture"}
                    or len({i["kind"] for i in items}) == 1 else "text",
            "section_path": items[0].get("section_path", []),
            "page_no": pages[0] if pages else None, "page_numbers": pages,
            "provenance": [p for item in items for p in item.get("provenance", [])],
            "caption": "\n".join(dict.fromkeys(item["caption"] for item in items if item.get("caption"))),
            "context_text": "\n".join(dict.fromkeys(item["context_text"] for item in items if item.get("context_text"))),
        }
        # This canonical copy is read back after vector retrieval. The vector DB
        # provides recall, not the authoritative evidence or source locations.
        canonical_units.append({"text": text, "metadata": unit_metadata})
        documents.append(Document(page_content=text, metadata=unit_metadata))
    result["retrieval_units"] = canonical_units
    return result, documents
