"""Real PostgreSQL/PGVector regression tests, isolated from application data.

Run only against a disposable database named test_*, audit_* or refactor_*:
GLOBALPILOT_RAG_TEST_DATABASE_URL=postgresql+psycopg://... python -m unittest tests.test_rag_atomicity
Embedding calls are deterministic local fakes; SQL, MVCC, locks and vector writes are real.
"""
import asyncio
import importlib.util
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import numpy  # Load native modules before temporary sys.modules replacements.
from fastapi import HTTPException
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.runnables import RunnableLambda
from langchain_postgres import PGVector
from pgvector.sqlalchemy import Vector
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.rag.models import KnowledgeAsset, KnowledgeDocument
from app.modules.teams.models import Team, TeamMember
from app.modules.users.models import User


TEST_URL = os.environ.get("GLOBALPILOT_RAG_TEST_DATABASE_URL")


def load_isolated(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "app" / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[1.0, float(len(value))] for value in texts]

    def embed_query(self, value):
        return [1.0, float(len(value))]

    async def aembed_documents(self, texts):
        return self.embed_documents(texts)

    async def aembed_query(self, value):
        return self.embed_query(value)


def split(text, metadata):
    return [Document(page_content=part, metadata={**metadata, "chunk_index": index})
            for index, part in enumerate(text.split("|"))]


