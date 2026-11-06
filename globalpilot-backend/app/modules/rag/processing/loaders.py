import asyncio
from pathlib import Path

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.http_errors import AppHTTPException
from app.modules.rag.processing.document_processing import submit_pdf
from app.modules.rag.processing.pdf_worker import parse_pdf

ALLOWED_EXTENSIONS = {".txt", ".pdf"}


def media_type_for(filename: str) -> str:
    if Path(filename).suffix.lower() == ".pdf":
        return "application/pdf"
    return "text/plain; charset=utf-8"


def load_upload(filename: str, content: bytes) -> tuple[str, str]:
    name, suffix = _upload_name(filename)
    if suffix == ".pdf":
        return name, _load_pdf(content)
    return name, _load_txt(content)


def _upload_name(filename: str) -> tuple[str, str]:
    name = filename or "untitled"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise AppHTTPException(
            status_code=415,
            detail="目前仅支持 txt 和 pdf 文件",
            code="UNSUPPORTED_MEDIA_TYPE",
        )
    return name, suffix


async def aload_upload(filename: str, content: bytes) -> tuple[str, str]:
    parsed = await aload_document_upload(filename, content)
    return parsed["source"], parsed["text"]


async def aload_document_upload(filename: str, content: bytes) -> dict:
    """Retain structure and image assets instead of discarding them after OCR."""
    from app.modules.rag.processing.structured import text_structure

    name, suffix = _upload_name(filename)
    if suffix != ".pdf":
        text = _load_txt(content)
        return {"source": name, "text": text, "structured": text_structure(text), "assets": []}
    if not content:
        raise AppHTTPException(status_code=400, detail="PDF 文件不能为空", code="INVALID_PDF")
    future = await run_in_threadpool(submit_pdf, content)
    try:
        result = await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        # cancel() only stops queued work; running work remains owned by the
        # bounded shared pool and is joined during lifecycle shutdown.
        future.cancel()
        raise
    text = _pdf_text(result)
    return {"source": name, "text": text, "structured": result.get("structured"),
            "assets": result.get("assets", [])}


def _load_txt(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="文件必须是utf-8编码",
        )


def _load_pdf(content: bytes) -> str:
    return _pdf_text(parse_pdf(content))


def _pdf_text(result: dict) -> str:
    if not result["ok"]:
        raise AppHTTPException(
            status_code=result.get("status_code", 400), detail=result["detail"],
            code=result.get("code", "INVALID_PDF")
        )
    return result["text"]
