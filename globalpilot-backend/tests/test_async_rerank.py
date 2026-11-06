"""Native AsyncClient reranking and sync/background compatibility without network."""
import asyncio
import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx


class AsyncRerankTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = types.ModuleType("app.core.config")
        config.settings = types.SimpleNamespace(dashscope_rerank_base_url="https://example.invalid/v1",
                                                dashscope_rerank_api_key="fake", dashscope_rerank_model="rerank-test")
        path = Path(__file__).resolve().parents[1] / "app/modules/rag/retrieval/rerank.py"
        spec = importlib.util.spec_from_file_location("_async_rerank_test", path)
        self.rerank = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"app.core.config": config}):
            spec.loader.exec_module(self.rerank)
        self.candidates = [(types.SimpleNamespace(page_content=f"Document {i}"), float(i)) for i in range(3)]
        self.client = Mock(post=AsyncMock(), aclose=AsyncMock())
        self.response = Mock(json=Mock(return_value={"results": [{"index": 2}, {"index": 0}]}))
        self.client.post.return_value = self.response
        self.rerank._async_client = self.client
        self.loop_thread = threading.get_ident()

    async def asyncTearDown(self):
        await self.rerank.aclose_rerank_client()

    async def test_reuses_client_and_awaits_native_post_and_close(self):
        with patch.object(httpx, "post", side_effect=AssertionError("Sync HTTP forbidden")):
            for _ in range(2):
                self.assertEqual(await self.rerank.arerank_documents("Blue", self.candidates, 2),
                                 [self.candidates[2], self.candidates[0]])
        self.assertEqual(self.client.post.await_count, 2)
        url = self.client.post.call_args.args[0]
        self.assertEqual(url, "https://example.invalid/v1/reranks")
        self.assertEqual(self.client.post.call_args.kwargs["json"]["documents"],
                         ["Document 0", "Document 1", "Document 2"])
        await self.rerank.aclose_rerank_client()
        await self.rerank.aclose_rerank_client()
        self.client.aclose.assert_awaited_once()

    async def test_real_async_client_transport_waits_without_blocking_heartbeat(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def transport(request):
            self.assertEqual(threading.get_ident(), self.loop_thread)
            self.assertEqual(request.url.path, "/v1/reranks")
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"results": [{"index": 1}]})

        client = await asyncio.to_thread(httpx.AsyncClient, transport=httpx.MockTransport(transport))
        self.rerank._async_client = client
        task = asyncio.create_task(self.rerank.arerank_documents("Blue", self.candidates, 1))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(3):
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            release.set()
            self.assertEqual(await task, [self.candidates[1]])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_sync_and_async_paths_share_flat_nested_and_invalid_result_parsing(self):
        for payload in (
            {"results": [{"index": 2}, {"index": 2}, {"index": True}, {"index": -1}, {"index": 0}]},
            {"output": {"results": [{"index": 2}, "bad", {"index": 0}]}},
        ):
            self.response.json.return_value = payload
            with patch.object(httpx, "post", return_value=self.response):
                sync = self.rerank.rerank_documents("Blue", self.candidates, 2)
            async_result = await self.rerank.arerank_documents("Blue", self.candidates, 2)
            self.assertEqual(sync, [self.candidates[2], self.candidates[0]])
            self.assertEqual(async_result, sync)

    async def test_upstream_failures_fall_back_and_log_in_a_worker(self):
        for failure in (httpx.ReadTimeout("timeout"), ValueError("invalid JSON"), TypeError("invalid payload")):
            self.client.post.side_effect = failure
            logs = []
            with patch.object(self.rerank.logger, "warning", side_effect=lambda *args, **kwargs:
                              logs.append((threading.get_ident(), kwargs))):
                self.assertEqual(await self.rerank.arerank_documents("Blue", self.candidates, 2), self.candidates[:2])
            self.assertEqual(len(logs), 1)
            self.assertNotEqual(logs[0][0], self.loop_thread)
            self.assertIs(logs[0][1]["exc_info"][1], failure)

    async def test_unusable_results_fall_back_and_log_in_a_worker(self):
        for payload in (None, [], {"results": [{"index": 99}, {"index": "1"}]}):
            self.response.json.return_value = payload
            logs = []
            with patch.object(self.rerank.logger, "warning", side_effect=lambda *args, **kwargs:
                              logs.append(threading.get_ident())):
                self.assertEqual(await self.rerank.arerank_documents("Blue", self.candidates, 2), self.candidates[:2])
            self.assertEqual(len(logs), 1)
            self.assertNotEqual(logs[0], self.loop_thread)

    async def test_empty_singleton_or_zero_limit_skip_http(self):
        self.assertEqual(await self.rerank.arerank_documents("Blue", [], 2), [])
        self.assertEqual(await self.rerank.arerank_documents("Blue", self.candidates[:1], 2), self.candidates[:1])
        self.assertEqual(await self.rerank.arerank_documents("Blue", self.candidates, 0), [])
        self.client.post.assert_not_awaited()

    async def test_lazy_tls_client_construction_runs_off_loop_and_is_reused(self):
        self.rerank._async_client = None
        constructed_in = []

        def construct(**kwargs):
            constructed_in.append(threading.get_ident())
            self.assertEqual(kwargs["timeout"], 30.0)
            return self.client

        with patch.object(httpx, "AsyncClient", side_effect=construct):
            await self.rerank.arerank_documents("Blue", self.candidates, 1)
            await self.rerank.arerank_documents("Blue", self.candidates, 1)
        self.assertEqual(len(constructed_in), 1)
        self.assertNotEqual(constructed_in[0], self.loop_thread)


if __name__ == "__main__":
    unittest.main()