@unittest.skipUnless(TEST_URL, "requires an explicitly configured disposable PostgreSQL database")
class RagAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = make_url(TEST_URL)
        if url.get_backend_name() != "postgresql" or not url.database.startswith(("test_", "audit_", "refactor_")):
            raise AssertionError("Refusing to run RAG integration tests against a non-test database")
        self.engine = create_async_engine(url)
        self.sync_engine = create_engine(url)
        self.addAsyncCleanup(self.engine.dispose)
        self.addCleanup(self.sync_engine.dispose)
        self.embeddings = LocalEmbeddings()
        database = types.ModuleType("app.db.database")
        database.async_engine = self.engine
        embeddings_module = types.ModuleType("app.infra.ai.embeddings")
        embeddings_module.embeddings = self.embeddings
        with patch.dict(sys.modules, {"app.db.database": database, "app.infra.ai.embeddings": embeddings_module}):
            self.vector = load_isolated("atomic_vector_module", "modules/rag/retrieval/vector_store.py")
        llm_module = types.ModuleType("app.infra.ai.llm")
        llm_module.llm = RunnableLambda(lambda value: "unused")
        with patch.dict(sys.modules, {"app.modules.rag.retrieval.vector_store": self.vector, "app.infra.ai.llm": llm_module}):
            self.service = load_isolated("atomic_rag_service", "modules/rag/service.py")
        self.store = PGVector(connection=self.engine, embeddings=self.embeddings,
                              collection_name="rag_atomicity_tests", async_mode=True, use_jsonb=True)
        await self.store.__apost_init__()
        self.sync_store = await asyncio.to_thread(
            PGVector, connection=self.sync_engine, embeddings=self.embeddings,
            collection_name="rag_atomicity_tests", use_jsonb=True,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(
                conn, tables=[User.__table__, Team.__table__, TeamMember.__table__, Brand.__table__, KnowledgeDocument.__table__, KnowledgeAsset.__table__]))
        for item in (
            patch.object(self.service, "get_async_vector_store", AsyncMock(return_value=self.store)),
            patch.object(self.service, "vector_store", Mock(initialize=Mock(return_value=self.sync_store))),
            patch.object(self.service, "asplit_to_documents", AsyncMock(side_effect=split)),
            patch.object(self.service, "split_to_documents", split),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.brand_id, self.team_id, self.user_id = uuid4(), uuid4(), uuid4()
        async with AsyncSession(self.engine) as db, db.begin():
            db.add(User(id=self.user_id, email=f"{self.user_id}@rag-test.invalid", is_active=True))
            db.add(Team(id=self.team_id, name=f"RAG test {self.team_id}"))
            await db.flush()
            db.add(TeamMember(team_id=self.team_id, user_id=self.user_id, role="employee", status="active"))
            db.add(Brand(id=self.brand_id, team_id=self.team_id, name=str(self.brand_id)))
        self.addAsyncCleanup(self.clear_brand)
        async with AsyncSession(self.engine) as db:
            self.initial = await self.service.aindex_text(db, self.brand_id, "old0|old1", "old.txt", file_bytes=b"old", team_id=self.team_id, actor_user_id=self.user_id)
        self.document_id = self.initial["id"]

    async def clear_brand(self):
        async with AsyncSession(self.engine) as db, db.begin():
            await db.execute(delete(self.store.EmbeddingStore).where(
                self.store.EmbeddingStore.cmetadata["brand_id"].astext == str(self.brand_id)))
            await db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.brand_id == self.brand_id))
            await db.execute(delete(Brand).where(Brand.id == self.brand_id))
            await db.execute(delete(TeamMember).where(TeamMember.team_id == self.team_id))
            await db.execute(delete(Team).where(Team.id == self.team_id))
            await db.execute(delete(User).where(User.id == self.user_id))

    async def update(self, content, expected_version=1):
        async with AsyncSession(self.engine) as db:
            return await self.service.aindex_text(
                db, self.brand_id, content, "updated.txt", document_id=self.document_id,
                file_bytes=content.encode(), expected_version=expected_version, team_id=self.team_id, actor_user_id=self.user_id,
            )

    async def remove(self, expected_version=1):
        async with AsyncSession(self.engine) as db:
            return await self.service.adelete_document(db, self.brand_id, self.document_id, expected_version, team_id=self.team_id, actor_user_id=self.user_id)

    async def snapshot(self):
        async with AsyncSession(self.engine) as db:
            record = await db.get(KnowledgeDocument, self.document_id)
            row = None if record is None else (record.body, record.chunk_count, record.version, record.file_bytes)
            result = await db.execute(select(self.store.EmbeddingStore.document).where(
                self.store.EmbeddingStore.cmetadata["document_id"].astext == self.document_id,
            ).order_by(self.store.EmbeddingStore.id))
            return row, list(result.scalars())

    async def test_two_puts_with_same_version_have_one_winner_and_no_mixed_chunks(self):
        arrived = 0
        ready = asyncio.Event()
        original = self.embeddings.aembed_documents

        async def both_prepared(texts):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 3)
            return await original(texts)

        with patch.object(self.embeddings, "aembed_documents", side_effect=both_prepared):
            outcomes = await asyncio.wait_for(asyncio.gather(
                self.update("A0|A1|A2"), self.update("B0"), return_exceptions=True,
            ), 5)
        self.assertEqual(sum(isinstance(value, dict) for value in outcomes), 1)
        loser = next(value for value in outcomes if isinstance(value, Exception))
        self.assertIsInstance(loser, HTTPException)
        self.assertEqual(loser.status_code, 409)
        row, chunks = await self.snapshot()
        self.assertEqual(row[2], 2)
        self.assertEqual(chunks, row[0].split("|"))
        self.assertEqual(row[1], len(chunks))
        self.assertEqual(row[3], row[0].encode())

    async def test_delete_during_embedding_prevents_resurrection_and_orphan_vectors(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.embeddings.aembed_documents

        async def slow_embedding(texts):
            entered.set()
            await release.wait()
            return await original(texts)

        with patch.object(self.embeddings, "aembed_documents", side_effect=slow_embedding):
            update = asyncio.create_task(self.update("new0"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                removed = await asyncio.wait_for(self.remove(), 3)
                self.assertEqual(removed["version"], 1)
                self.assertEqual(removed["deleted_chunks"], 2)
            finally:
                release.set()
            with self.assertRaises(HTTPException) as error:
                await update
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(await self.snapshot(), (None, []))

    async def test_stale_delete_does_not_remove_newer_document(self):
        await self.update("new0")
        with self.assertRaises(HTTPException) as error:
            await self.remove(1)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(await self.snapshot(), (("new0", 1, 2, b"new0"), ["new0"]))

    async def test_embedding_failure_and_cancellation_leave_old_version_searchable(self):
        initial = await self.snapshot()
        for failure in (RuntimeError("embedding unavailable"), asyncio.CancelledError()):
            with patch.object(self.embeddings, "aembed_documents", side_effect=failure):
                with self.assertRaises(type(failure)):
                    await self.update("new0")
            self.assertEqual(await self.snapshot(), initial)

    async def test_sql_failure_after_vector_replacement_rolls_back_both_tables(self):
        initial = await self.snapshot()
        original = self.service.awrite_document_chunks

        async def fail_after_write(db, *args, **kwargs):
            await original(db, *args, **kwargs)
            await db.execute(text("SELECT 1 / 0"))

        with patch.object(self.service, "awrite_document_chunks", side_effect=fail_after_write):
            with self.assertRaises(Exception):
                await self.update("new0|new1|new2")
        self.assertEqual(await self.snapshot(), initial)

    async def test_failed_creation_and_deletion_do_not_leave_partial_rows(self):
        initial = await self.snapshot()
        original = self.service.awrite_document_chunks

        async def fail_after_write(db, *args, **kwargs):
            await original(db, *args, **kwargs)
            await db.execute(text("SELECT 1 / 0"))

        with patch.object(self.service, "awrite_document_chunks", side_effect=fail_after_write):
            with self.assertRaises(Exception):
                await self.remove()
            async with AsyncSession(self.engine) as db:
                with self.assertRaises(Exception):
                    await self.service.aindex_text(db, self.brand_id, "new0", "new.txt", team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(await self.snapshot(), initial)
        async with AsyncSession(self.engine) as db:
            documents = (await db.execute(select(KnowledgeDocument.id).where(
                KnowledgeDocument.brand_id == self.brand_id))).scalars().all()
            chunks = (await db.execute(select(self.store.EmbeddingStore.document).where(
                self.store.EmbeddingStore.cmetadata["brand_id"].astext == str(self.brand_id)))).scalars().all()
        self.assertEqual(len(documents), 1)
        self.assertCountEqual(chunks, ["old0", "old1"])

    async def test_cancellation_inside_write_transaction_rolls_back_both_tables(self):
        initial = await self.snapshot()
        entered = asyncio.Event()
        original = self.service.awrite_document_chunks

        async def wait_after_write(*args, **kwargs):
            await original(*args, **kwargs)
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.service, "awrite_document_chunks", side_effect=wait_after_write):
            update = asyncio.create_task(self.update("cancelled0"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
            finally:
                update.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await update
        self.assertEqual(await self.snapshot(), initial)

    async def test_readers_see_old_complete_snapshot_until_atomic_commit(self):
        initial = await self.snapshot()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.service.awrite_document_chunks

        async def hold_before_commit(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        with patch.object(self.service, "awrite_document_chunks", side_effect=hold_before_commit):
            update = asyncio.create_task(self.update("new0"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                self.assertEqual(await self.snapshot(), initial)
            finally:
                release.set()
            result = await update
        self.assertEqual(result["version"], 2)
        self.assertEqual(await self.snapshot(), (("new0", 1, 2, b"new0"), ["new0"]))

    async def test_sync_update_uses_the_same_atomic_write_and_version_protocol(self):
        def sync_update(expected):
            with Session(self.sync_engine) as db:
                return self.service.index_text(db, self.brand_id, "sync0|sync1|sync2", "sync.txt",
                                               document_id=self.document_id, expected_version=expected,
                                               file_bytes=b"sync", team_id=self.team_id, actor_user_id=self.user_id)
        result = await asyncio.to_thread(sync_update, 1)
        self.assertEqual(result["version"], 2)
        with self.assertRaises(HTTPException) as error:
            await asyncio.to_thread(sync_update, 1)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(await self.snapshot(), (("sync0|sync1|sync2", 3, 2, b"sync"), ["sync0", "sync1", "sync2"]))
        def sync_delete():
            with Session(self.sync_engine) as db:
                return self.service.delete_document(db, self.brand_id, self.document_id, 2, team_id=self.team_id, actor_user_id=self.user_id)
        removed = await asyncio.to_thread(sync_delete)
        self.assertEqual(removed["version"], 2)
        self.assertEqual(await self.snapshot(), (None, []))

    async def test_sync_and_async_updates_share_the_row_lock(self):
        sync_entered, release_sync = threading.Event(), threading.Event()
        original_sync = self.embeddings.embed_documents

        def slow_sync(texts):
            sync_entered.set()
            if not release_sync.wait(4):
                raise AssertionError("sync embedding was not released")
            return original_sync(texts)

        async def prepared_async(texts):
            return original_sync(texts)

        def sync_update():
            with Session(self.sync_engine) as db:
                return self.service.index_text(db, self.brand_id, "sync0", "sync.txt",
                                               document_id=self.document_id, expected_version=1, team_id=self.team_id, actor_user_id=self.user_id)

        with patch.object(self.embeddings, "embed_documents", side_effect=slow_sync), \
             patch.object(self.embeddings, "aembed_documents", side_effect=prepared_async):
            update = asyncio.create_task(asyncio.to_thread(sync_update))
            try:
                self.assertTrue(await asyncio.to_thread(sync_entered.wait, 3))
                result = await self.update("async0|async1")
                self.assertEqual(result["version"], 2)
            finally:
                release_sync.set()
            with self.assertRaises(HTTPException) as error:
                await update
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(await self.snapshot(), (("async0|async1", 2, 2, b"async0|async1"), ["async0", "async1"]))


if __name__ == "__main__":
    unittest.main()
