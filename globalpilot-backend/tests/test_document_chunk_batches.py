"""Real 1024-dimensional chunk batches and rollback across later batch failures.

RAG_BATCH_TEST_DATABASE_URL must name an isolated test_/audit_/refactor_ database.
No embedding/LLM provider is called. Data cleanup is scoped to each test's brand.
"""

import os
import unittest
from uuid import uuid4

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_postgres import PGVector
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.rag.models import KnowledgeAsset, KnowledgeDocument
from app.modules.teams.models import Team
from app.modules.rag.retrieval import vector_store

URL = os.environ.get("RAG_BATCH_TEST_DATABASE_URL")


class NoProvider(Embeddings):
    def embed_documents(self, texts):
        raise AssertionError("Tests supply vectors directly")

    def embed_query(self, text):
        raise AssertionError("Tests supply vectors directly")


class FailSecondAsyncBatch(AsyncSession):
    batch_count = 0

    async def execute(self, statement, *args, **kwargs):
        if getattr(statement, "is_insert", False) and statement.table.name == "langchain_pg_embedding":
            self.batch_count += 1
            if self.batch_count == 2:
                await super().execute(text("SELECT 1 / 0"))
        return await super().execute(statement, *args, **kwargs)


class FailSecondSyncBatch(Session):
    batch_count = 0

    def execute(self, statement, *args, **kwargs):
        if getattr(statement, "is_insert", False) and statement.table.name == "langchain_pg_embedding":
            self.batch_count += 1
            if self.batch_count == 2:
                super().execute(text("SELECT 1 / 0"))
        return super().execute(statement, *args, **kwargs)


