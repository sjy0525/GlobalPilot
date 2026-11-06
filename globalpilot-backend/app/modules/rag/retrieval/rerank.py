import logging
from threading import Lock

import httpx
from starlette.concurrency import run_in_threadpool

from app.core.config import settings

logger = logging.getLogger(__name__)
_client_lock = Lock()
_async_client: httpx.AsyncClient | None = None


def initialize_async_rerank_client() -> httpx.AsyncClient:
    """Prepare the client's TLS context in the startup worker without HTTP requests."""
    global _async_client
    with _client_lock:
        if _async_client is None:
            _async_client = httpx.AsyncClient(timeout=30.0)
        return _async_client


def _take_async_rerank_client() -> httpx.AsyncClient | None:
    global _async_client
    with _client_lock:
        client, _async_client = _async_client, None
        return client


async def aclose_rerank_client() -> None:
    client = await run_in_threadpool(_take_async_rerank_client)
    if client is not None:
        await client.aclose()


def _rerank_url() -> str:
    base = settings.dashscope_rerank_base_url.rstrip("/")
    if base.endswith("/reranks"):
        return base
    return f"{base}/reranks"


def _parse_results(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if isinstance(results, list):
        return results

    output = payload.get("output")
    if isinstance(output, dict) and isinstance(output.get("results"), list):
        return output["results"]
    return []


def _rerank_request(query: str, candidates: list[tuple], top_n: int) -> dict:
    return {
        "headers": {
            "Authorization": f"Bearer {settings.dashscope_rerank_api_key}",
            "Content-Type": "application/json",
        },
        "json": {
            "model": settings.dashscope_rerank_model,
            "query": query,
            "documents": [doc.page_content for doc, _distance in candidates],
            "top_n": top_n,
        },
        "timeout": 30.0,
    }


def _rank_candidates(payload, candidates: list[tuple], top_n: int) -> list[tuple]:
    ranked: list[tuple] = []
    seen: set[int] = set()
    for item in _parse_results(payload):
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if type(index) is not int or index < 0 or index >= len(candidates) or index in seen:
            continue
        seen.add(index)
        ranked.append(candidates[index])
        if len(ranked) >= top_n:
            break
    return ranked[:top_n]


def rerank_documents(
    query: str,
    candidates: list[tuple],
    top_n: int,
) -> list[tuple]:
    if not candidates or top_n <= 0:
        return []
    if len(candidates) <= 1:
        return candidates[:top_n]

    try:
        response = httpx.post(_rerank_url(), **_rerank_request(query, candidates, top_n))
        response.raise_for_status()
        ranked = _rank_candidates(response.json(), candidates, top_n)
    except (httpx.HTTPError, ValueError, TypeError):
        logger.warning("rerank failed, falling back to vector order", exc_info=True)
        return candidates[:top_n]

    if not ranked:
        logger.warning("rerank returned no usable results, falling back to vector order")
        return candidates[:top_n]
    return ranked[:top_n]


async def arerank_documents(query: str, candidates: list[tuple], top_n: int) -> list[tuple]:
    if not candidates or top_n <= 0:
        return []
    if len(candidates) <= 1:
        return candidates[:top_n]
    # Startup normally prepares the client. Lazy callers also keep TLS setup off-loop.
    client = _async_client or await run_in_threadpool(initialize_async_rerank_client)
    try:
        response = await client.post(_rerank_url(), **_rerank_request(query, candidates, top_n))
        response.raise_for_status()
        ranked = _rank_candidates(response.json(), candidates, top_n)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        await run_in_threadpool(
            logger.warning,
            "rerank failed, falling back to vector order",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return candidates[:top_n]
    if not ranked:
        await run_in_threadpool(logger.warning, "rerank returned no usable results, falling back to vector order")
        return candidates[:top_n]
    return ranked
