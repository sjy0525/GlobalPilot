"""Native async RAG routes/services with isolated SQL, vector and provider APIs."""
import asyncio
import importlib.util
import inspect
import sys
import threading
import types
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import openai
import sqlalchemy.orm
import app.modules.briefs.models  # Preload mapped classes before temporary sys.modules replacements.
import app.modules.evaluation.models
import app.modules.rag.models
import app.modules.users.models
from fastapi import FastAPI, HTTPException
from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda

import app.modules.teams.dependencies  # Load real auth before isolated DB module replacements.
from app.core.http_errors import install_exception_handlers
from app.modules.brands.models import Brand


async def unused_database_dependency():
    raise AssertionError("Async database dependency must be overridden")


def load_isolated(name, relative_path):
    path = Path(__file__).resolve().parents[1] / "app" / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UploadConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        database = types.ModuleType("app.db.database")
        database.get_async_db = unused_database_dependency
        vector = types.ModuleType("app.modules.rag.retrieval.vector_store")
        vector.vector_store = Mock()
        vector.async_vector_store = Mock()
        vector.brand_filter = lambda brand, *, team_id: {"brand_id": brand, "team_id": team_id}
        vector.document_filter = lambda brand, document: {"brand_id": brand, "document_id": document}
        vector.chunk_id = lambda brand, document, index: f"{brand}:{document}:{index}"
        vector.delete_by_metadata = Mock()
        vector.load_document_text = Mock()
        vector.awrite_document_chunks = AsyncMock()
        vector.write_document_chunks = Mock()
        vector.get_async_vector_store = AsyncMock()
        vector.aload_document_text = AsyncMock()
        rerank = types.ModuleType("app.modules.rag.retrieval.rerank")
        rerank.rerank_documents = Mock()
        rerank.arerank_documents = AsyncMock()
        llm = types.ModuleType("app.infra.ai.llm")
        llm.llm = RunnableLambda(lambda prompt: "unused")
        with patch.dict(sys.modules, {
            "app.db.database": database, "app.modules.rag.retrieval.vector_store": vector,
            "app.modules.rag.retrieval.rerank": rerank, "app.infra.ai.llm": llm,
        }):
            cls.service = load_isolated("upload_async_service", "modules/rag/service.py")
            with patch.dict(sys.modules, {"app.modules.rag.service": cls.service}):
                cls.routes = load_isolated("upload_async_routes", "modules/rag/router.py")
                cls.brand_routes = load_isolated("upload_async_brand_routes", "modules/brands/router.py")

    async def asyncSetUp(self):
        self.loop_thread = threading.get_ident()
        self.brand_id, self.document_id, self.team_id, self.user_id = uuid4(), uuid4(), uuid4(), uuid4()
        self.member = types.SimpleNamespace(team_id=self.team_id, user_id=self.user_id)
        self.brand = types.SimpleNamespace(id=self.brand_id, team_id=self.team_id, name="Audio", created_at=datetime.now())
        self.record = self.service.KnowledgeDocument(
            id=self.document_id, team_id=self.team_id, brand_id=self.brand_id, source="sample.txt", body="hello",
            media_type="text/plain; charset=utf-8", file_bytes=b"hello", chunk_count=1, version=1,
            created_at=datetime.now(), updated_at=datetime.now(),
        )
        self.missing_brand = self.missing_document = False
        self.held_stage = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.stages = []

        async def execute(statement):
            if not hasattr(statement, "column_descriptions"):
                return Mock()
            is_brand = statement.column_descriptions[0]["entity"] is Brand
            await self.stage("brand_sql" if is_brand else "document_sql")
            result = Mock()
            row = self.brand if is_brand else self.record
            missing = self.missing_brand if is_brand else self.missing_document
            if missing:
                row = None
            result.scalar_one_or_none.return_value = row
            result.scalars.return_value.all.return_value = [] if row is None else [row]
            result.all.return_value = [] if row is None else [row]
            return result

        def add(record):
            record.id = self.document_id
            record.created_at = record.updated_at = datetime.now()
            self.record = record

        self.db = Mock()
        self.db.execute = AsyncMock(side_effect=execute)
        self.db.add.side_effect = add
        for method in ("flush", "commit", "refresh", "delete", "rollback"):
            async def operation(*args, stage_name=method):
                await self.stage(stage_name)
            setattr(self.db, method, AsyncMock(side_effect=operation))

        self.write_db = Mock()
        self.write_db.add.side_effect = add
        self.write_db.execute = AsyncMock(side_effect=execute)
        self.write_db.connection = AsyncMock()
        for method in ("flush", "delete", "commit"):
            async def operation(*args, stage_name=method):
                await self.stage(stage_name)
            setattr(self.write_db, method, AsyncMock(side_effect=operation))

        async def scalar(statement):
            result = await execute(statement)
            return result.scalar_one_or_none()
        self.write_db.scalar = AsyncMock(side_effect=scalar)
        self.db.scalar = AsyncMock(side_effect=scalar)
        self.write_db.scalars = AsyncMock(side_effect=lambda _statement: Mock(all=Mock(return_value=[self.record])))

        @asynccontextmanager
        async def transaction():
            yield
            await self.write_db.commit()
        self.write_db.begin = transaction

        @asynccontextmanager
        async def fresh_session(**kwargs):
            yield self.write_db

        async def embed(texts):
            await self.stage("embedding")
            return [[1.0, 0.0] for text in texts]

        async def write_vectors(*args, **kwargs):
            await self.stage("vector_write")
            return 1

        async def search_vectors(**kwargs):
            await self.stage("search")
            return [(Document(page_content="hello", metadata={
                "source": "sample.txt", "team_id": str(self.team_id), "brand_id": str(self.brand_id), "document_id": str(self.document_id), "chunk_index": 0,
            }), 0.0)]

        async def rerank(question, candidates, top_n):
            await self.stage("rerank")
            return candidates[:top_n]

        async def answer(payload):
            await self.stage("llm")
            return "answer"

        self.vector = types.SimpleNamespace(
            embeddings=types.SimpleNamespace(aembed_documents=AsyncMock(side_effect=embed)),
            asimilarity_search_with_score=AsyncMock(side_effect=search_vectors),
        )
        self.chain = types.SimpleNamespace(ainvoke=AsyncMock(side_effect=answer))
        for item in (
            patch.object(self.service, "_arequire_brand_scope", AsyncMock(return_value=self.brand)),
            patch.object(self.service, "_require_brand_scope", Mock(return_value=self.brand)),
            patch("app.db.database.async_sessionLocal", fresh_session),
            patch.object(self.brand_routes, "require_active_team_member_async", AsyncMock()),
            patch.object(self.service, "async_vector_store", self.vector),
            patch.object(self.service, "awrite_document_chunks", AsyncMock(side_effect=write_vectors)),
            patch.object(self.service, "get_async_vector_store", AsyncMock(return_value=self.vector)),
            patch.object(self.service, "AsyncSession", fresh_session),
            patch.object(self.service, "uuid4", return_value=self.document_id),
            patch.object(self.service, "arerank_documents", AsyncMock(side_effect=rerank)),
            patch.object(self.service, "chain", self.chain),
            patch.object(self.service, "asplit_to_documents", AsyncMock(
                side_effect=lambda text, metadata: [Document(page_content=text, metadata=metadata)]
            )),
        ):
            item.start()
            self.addCleanup(item.stop)

        self.app = FastAPI()
        install_exception_handlers(self.app)
        self.app.include_router(self.routes.router, prefix="/api")
        self.app.include_router(self.brand_routes.router, prefix="/api")
        async def database():
            return self.db
        self.app.dependency_overrides[unused_database_dependency] = database
        self.app.dependency_overrides[self.routes.get_current_member_from_query] = lambda: self.member

        @self.app.get("/ping")
        async def ping():
            return {"ok": True, "thread": threading.get_ident()}

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://audit.invalid", params={"team_id": str(self.team_id)})
        self.addAsyncCleanup(self.client.aclose)

    async def stage(self, name):
        self.stages.append((name, threading.get_ident()))
        if name == self.held_stage:
            self.entered.set()
            await self.release.wait()

    def path(self, method):
        return "/api/documents" if method == "POST" else f"/api/documents/{self.document_id}"

    async def request(self, method, filename="sample.txt", content=b"hello"):
        return await self.client.request(method, self.path(method), params={"brand_id": str(self.brand_id), "expected_version": self.record.version}, files={"file": (filename, content)})

    async def assert_ping_before_completion(self, task):
        await asyncio.wait_for(self.entered.wait(), timeout=2)
        self.assertFalse(task.done())
        ping = await asyncio.wait_for(self.client.get("/ping"), timeout=2)
        self.assertEqual(ping.json(), {"ok": True, "thread": self.loop_thread})
        self.assertFalse(task.done())

    async def test_sql_and_embedding_waits_yield_without_routing_through_threadpool(self):
        for method in ("POST", "PUT"):
            for held_stage in ("brand_sql", "embedding"):
                with self.subTest(method=method, held_stage=held_stage):
                    self.held_stage = held_stage
                    self.entered, self.release = asyncio.Event(), asyncio.Event()
                    self.stages.clear()
                    upload = asyncio.create_task(self.request(method))
                    try:
                        await self.assert_ping_before_completion(upload)
                    finally:
                        self.release.set()
                        response = await asyncio.wait_for(upload, timeout=3)
                    self.assertEqual(response.status_code, 201 if method == "POST" else 200)
                    self.assertEqual(response.json()["id"], str(self.document_id))
                    self.assertTrue(all(thread == self.loop_thread for _, thread in self.stages))
                    expected = ["brand_sql", "rollback", "embedding", "flush", "vector_write", "commit"] if method == "POST" else ["brand_sql", "rollback", "document_sql", "embedding", "document_sql", "flush", "vector_write", "commit"]
                    self.assertEqual([name for name, _ in self.stages], expected)
                    if method == "POST":
                        location = urlsplit(response.headers["location"])
                        self.assertEqual(location.path, f"/api/documents/{self.document_id}/file")
                        self.assertEqual(parse_qs(location.query), {"brand_id": [str(self.brand_id)], "team_id": [str(self.team_id)]})

    async def test_rolled_file_uses_uploadfile_async_read_worker_only(self):
        original = self.routes._read_upload
        loop = asyncio.get_running_loop()
        for method in ("POST", "PUT"):
            with self.subTest(method=method):
                self.entered = asyncio.Event()
                released = threading.Event()
                reads = []

                async def read_upload(file):
                    self.assertTrue(file.file._rolled)
                    original_read = file.file.read
                    def read(*args, **kwargs):
                        reads.append(threading.get_ident())
                        loop.call_soon_threadsafe(self.entered.set)
                        if not released.wait(timeout=3):
                            raise AssertionError("Disk read was not released")
                        return original_read(*args, **kwargs)
                    with patch.object(file.file, "read", read):
                        return await original(file)

                with patch.object(self.routes, "_read_upload", read_upload):
                    upload = asyncio.create_task(self.request(method, content=b"x" * (1024 * 1024 + 1)))
                    try:
                        await self.assert_ping_before_completion(upload)
                    finally:
                        released.set()
                        response = await asyncio.wait_for(upload, timeout=3)
                self.assertEqual(response.status_code, 201 if method == "POST" else 200)
                self.assertEqual(len(reads), 1)
                self.assertNotEqual(reads[0], self.loop_thread)
                self.assertTrue(all(thread == self.loop_thread for _, thread in self.stages))

    async def test_client_errors_keep_status_and_do_not_add_vectors(self):
        for method in ("POST", "PUT"):
            for filename, content, status, code in (
                ("unsupported.docx", b"hello", 415, "UNSUPPORTED_MEDIA_TYPE"),
                ("invalid.txt", b"\xff", 400, "BAD_REQUEST"),
                ("empty.txt", b"", 400, "BAD_REQUEST"),
                ("empty.pdf", b"", 400, "INVALID_PDF"),
            ):
                with self.subTest(method=method, filename=filename):
                    response = await self.request(method, filename, content)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json()["code"], code)
            self.missing_brand = True
            self.assertEqual((await self.request(method)).status_code, 404)
            self.missing_brand = False
        self.vector.embeddings.aembed_documents.assert_not_awaited()
        self.db.commit.assert_not_awaited()

    async def test_missing_update_document_is_404_before_vector_mutation(self):
        self.missing_document = True
        response = await self.request("PUT")
        self.assertEqual(response.status_code, 404)
        self.service.awrite_document_chunks.assert_not_awaited()
        self.vector.embeddings.aembed_documents.assert_not_awaited()

    async def test_async_provider_errors_keep_domain_and_upstream_mapping(self):
        request = httpx.Request("POST", "https://provider.invalid/embeddings")
        for method in ("POST", "PUT"):
            for error, status, code in (
                (HTTPException(status_code=404, detail="文档不存在"), 404, "NOT_FOUND"),
                (openai.AuthenticationError("private provider credentials", response=httpx.Response(401, request=request), body=None), 502, "UPSTREAM_ERROR"),
                (openai.APITimeoutError(request=request), 504, "UPSTREAM_TIMEOUT"),
                (RuntimeError("private internal detail"), 500, "INTERNAL_ERROR"),
            ):
                with self.subTest(method=method, error_type=type(error).__name__):
                    self.vector.embeddings.aembed_documents.side_effect = error
                    if isinstance(error, HTTPException):
                        response = await self.request(method)
                    else:
                        with self.assertLogs("app.core.http_errors", level="ERROR"):
                            response = await self.request(method)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json()["code"], code)
                    self.assertNotIn("private internal detail", response.text)
                    self.assertNotIn("private provider credentials", response.text)
                    if status != 401:
                        self.assertNotIn("www-authenticate", response.headers)
        self.db.commit.assert_not_awaited()

    async def test_remaining_rag_routes_use_async_services(self):
        self.assertTrue(inspect.iscoroutinefunction(self.brand_routes.get_brands))
        for name in ("get_documents", "download_document", "remove_document", "query_rag"):
            self.assertTrue(inspect.iscoroutinefunction(getattr(self.routes, name)))
        brands = await self.client.get("/api/brands")
        self.assertEqual(brands.json()[0]["name"], "Audio")
        documents = await self.client.get("/api/documents", params={"brand_id": str(self.brand_id)})
        self.assertEqual(documents.json()[0]["id"], str(self.document_id))
        file = await self.client.get(f"/api/documents/{self.document_id}/file", params={"brand_id": str(self.brand_id)})
        self.assertEqual(file.content, b"hello")
        sync_db = Mock()
        sync_db.scalar.return_value = self.record
        sync_file = self.service.document_file(sync_db, self.brand_id, self.document_id, team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(file.headers["content-disposition"], sync_file.headers["content-disposition"])
        deleted = await self.client.delete(f"/api/documents/{self.document_id}", params={"brand_id": str(self.brand_id), "expected_version": self.record.version})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"id": str(self.document_id), "deleted_chunks": 1, "version": 1})
        self.write_db.delete.assert_awaited_once_with(self.record)

    async def test_async_file_fallback_keeps_original_missing_file_contract(self):
        self.record.file_bytes = None
        self.record.body = None
        self.record.source = "archived.md"
        path = f"/api/documents/{self.document_id}/file"
        with patch.object(self.service, "aload_document_text", AsyncMock(return_value="restored")) as load_text:
            response = await self.client.get(path, params={"brand_id": str(self.brand_id)})
            self.assertEqual(response.content, b"restored")
            self.assertTrue(response.headers["content-disposition"].endswith("filename*=UTF-8''archived.txt"))
            load_text.assert_awaited_once_with(str(self.brand_id), str(self.document_id), db=self.write_db, store=self.vector, team_id=str(self.team_id))
            load_text.return_value = ""
            self.assertEqual((await self.client.get(path, params={"brand_id": str(self.brand_id)})).status_code, 404)
            self.record.source = "archived.pdf"
            load_text.reset_mock()
            self.assertEqual((await self.client.get(path, params={"brand_id": str(self.brand_id)})).status_code, 409)
            load_text.assert_not_awaited()

    async def test_async_query_yields_during_llm_wait_and_preserves_sources(self):
        self.held_stage = "llm"
        query = asyncio.create_task(self.client.post("/api/query", json={"brand_id": str(self.brand_id), "question": "question"}))
        try:
            await self.assert_ping_before_completion(query)
        finally:
            self.release.set()
            response = await asyncio.wait_for(query, timeout=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "answer")
        self.assertEqual(response.json()["sources"][0]["document_id"], str(self.document_id))
        self.vector.asimilarity_search_with_score.assert_awaited_once()
        self.chain.ainvoke.assert_awaited_once()
        self.assertTrue(all(thread == self.loop_thread for _, thread in self.stages))

    async def test_no_knowledge_query_remains_200_without_llm_call(self):
        self.vector.asimilarity_search_with_score.side_effect = None
        self.vector.asimilarity_search_with_score.return_value = []
        response = await self.client.post("/api/query", json={"brand_id": str(self.brand_id), "question": "question"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"answer": self.service.INSUFFICIENT_CONTEXT_ANSWER, "sources": []})
        self.chain.ainvoke.assert_not_awaited()

    async def test_sync_service_entry_points_remain_available_for_background_callers(self):
        for name in ("index_text", "list_documents", "document_file", "delete_document", "search_documents", "ask"):
            self.assertFalse(inspect.iscoroutinefunction(getattr(self.service, name)))
        db = Mock()
        write = MagicMock()
        write.__enter__.return_value = write
        write.add.side_effect = lambda record: setattr(record, "created_at", datetime.now())
        store = Mock()
        store.embeddings.embed_documents.return_value = [[1.0, 0.0]]
        with patch.object(self.service, "vector_store", Mock(initialize=Mock(return_value=store))), \
             patch.object(self.service, "Session", return_value=write), \
             patch.object(self.service, "write_document_chunks") as write_chunks:
            result = self.service.index_text(db, self.brand_id, "hello", "sample.txt", team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(result["id"], str(self.document_id))
        self.assertEqual(result["version"], 1)
        write_chunks.assert_called_once()
        store.embeddings.embed_documents.assert_called_once_with(["hello"])
        db.commit.assert_not_called()



if __name__ == "__main__":
    unittest.main()
