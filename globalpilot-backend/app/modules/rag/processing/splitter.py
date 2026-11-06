import asyncio
import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.modules.rag.processing.document_processing import submit_text_split

TEXT_SPLIT_PROCESS_THRESHOLD_BYTES = 64 * 1024

# 超长段落再按句子切开，短段落保持整段，避免再切成几十个字的碎片。
_overflow_splitter = RecursiveCharacterTextSplitter(
    chunk_size=settings.rag_chunk_size,
    chunk_overlap=settings.rag_chunk_overlap,
    separators=["\n", "。", "！", "？", "；", ".", "!", "?", " ", ""],
)


def split_text(text: str) -> list[str]:
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]

    if not paragraphs:
        stripped = text.strip()
        return [stripped] if stripped else []

    chunks: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= settings.rag_chunk_size:
            chunks.append(paragraph)
            continue
        chunks.extend(_overflow_splitter.split_text(paragraph))
    return chunks


def split_to_documents(
    text: str,
    metadata: dict,
) -> list[Document]:
    return _documents_from_chunks(split_text(text), metadata)


def _documents_from_chunks(chunks: list[str], metadata: dict) -> list[Document]:
    return [
        Document(
            page_content=chunk,
            metadata={
                **metadata,
                "chunk_index": index,
            },
        )
        for index, chunk in enumerate(chunks)
    ]


async def asplit_to_documents(text: str, metadata: dict) -> list[Document]:
    # len(str) avoids encoding an entire large document just to choose the path;
    # any UTF-8 string needs at least one byte per character.
    large = len(text) >= TEXT_SPLIT_PROCESS_THRESHOLD_BYTES or (
        len(text.encode("utf-8", errors="surrogatepass")) >= TEXT_SPLIT_PROCESS_THRESHOLD_BYTES
    )
    if not large:
        return split_to_documents(text, metadata)
    future = await run_in_threadpool(
        submit_text_split, text, settings.rag_chunk_size, settings.rag_chunk_overlap
    )
    try:
        chunks = await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.cancel()
        raise
    # Documents and metadata stay in the parent process; wrapping a very large
    # chunk list is also kept off its event loop.
    return await run_in_threadpool(_documents_from_chunks, chunks, metadata)
