"""Exercise the public error contract without database or provider requests."""

import unittest

import httpx
import openai
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from google.auth.exceptions import TransportError as GoogleTransportError
from pydantic import BaseModel, Field

from app.core.http_errors import (
    APIErrorMiddleware,
    AppHTTPException,
    install_exception_handlers,
    stream_error_event,
)


class Input(BaseModel):
    value: str = Field(min_length=2)


class HTTPErrorTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        install_exception_handlers(self.app)
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @self.app.post("/validate")
        def validate(body: Input):
            return body

        self.error = None

        @self.app.get("/error")
        def error():
            raise self.error

        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def fetch_error(self, error):
        self.error = error
        return self.client.get("/error", headers={"Origin": "http://localhost:5173"})

    def test_http_errors_preserve_detail_domain_code_and_headers(self):
        response = self.fetch_error(AppHTTPException(
            status_code=409,
            detail="资源状态冲突",
            code="REVIEW_REQUIRED",
            headers={"Retry-After": "2"},
        ))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "资源状态冲突", "code": "REVIEW_REQUIRED"})
        self.assertEqual(response.headers["retry-after"], "2")

    def test_framework_404_and_405_keep_status_and_allow_header(self):
        missing = self.client.get("/missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "NOT_FOUND")
        wrong_method = self.client.post("/error")
        self.assertEqual(wrong_method.status_code, 405)
        self.assertIn("GET", wrong_method.headers["allow"])
        self.assertEqual(wrong_method.json()["code"], "METHOD_NOT_ALLOWED")

    def test_401_supplies_bearer_challenge(self):
        response = self.fetch_error(HTTPException(status_code=401, detail="登录已过期"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(response.json()["code"], "UNAUTHORIZED")

    def test_validation_keeps_detail_array_and_422(self):
        response = self.client.post("/validate", json={"value": "x"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "VALIDATION_ERROR")
        self.assertIsInstance(response.json()["detail"], list)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "value"])

    def test_unexpected_error_is_json_with_cors_and_no_internal_details(self):
        with self.assertLogs("app.core.http_errors", level="ERROR"):
            response = self.fetch_error(RuntimeError("private database credentials"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertNotIn("private database credentials", response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
        self.assertEqual(response.headers["access-control-allow-credentials"], "true")

    def test_upstream_errors_are_classified_without_provider_requests(self):
        request = httpx.Request("POST", "https://provider.invalid/chat")
        cases = [
            (openai.APITimeoutError(request=request), 504, "UPSTREAM_TIMEOUT"),
            (openai.APIConnectionError(request=request), 503, "SERVICE_UNAVAILABLE"),
            (httpx.ReadTimeout("timeout", request=request), 504, "UPSTREAM_TIMEOUT"),
            (httpx.ConnectError("offline", request=request), 503, "SERVICE_UNAVAILABLE"),
            (requests.Timeout("timeout"), 504, "UPSTREAM_TIMEOUT"),
            (GoogleTransportError("certificate service unavailable"), 503, "SERVICE_UNAVAILABLE"),
        ]
        for status in (401, 403, 400, 500, 429, 503, 504):
            upstream_response = httpx.Response(status, request=request)
            mapped_status = 503 if status in (429, 503) else 504 if status == 504 else 502
            mapped_code = {502: "UPSTREAM_ERROR", 503: "SERVICE_UNAVAILABLE", 504: "UPSTREAM_TIMEOUT"}[mapped_status]
            cases.append((openai.APIStatusError("private provider error", response=upstream_response, body=None), mapped_status, mapped_code))
        for error, status, code in cases:
            with self.subTest(error=type(error).__name__, status=status):
                with self.assertLogs("app.core.http_errors", level="ERROR"):
                    response = self.fetch_error(error)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
                self.assertNotIn("private provider error", response.text)
                self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
                if status != 401:
                    self.assertNotIn("www-authenticate", response.headers)

    def test_ndjson_uses_the_same_error_codes_as_http(self):
        event = stream_error_event(AppHTTPException(status_code=409, detail="需要重新审核", code="REVIEW_REQUIRED"))
        self.assertEqual(event, {"type": "error", "status": 409, "detail": "需要重新审核", "code": "REVIEW_REQUIRED"})
        with self.assertLogs("app.core.http_errors", level="ERROR"):
            timeout = stream_error_event(openai.APITimeoutError(request=httpx.Request("POST", "https://provider.invalid")))
        self.assertEqual(timeout["status"], 504)
        self.assertEqual(timeout["code"], "UPSTREAM_TIMEOUT")


class StartedStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_response_is_not_replaced_with_a_second_status(self):
        sent = []

        async def started_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RuntimeError("stream already started")

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            sent.append(message)

        with self.assertRaisesRegex(RuntimeError, "stream already started"):
            await APIErrorMiddleware(started_app)({"type": "http"}, receive, send)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], 200)


if __name__ == "__main__":
    unittest.main()
