"""One error contract for ordinary HTTP responses and NDJSON error events."""

import logging
from typing import Any

import httpx
import openai
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google.auth.exceptions import TransportError as GoogleTransportError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.modules.marketing.workflow.research_validation import ResearchReportError

logger = logging.getLogger(__name__)

STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "CONTENT_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "UPSTREAM_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}


class AppHTTPException(HTTPException):
    def __init__(
        self,
        status_code: int,
        detail: Any,
        code: str,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def error_payload(exc: StarletteHTTPException) -> dict[str, Any]:
    return {
        "detail": exc.detail,
        "code": getattr(exc, "code", STATUS_CODES.get(exc.status_code, "HTTP_ERROR")),
    }


def _upstream_status(status_code: int) -> AppHTTPException:
    if status_code == 429 or status_code == 503:
        return AppHTTPException(
            status_code=503,
            detail="上游服务暂时不可用，请稍后重试",
            code="SERVICE_UNAVAILABLE",
        )
    if status_code in (408, 504):
        return AppHTTPException(
            status_code=504, detail="上游服务响应超时，请稍后重试", code="UPSTREAM_TIMEOUT"
        )
    # An upstream 401/403 concerns our provider credentials, not the user's login.
    return AppHTTPException(
        status_code=502, detail="上游服务请求失败，请稍后重试", code="UPSTREAM_ERROR"
    )


def classify_upstream_error(exc: Exception) -> AppHTTPException | None:
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException, requests.Timeout)):
        return AppHTTPException(
            status_code=504, detail="上游服务响应超时，请稍后重试", code="UPSTREAM_TIMEOUT"
        )
    if isinstance(exc, openai.APIStatusError):
        return _upstream_status(exc.status_code)
    if isinstance(exc, httpx.HTTPStatusError):
        return _upstream_status(exc.response.status_code)
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return _upstream_status(exc.response.status_code)
    if isinstance(exc, (
        openai.APIConnectionError,
        httpx.TransportError,
        requests.ConnectionError,
        GoogleTransportError,
    )):
        return AppHTTPException(
            status_code=503,
            detail="上游服务暂时不可用，请稍后重试",
            code="SERVICE_UNAVAILABLE",
        )
    if isinstance(exc, (openai.APIError, httpx.HTTPError, requests.RequestException)):
        return AppHTTPException(
            status_code=502, detail="上游服务请求失败，请稍后重试", code="UPSTREAM_ERROR"
        )
    return None


def _public_error(exc: Exception) -> StarletteHTTPException:
    if isinstance(exc, StarletteHTTPException):
        return exc
    mapped = classify_upstream_error(exc)
    if isinstance(exc, ResearchReportError):
        # Research validation wraps provider errors at more than one boundary.
        # Preserve their status without exposing model output or request data.
        cause = exc.__cause__
        seen = {id(exc)}
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, Exception):
                mapped = classify_upstream_error(cause)
                if mapped is not None:
                    break
            cause = cause.__cause__
        mapped = mapped or AppHTTPException(
            status_code=502,
            detail="调研模型未返回有效的核对结果，请重试",
            code="RESEARCH_VALIDATION_FAILED",
        )
    logger.error("API operation failed", exc_info=(type(exc), exc, exc.__traceback__))
    return mapped or AppHTTPException(
        status_code=500, detail="服务器内部错误，请稍后重试", code="INTERNAL_ERROR"
    )


def stream_error_event(exc: Exception) -> dict[str, Any]:
    error = _public_error(exc)
    return {"type": "error", "status": error.status_code, **error_payload(error)}


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    headers = dict(exc.headers or {})
    if exc.status_code == 401 and not any(key.lower() == "www-authenticate" for key in headers):
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(error_payload(exc)),
        headers=headers,
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Field locations and messages are sufficient; do not echo passwords or tokens.
    details = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(details), "code": "VALIDATION_ERROR"},
    )


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    error = await run_in_threadpool(_public_error, exc)
    return await http_exception_handler(request, error)


class APIErrorMiddleware:
    """Handle uncaught errors before headers are sent, inside the CORS middleware.

    Starlette's generic Exception handler normally runs outside user middleware.
    Handling here lets the browser read structured 5xx responses across origins.
    Once a stream has started, its generator must emit its own error event.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def track_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, track_send)
        except Exception as exc:
            if started:
                raise
            response = await unexpected_exception_handler(Request(scope), exc)
            await response(scope, receive, send)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)
    # Install before adding CORS, so CORS wraps the error responses too.
    app.add_middleware(APIErrorMiddleware)