@unittest.skipUnless(URL, "requires isolated RAG_BATCH_TEST_DATABASE_URL")
class DocumentChunkBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = make_url(URL)
        if url.get_backend_name() != "postgresql" or not url.database.startswith(("test_", "audit_", "refactor_")):
            raise AssertionError("Refusing a non-test database")
        self.engine = create_async_engine(url)
        self.sync_engine = create_engine(url)
        self.addAsyncCleanup(self.engine.dispose)
        self.addCleanup(self.sync_engine.dispose)
        self.store = PGVector(connection=self.engine, embeddings=NoProvider(),
                              collection_name="chunk_batch_tests", async_mode=True, use_jsonb=True)
        await self.store.__apost_init__()
        # Only mapping/get_collection are used by the sync writer. Avoid another
        # PGVector constructor or any extra setup connection during a write.
        self.sync_store = object.__new__(PGVector)
        self.sync_store._async_engine = None
        self.sync_store.EmbeddingStore = self.store.EmbeddingStore
        self.sync_store.CollectionStore = self.store.CollectionStore
        self.sync_store.collection_name = "chunk_batch_tests"
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(
                conn, tables=[Team.__table__, Brand.__table__, KnowledgeDocument.__table__, KnowledgeAsset.__table__]))
        self.brand_id, self.document_id, self.team_id = uuid4(), uuid4(), uuid4()
        self.vector = [1.0] + [0.0] * 1023
        async with AsyncSession(self.engine) as session, session.begin():
            session.add(Team(id=self.team_id, name=f"Batch {self.team_id}"))
            await session.flush()
            session.add(Brand(id=self.brand_id, team_id=self.team_id, name=str(self.brand_id)))
            await session.flush()
            session.add(KnowledgeDocument(
                id=self.document_id, team_id=self.team_id, brand_id=self.brand_id, source="old.txt",
                body="old", file_bytes=b"old", media_type="text/plain", chunk_count=1, version=1,
            ))
            await session.flush()
            await vector_store.awrite_document_chunks(
                session, self.store, str(self.brand_id), str(self.document_id), self.chunks(1, "old"), [self.vector], team_id=str(self.team_id),
            )
        self.addAsyncCleanup(self.clear_data)

    def chunks(self, count, prefix="new"):
        return [Document(page_content=f"{prefix}-{index}", metadata={
            "team_id": str(self.team_id), "brand_id": str(self.brand_id), "document_id": str(self.document_id), "chunk_index": index,
        }) for index in range(count)]

    async def clear_data(self):
        table = self.store.EmbeddingStore
        async with AsyncSession(self.engine) as session, session.begin():
            await session.execute(delete(table).where(table.cmetadata["brand_id"].astext == str(self.brand_id)))
            await session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == self.document_id))
            await session.execute(delete(Brand).where(Brand.id == self.brand_id))
            await session.execute(delete(Team).where(Team.id == self.team_id))

    async def assert_old_document_intact(self):
        table = self.store.EmbeddingStore
        async with AsyncSession(self.engine) as read:
            record = await read.get(KnowledgeDocument, self.document_id)
            chunks = (await read.execute(select(table.document).where(
                table.cmetadata["document_id"].astext == str(self.document_id)))).scalars().all()
        self.assertEqual((record.body, record.file_bytes, record.version, record.chunk_count), ("old", b"old", 1, 1))
        self.assertEqual(chunks, ["old-0"])

    async def test_14000_chunks_with_real_1024_dimensional_binds_succeed(self):
        count = 14000
        async with AsyncSession(self.engine) as write, write.begin():
            await write.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == self.document_id).values(
                body="new", file_bytes=b"new", version=2, chunk_count=count))
            deleted = await vector_store.awrite_document_chunks(
                write, self.store, str(self.brand_id), str(self.document_id), self.chunks(count), [self.vector] * count, team_id=str(self.team_id),
            )
        self.assertEqual(deleted, 1)
        table = self.store.EmbeddingStore
        async with AsyncSession(self.engine) as read:
            record = await read.get(KnowledgeDocument, self.document_id)
            amount = await read.scalar(select(func.count()).select_from(table).where(
                table.cmetadata["document_id"].astext == str(self.document_id)))
            last = (await read.execute(select(table.document, table.cmetadata).where(
                table.id == vector_store.chunk_id(str(self.brand_id), str(self.document_id), count - 1)))).one()
        self.assertEqual((record.body, record.version, record.chunk_count, amount), ("new", 2, count, count))
        self.assertEqual((last.document, last.cmetadata["chunk_index"]), ("new-13999", 13999))

    async def test_async_second_batch_sql_failure_rolls_back_metadata_delete_and_first_batch(self):
        count = vector_store.DOCUMENT_INSERT_BATCH_SIZE * 2 + 1
        session = FailSecondAsyncBatch(self.engine)
        with self.assertRaises(DBAPIError) as caught:
            async with session, session.begin():
                await session.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == self.document_id).values(
                    body="new", file_bytes=b"new", version=2, chunk_count=count))
                await vector_store.awrite_document_chunks(
                    session, self.store, str(self.brand_id), str(self.document_id), self.chunks(count), [self.vector] * count, team_id=str(self.team_id),
                )
        self.assertEqual(getattr(caught.exception.orig, "sqlstate", None), "22012")
        self.assertEqual(session.batch_count, 2)
        await self.assert_old_document_intact()

    async def test_sync_second_batch_sql_failure_rolls_back_metadata_delete_and_first_batch(self):
        count = vector_store.DOCUMENT_INSERT_BATCH_SIZE * 2 + 1
        with self.assertRaises(DBAPIError) as caught:
            with FailSecondSyncBatch(self.sync_engine) as session, session.begin():
                session.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == self.document_id).values(
                    body="new", file_bytes=b"new", version=2, chunk_count=count))
                vector_store.write_document_chunks(
                    session, self.sync_store, str(self.brand_id), str(self.document_id), self.chunks(count), [self.vector] * count, team_id=str(self.team_id),
                )
        self.assertEqual(getattr(caught.exception.orig, "sqlstate", None), "22012")
        self.assertEqual(session.batch_count, 2)
        await self.assert_old_document_intact()


if __name__ == "__main__":
    unittest.main()
