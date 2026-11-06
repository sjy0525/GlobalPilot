"""Native async PGVector operations with fake sessions and no database connection."""
import asyncio
import importlib.util
import sys
import threading
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.documents import Document
from langchain_postgres import PGVector as InstalledPGVector
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


class AsyncVectorStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = Mock(dispose=AsyncMock())
        self.embeddings = Mock(aembed_query=AsyncMock(return_value=[0.1, 0.2]),
                               aembed_documents=AsyncMock(return_value=[[0.1, 0.2]]))
        self.store = Mock(__apost_init__=AsyncMock(), aadd_documents=AsyncMock(return_value=["id"]),
                          asimilarity_search_with_score=AsyncMock(return_value=[("doc", 0.1)]),
                          aget_collection=AsyncMock(return_value=types.SimpleNamespace(uuid="collection")),
                          adelete=AsyncMock())
        self.factory = Mock(return_value=self.store)
        path = Path(__file__).resolve().parents[1] / "app/modules/rag/retrieval/vector_store.py"
        spec = importlib.util.spec_from_file_location("_async_vector_test", path)
        self.vector = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            "langchain_postgres": module("langchain_postgres", PGVector=self.factory),
            "app.core.config": module("app.core.config", settings=types.SimpleNamespace(database_url="postgresql://unused")),
            "app.db.database": module("app.db.database", async_engine=self.engine),
            "app.infra.ai.embeddings": module("app.infra.ai.embeddings", embeddings=self.embeddings),
        }):
            spec.loader.exec_module(self.vector)

    def configure_session(self, rows):
        self.session = Mock(execute=AsyncMock(return_value=Mock(all=Mock(return_value=rows))))
        self.session_closed = False

        @asynccontextmanager
        async def session_context():
            try:
                yield self.session
            finally:
                self.session_closed = True

        table = Table("fake_embedding", MetaData(), Column("id", String), Column("collection_id", String),
                      Column("document", String), Column("cmetadata", JSONB))
        self.store.EmbeddingStore = types.SimpleNamespace(
            id=table.c.id, collection_id=table.c.collection_id, document=table.c.document, cmetadata=table.c.cmetadata)
        self.store._make_async_session = session_context
        self.store._create_filter_clause.return_value = table.c.id.is_not(None)

    async def test_import_does_not_construct_either_vector_store(self):
        self.factory.assert_not_called()
        self.assertIsNone(self.vector.vector_store._store)
        self.assertIsNone(self.vector.async_vector_store._store)
        await self.vector.aclose_vector_store()
        self.engine.dispose.assert_not_called()

    async def test_concurrent_initialization_waits_for_native_setup_once(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def initialize():
            entered.set()
            await release.wait()

        self.store.__apost_init__.side_effect = initialize
        tasks = [asyncio.create_task(self.vector.ainitialize_vector_store()) for _ in range(8)]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            self.assertTrue(all(not task.done() for task in tasks))
            self.assertIsNone(self.vector.async_vector_store._store)
            release.set()
            stores = await asyncio.gather(*tasks)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(store is self.store for store in stores))
        self.factory.assert_called_once_with(embeddings=self.embeddings, collection_name="brand_knowledge",
                                             connection=self.engine, use_jsonb=True, async_mode=True)
        self.store.__apost_init__.assert_awaited_once()

    async def test_failed_or_cancelled_setup_is_not_cached_and_can_retry(self):
        for failure in (RuntimeError("Schema setup failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                await self.vector.aclose_vector_store()
                self.store.__apost_init__.side_effect = failure
                with self.assertRaises(type(failure)):
                    await self.vector.ainitialize_vector_store()
                self.assertIsNone(self.vector.async_vector_store._store)
                next_store = Mock(__apost_init__=AsyncMock())
                self.factory.return_value = next_store
                self.assertIs(await self.vector.ainitialize_vector_store(), next_store)
                next_store.__apost_init__.assert_awaited_once()
                self.factory.return_value = self.store
        self.engine.dispose.assert_not_called()

    async def test_shared_proxy_awaits_native_methods_and_close_does_not_dispose_engine(self):
        handle = self.vector.async_vector_store
        document = Document(page_content="Blue product")
        self.assertEqual(await handle.aadd_documents(documents=[document], ids=["id"]), ["id"])
        self.assertEqual(await handle.asimilarity_search_with_score(query="blue", k=3, filter={"brand_id": "brand"}),
                         [("doc", 0.1)])
        self.store.aadd_documents.assert_awaited_once_with(documents=[document], ids=["id"])
        self.store.asimilarity_search_with_score.assert_awaited_once_with(query="blue", k=3, filter={"brand_id": "brand"})
        self.factory.assert_called_once()
        await self.vector.aclose_vector_store()
        await self.vector.aclose_vector_store()
        self.assertIs(self.vector.async_vector_store, handle)
        self.assertIsNone(handle._store)
        self.engine.dispose.assert_not_called()

    async def test_metadata_delete_awaits_session_and_scopes_the_native_delete(self):
        self.configure_session([("first",), ("second",)])
        metadata_filter = {"brand_id": "brand", "document_id": "document"}
        self.assertEqual(await self.vector.adelete_by_metadata(metadata_filter, team_id="team"), 2)
        self.store.aget_collection.assert_awaited_once_with(self.session)
        self.session.execute.assert_awaited_once()
        self.store._create_filter_clause.assert_called_once_with({"$and": [metadata_filter, {"team_id": {"$in": ["team"]}}]})
        self.store.adelete.assert_awaited_once_with(ids=["first", "second"], collection_only=True)
        self.assertIn("fake_embedding.collection_id", str(self.session.execute.call_args.args[0]))
        self.assertTrue(self.session_closed)

    async def test_missing_collection_does_not_query_or_delete(self):
        self.configure_session([])
        self.store.aget_collection.return_value = None
        self.assertEqual(await self.vector.adelete_by_metadata({"brand_id": "brand"}, team_id="team"), 0)
        self.assertEqual(await self.vector.aload_document_text("brand", "document", team_id="team"), "")
        self.session.execute.assert_not_awaited()
        self.store.adelete.assert_not_awaited()
        self.assertTrue(self.session_closed)

    async def test_document_text_uses_async_select_and_shared_chunk_ordering(self):
        self.configure_session([(" second ", {"chunk_index": "2"}), ("first", {"chunk_index": 1}),
                                (" fallback ", {"chunk_index": "invalid"}), (" ", {"chunk_index": 0})])
        original_join = self.vector._join_document_rows
        formatted_in = []

        def format_rows(rows):
            formatted_in.append(threading.get_ident())
            return original_join(rows)

        with patch.object(self.vector, "_join_document_rows", side_effect=format_rows):
            self.assertEqual(await self.vector.aload_document_text("brand", "document", team_id="team"), "fallback\n\nfirst\n\nsecond")
        self.assertEqual(len(formatted_in), 1)
        self.assertNotEqual(formatted_in[0], threading.get_ident())
        self.session.execute.assert_awaited_once()
        statement = str(self.session.execute.call_args.args[0])
        self.assertIn(" ->> ", statement)
        self.assertNotIn("jsonb_path_match", statement)
        self.store._create_filter_clause.assert_not_called()
        self.assertTrue(self.session_closed)

    async def test_installed_pgvector_uses_async_embeddings_and_native_dispatch_without_executor(self):
        engine = await asyncio.to_thread(create_async_engine, "postgresql+psycopg://unused:unused@127.0.0.1/unused")
        try:
            with patch.object(InstalledPGVector, "__post_init__") as sync_initialize:
                store = InstalledPGVector(connection=engine, embeddings=self.embeddings, async_mode=True, use_jsonb=True)
            sync_initialize.assert_not_called()
            self.assertTrue(store.async_mode)
            self.assertIs(store._async_engine, engine)
            store.__apost_init__ = AsyncMock()
            store.asimilarity_search_with_score_by_vector = AsyncMock(return_value=[("native", 0.2)])
            store.aadd_embeddings = AsyncMock(return_value=["native-id"])
            with patch("langchain_core.vectorstores.base.run_in_executor", side_effect=AssertionError("Sync fallback forbidden")), \
                 patch.object(self.vector, "PGVector", return_value=store):
                self.assertEqual(await self.vector.async_vector_store.asimilarity_search_with_score(query="Blue", k=2),
                                 [("native", 0.2)])
                self.assertEqual(await self.vector.async_vector_store.aadd_documents(
                    documents=[Document(page_content="Blue", metadata={"brand_id": "brand"})], ids=["native-id"]),
                    ["native-id"])
            self.embeddings.aembed_query.assert_awaited_once_with("Blue")
            self.embeddings.aembed_documents.assert_awaited_once_with(["Blue"])
            store.aadd_embeddings.assert_awaited_once()
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
