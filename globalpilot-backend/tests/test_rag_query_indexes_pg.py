"""Native PGVector query semantics and narrow document-list regression tests."""
import asyncio
import unittest
from uuid import uuid4

import numpy as np
from sqlalchemy import delete, event, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

try:
    from . import test_rag_atomicity as atomicity
except ImportError:
    import test_rag_atomicity as atomicity


@unittest.skipUnless(atomicity.TEST_URL, "requires an explicitly configured disposable PostgreSQL database")
class RagQueryIndexesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = atomicity.RagAtomicityTests.asyncSetUp
    clear_brand = atomicity.RagAtomicityTests.clear_brand

    async def test_native_text_filter_preserves_exact_neighbors_and_brand_scope(self):
        rng = np.random.default_rng(123)
        vectors = -np.abs(rng.standard_normal((40, 2)))
        query = [-1.0, -1.0]
        foreign_id = str(uuid4())
        async with AsyncSession(self.engine) as db, db.begin():
            collection = await self.store.aget_collection(db)
            rows = [{"id": str(uuid4()), "collection_id": collection.uuid,
                     "embedding": vector.tolist(), "document": f"candidate {i}",
                     "cmetadata": {"team_id": str(self.team_id), "brand_id": str(self.brand_id), "document_id": self.document_id}}
                    for i, vector in enumerate(vectors)]
            rows.append({"id": foreign_id, "collection_id": collection.uuid, "embedding": query,
                         "document": "different brand, exact vector match",
                         "cmetadata": {"brand_id": str(uuid4()), "document_id": str(uuid4())}})
            await db.execute(insert(self.store.EmbeddingStore).values(rows))
        try:
            before = await self.store.asimilarity_search_with_score_by_vector(
                query, k=20, filter={"brand_id": str(self.brand_id)})
            after = await self.store.asimilarity_search_with_score_by_vector(
                query, k=20, filter=self.vector.brand_filter(str(self.brand_id), team_id=str(self.team_id)))
            self.assertEqual([doc.id for doc, _ in before], [doc.id for doc, _ in after])
            self.assertEqual(len(after), 20)
            for (old_doc, old_score), (doc, score) in zip(before, after):
                self.assertEqual(doc.metadata["brand_id"], str(self.brand_id))
                self.assertNotEqual(doc.id, foreign_id)
                self.assertAlmostEqual(old_score, score, places=12)
            clause = str(self.store._create_filter_clause(self.vector.brand_filter(str(self.brand_id), team_id=str(self.team_id))))
            self.assertIn(" ->> ", clause)
            self.assertNotIn("jsonb_path_match", clause)
        finally:
            async with AsyncSession(self.engine) as db, db.begin():
                await db.execute(delete(self.store.EmbeddingStore).where(self.store.EmbeddingStore.id == foreign_id))

    async def test_sync_and_async_document_lists_return_same_metadata_without_large_fields(self):
        statements = []

        def capture(_conn, _cursor, sql, _parameters, _context, _many):
            if sql.startswith("SELECT") and "knowledge_documents" in sql:
                statements.append(sql)

        event.listen(self.engine.sync_engine, "before_cursor_execute", capture)
        event.listen(self.sync_engine, "before_cursor_execute", capture)
        try:
            async with AsyncSession(self.engine) as db:
                asynchronous = await self.service.alist_documents(db, self.brand_id, team_id=self.team_id, actor_user_id=self.user_id)
                self.assertEqual(len(db.identity_map), 0)

            def read_sync():
                with Session(self.sync_engine) as db:
                    rows = self.service.list_documents(db, self.brand_id, team_id=self.team_id, actor_user_id=self.user_id)
                    self.assertEqual(len(db.identity_map), 0)
                    return rows

            synchronous = await asyncio.to_thread(read_sync)
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", capture)
            event.remove(self.sync_engine, "before_cursor_execute", capture)
        self.assertEqual(synchronous, asynchronous)
        self.assertEqual(synchronous, [self.initial])
        self.assertEqual(len(statements), 2)
        for sql in statements:
            columns = sql.split("FROM", 1)[0]
            for large_or_unused in ("body", "file_bytes", "media_type"):
                self.assertNotIn(f"knowledge_documents.{large_or_unused}", columns)
            self.assertIn("ORDER BY knowledge_documents.updated_at DESC", sql)


if __name__ == "__main__":
    unittest.main()
