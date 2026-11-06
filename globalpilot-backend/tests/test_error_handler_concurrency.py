"""A blocked error-log sink must not block unrelated async HTTP requests."""

import asyncio
import logging
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.core import http_errors


class BlockingLogStream:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.write_threads = []
        self.flush_threads = []
        self.release_timed_out = False

    def write(self, message):
        self.write_threads.append(threading.get_ident())
        self.entered.set()
        # A bounded guard also lets the test fail cleanly if a regression calls
        # this blocking sink on the event-loop thread.
        if not self.release.wait(timeout=3):
            self.release_timed_out = True

    def flush(self):
        self.flush_threads.append(threading.get_ident())


class RecordingStreamHandler(logging.StreamHandler):
    def __init__(self, stream):
        super().__init__(stream)
        self.records = []

    def emit(self, record):
        self.records.append(record)
        super().emit(record)


class ErrorHandlerConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_error_logging_allows_ping_and_emits_one_error_response(self):
        app = FastAPI()
        http_errors.install_exception_handlers(app)
        origin = "http://localhost:5173"
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[origin],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/error")
        async def error():
            raise RuntimeError("private error details")

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        @app.get("/protected")
        async def protected():
            raise HTTPException(
                status_code=401, detail="请登录", headers={"WWW-Authenticate": "Bearer"}
            )

        stream = BlockingLogStream()
        handler = RecordingStreamHandler(stream)
        event_loop_thread = threading.get_ident()
        transport = httpx.ASGITransport(app=app)
        with patch.object(http_errors.logger, "handlers", [handler]), \
             patch.object(http_errors.logger, "propagate", False), \
             patch.object(http_errors.logger, "level", logging.ERROR):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                pending_error = asyncio.create_task(client.get("/error", headers={"Origin": origin}))
                try:
                    started = await asyncio.to_thread(stream.entered.wait, 1)
                    self.assertTrue(started, "the error request never reached the logging sink")
                    self.assertFalse(pending_error.done(), "error response finished before logging was released")
                    response = await asyncio.wait_for(client.get("/ping"), timeout=1)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"ok": True})
                    self.assertFalse(pending_error.done(), "ping must finish while error logging is blocked")
                finally:
                    stream.release.set()
                    error_response = await pending_error

                self.assertEqual(error_response.status_code, 500)
                self.assertEqual(error_response.json(), {
                    "detail": "服务器内部错误，请稍后重试", "code": "INTERNAL_ERROR"
                })
                self.assertNotIn("private error details", error_response.text)
                self.assertEqual(error_response.headers.get_list("access-control-allow-origin"), [origin])
                self.assertEqual(error_response.headers.get_list("access-control-allow-credentials"), ["true"])
                self.assertEqual(error_response.headers.get_list("content-type"), ["application/json"])
                self.assertEqual(error_response.headers.get_list("content-length"), [str(len(error_response.content))])

                protected_response = await client.get("/protected", headers={"Origin": origin})
                self.assertEqual(protected_response.status_code, 401)
                self.assertEqual(protected_response.json(), {"detail": "请登录", "code": "UNAUTHORIZED"})
                self.assertEqual(protected_response.headers.get_list("www-authenticate"), ["Bearer"])

        self.assertFalse(stream.release_timed_out)
        self.assertEqual(len(handler.records), 1)
        self.assertEqual(handler.records[0].getMessage(), "API operation failed")
        self.assertEqual(len(stream.write_threads), 1)
        self.assertEqual(len(stream.flush_threads), 1)
        self.assertEqual(stream.write_threads, stream.flush_threads)
        self.assertNotEqual(stream.write_threads[0], event_loop_thread)


if __name__ == "__main__":
    unittest.main()
