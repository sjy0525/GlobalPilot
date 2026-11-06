"""RAG reads use consistent snapshots without exhausting a one-connection pool."""
import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from langchain_postgres import PGVector
from sqlalchemy import create_engine, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

import app.modules.teams.dependencies  # Load real auth before isolated DB module replacements.
from app.modules.rag.models import KnowledgeDocument

try:
    from . import test_rag_atomicity as atomicity
except ImportError:
    import test_rag_atomicity as atomicity


@unittest.skipUnless(atomicity.TEST_URL, "requires an explicitly configured disposable PostgreSQL database")
class RagReadConnectionTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only fixture methods, not the original TestCase's tests.
    asyncSetUp = atomicity.RagAtomicityTests.asyncSetUp
    clear_brand = atomicity.RagAtomicityTests.clear_brand

    async def limited_async_store(self):
        engine = create_async_engine(atomicity.TEST_URL, pool_size=1, max_overflow=0, pool_timeout=0.2)
        self.addAsyncCleanup(engine.dispose)
        store = PGVector(
            connection=engine, embeddings=self.embeddings, collection_name=self.store.collection_name,
            async_mode=True, use_jsonb=True,
        )
        await store.__apost_init__()
        return engine, store

    async def make_legacy_document(self):
        async with AsyncSession(self.engine) as db, db.begin():
            await db.execute(
                update(KnowledgeDocument).where(KnowledgeDocument.id == self.document_id)
                .values(body=None, file_bytes=None)
            )

    async def test_query_releases_brand_connection_before_vector_retrieval(self):
        engine, store = await self.limited_async_store()
        database = types.ModuleType("app.db.database")
        database.get_async_db = lambda: None
        with patch.dict(sys.modules, {"app.db.database": database, "app.modules.rag.service": self.service}):
            routes = atomicity.load_isolated("rag_read_connection_routes", "modules/rag/router.py")
        with patch.object(self.service, "async_vector_store", store), \
             patch.object(self.service, "arerank_documents", AsyncMock(side_effect=lambda question, hits, **kw: hits)):
            async with AsyncSession(engine) as db:
                result = await routes.query_rag(routes.RagQuery(question="old0", brand_id=self.brand_id), types.SimpleNamespace(team_id=self.team_id, user_id=self.user_id), db)
                self.assertEqual(result["answer"], "unused")
                self.assertEqual(len(result["sources"]), 2)
                self.assertFalse(db.in_transaction())
                self.assertEqual(engine.pool.checkedout(), 0)

    async def test_async_legacy_preview_releases_document_connection(self):
        await self.make_legacy_document()
        engine, store = await self.limited_async_store()
        with patch.object(self.service, "get_async_vector_store", AsyncMock(return_value=store)):
            async with AsyncSession(engine) as db:
                response = await self.service.adocument_file(db, self.brand_id, self.document_id, team_id=self.team_id, actor_user_id=self.user_id)
                self.assertEqual(response.body, b"old0\n\nold1")
                self.assertIn("old.txt", response.headers["content-disposition"])
                self.assertFalse(db.in_transaction())
                self.assertEqual(engine.pool.checkedout(), 0)

    async def test_sync_legacy_preview_releases_document_connection(self):
        await self.make_legacy_document()
        engine = create_engine(atomicity.TEST_URL, pool_size=1, max_overflow=0, pool_timeout=0.2)
        self.addCleanup(engine.dispose)
        store = await asyncio.to_thread(
            PGVector, connection=engine, embeddings=self.embeddings,
            collection_name=self.store.collection_name, use_jsonb=True,
        )

        def read():
            with Session(engine) as db:
                response = self.service.document_file(db, self.brand_id, self.document_id, team_id=self.team_id, actor_user_id=self.user_id)
                self.assertEqual(response.body, b"old0\n\nold1")
                self.assertIn("old.txt", response.headers["content-disposition"])
                self.assertFalse(db.in_transaction())
                self.assertEqual(engine.pool.checkedout(), 0)

        with patch.object(self.service, "vector_store", types.SimpleNamespace(initialize=lambda: store)):
            await asyncio.to_thread(read)

    async def test_async_legacy_preview_keeps_metadata_and_chunks_from_one_snapshot(self):
        await self.make_legacy_document()
        engine, store = await self.limited_async_store()
        original = self.service.aload_document_text

        async def replace_before_reading_chunks(*args, **kwargs):
            async with AsyncSession(self.engine) as writer:
                await self.service.aindex_text(
                    writer, self.brand_id, "new0", "new-name.txt", document_id=self.document_id,
                    expected_version=1, file_bytes=b"new0", team_id=self.team_id, actor_user_id=self.user_id,
                )
            return await original(*args, **kwargs)

        with patch.object(self.service, "get_async_vector_store", AsyncMock(return_value=store)), \
             patch.object(self.service, "aload_document_text", side_effect=replace_before_reading_chunks):
            async with AsyncSession(engine) as db:
                response = await self.service.adocument_file(db, self.brand_id, self.document_id, team_id=self.team_id, actor_user_id=self.user_id)
                self.assertEqual(response.body, b"old0\n\nold1")
                self.assertIn("old.txt", response.headers["content-disposition"])
                self.assertFalse(db.in_transaction())
                self.assertEqual(engine.pool.checkedout(), 0)
        async with AsyncSession(engine) as db:
            current = await self.service.adocument_file(db, self.brand_id, self.document_id, team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(current.body, b"new0")
        self.assertIn("new-name.txt", current.headers["content-disposition"])

    async def test_async_legacy_preview_finishes_its_snapshot_when_document_is_deleted(self):
        await self.make_legacy_document()
        engine, store = await self.limited_async_store()
        original = self.service.aload_document_text

        async def delete_before_reading_chunks(*args, **kwargs):
            async with AsyncSession(self.engine) as writer:
                await self.service.adelete_document(writer, self.brand_id, self.document_id, 1, team_id=self.team_id, actor_user_id=self.user_id)
            return await original(*args, **kwargs)

        with patch.object(self.service, "get_async_vector_store", AsyncMock(return_value=store)), \
             patch.object(self.service, "aload_document_text", side_effect=delete_before_reading_chunks):
            async with AsyncSession(engine) as db:
                response = await self.service.adocument_file(db, self.brand_id, self.document_id, team_id=self.team_id, actor_user_id=self.user_id)
                self.assertEqual(response.body, b"old0\n\nold1")
                self.assertIn("old.txt", response.headers["content-disposition"])
                self.assertEqual(engine.pool.checkedout(), 0)
        async with AsyncSession(engine) as db:
            self.assertIsNone(await db.get(KnowledgeDocument, self.document_id))

    async def test_sync_legacy_preview_keeps_metadata_and_chunks_from_one_snapshot(self):
        await self.make_legacy_document()
        engine = create_engine(atomicity.TEST_URL, pool_size=1, max_overflow=0, pool_timeout=0.2)
        self.addCleanup(engine.dispose)
        store = await asyncio.to_thread(
            PGVector, connection=engine, embeddings=self.embeddings,
            collection_name=self.store.collection_name, use_jsonb=True,
        )
        original = self.service.load_document_text

        def replace_before_reading_chunks(*args, **kwargs):
            with Session(self.sync_engine) as writer:
                self.service.index_text(
                    writer, self.brand_id, "new0", "new-name.txt", document_id=self.document_id,
                    expected_version=1, file_bytes=b"new0", team_id=self.team_id, actor_user_id=self.user_id,
                )
            return original(*args, **kwargs)

        def read():
            with Session(engine) as db:
                response = self.service.document_file(db, self.brand_id, self.document_id, team_id=self.team_id, actor_user_id=self.user_id)
                self.assertEqual(response.body, b"old0\n\nold1")
                self.assertIn("old.txt", response.headers["content-disposition"])
                self.assertFalse(db.in_transaction())
                self.assertEqual(engine.pool.checkedout(), 0)

        with patch.object(self.service, "vector_store", types.SimpleNamespace(initialize=lambda: store)), \
             patch.object(self.service, "load_document_text", side_effect=replace_before_reading_chunks):
            await asyncio.to_thread(read)


if __name__ == "__main__":
    unittest.main()
