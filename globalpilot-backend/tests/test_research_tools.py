"""Exercise the real Baidu tool with an isolated config and offline HTTP transport."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr


TEST_KEY = "offline-only-key-never-send"
TEST_URL = "https://search.example.test/web_search"


class ResearchToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Loading only this source avoids importing config, .env, or the real LLM.
        settings = types.SimpleNamespace(
            baidu_search_api_key=SecretStr(TEST_KEY),
            baidu_search_url=TEST_URL,
        )
        config = types.ModuleType("app.core.config")
        config.settings = settings
        source = Path(__file__).resolve().parents[1] / "app/modules/marketing/workflow/research_tools.py"
        spec = importlib.util.spec_from_file_location("research_tools_under_test", source)
        cls.tools = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"app.core.config": config}):
            spec.loader.exec_module(cls.tools)

    @contextmanager
    def mock_http(self, handler):
        # Every client request is intercepted; the real timeout/request code runs.
        real_client = httpx.Client
        transport = httpx.MockTransport(handler)
        with patch.object(
            self.tools.httpx,
            "Client",
            side_effect=lambda *args, **kwargs: real_client(
                *args, transport=transport, **kwargs
            ),
        ):
            yield

    def assert_safe_error(self, handler, expected_text):
        with self.mock_http(handler):
            with self.assertRaises(self.tools.ResearchSearchError) as caught:
                self.tools.search_web.invoke({"query": "library charger"})
        self.assertIn(expected_text, str(caught.exception))
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_request_contract_and_result_filtering(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"references": [
                {"title": "Library", "url": "https://university.example.test/charging",
                 "content": "x" * 5000, "date": "2026-10-01"},
                {"url": "http://university.example.test/borrow", "content": "Borrow chargers."},
                {"url": "file:///private/example", "content": "Invalid protocol"},
                {"url": "https://empty.example.test", "content": "  "},
                {"url": "https://invalid.example.test", "content": 123},
                {"content": "Missing link"},
                "malformed item",
            ]})

        with self.mock_http(handler):
            result = self.tools.search_web.invoke({"query": "  library charger  "})

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), TEST_URL)
        self.assertEqual(request.headers["Authorization"], f"Bearer {TEST_KEY}")
        self.assertEqual(json.loads(request.content), {
            "messages": [{"role": "user", "content": "library charger"}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": 3}],
        })
        self.assertEqual(request.extensions["timeout"], {
            "connect": 5.0, "read": 20.0, "write": 20.0, "pool": 20.0,
        })
        self.assertEqual(result["query"], "library charger")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0], {
            "title": "Library", "url": "https://university.example.test/charging",
            "content": "x" * 4000, "date": "2026-10-01",
        })
        self.assertEqual(result["results"][1]["title"], "")

    def test_empty_query_is_rejected_before_network(self):
        with patch.object(self.tools.httpx, "Client") as client:
            with self.assertRaisesRegex(ValueError, "搜索词不能为空"):
                self.tools.search_web.invoke({"query": " \n "})
        client.assert_not_called()

    def test_missing_key_is_rejected_before_network(self):
        with patch.object(self.tools.settings, "baidu_search_api_key", SecretStr("")):
            with patch.object(self.tools.httpx, "Client") as client:
                with self.assertRaisesRegex(self.tools.ResearchSearchError, "尚未配置"):
                    self.tools.search_web.invoke({"query": "library charger"})
        client.assert_not_called()

    def test_timeout_has_sanitized_public_error(self):
        def handler(request):
            raise httpx.ReadTimeout(f"provider error contains {TEST_KEY}", request=request)

        self.assert_safe_error(handler, "请求超时")

    def test_http_error_has_sanitized_public_error(self):
        self.assert_safe_error(
            lambda request: httpx.Response(401, text=TEST_KEY), "请求失败"
        )

    def test_transport_error_has_sanitized_public_error(self):
        def handler(request):
            raise httpx.ConnectError(f"connection failure {TEST_KEY}", request=request)

        self.assert_safe_error(handler, "请求失败")

    def test_invalid_json_has_sanitized_public_error(self):
        self.assert_safe_error(
            lambda request: httpx.Response(200, text=f"invalid JSON {TEST_KEY}"),
            "不是有效 JSON",
        )

    def test_invalid_response_shape_has_sanitized_public_error(self):
        for payload in ({"message": TEST_KEY}, {"references": TEST_KEY}, []):
            with self.subTest(payload_type=type(payload).__name__):
                self.assert_safe_error(
                    lambda request: httpx.Response(200, json=payload), "缺少结果列表"
                )

    def test_empty_references_are_a_successful_empty_result(self):
        with self.mock_http(lambda request: httpx.Response(200, json={"references": []})):
            result = self.tools.search_web.invoke({"query": "no results"})
        self.assertEqual(result, {
            "query": "no results", "ok": True, "results": [], "error": None,
        })


if __name__ == "__main__":
    unittest.main()
