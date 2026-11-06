"""Real FastAPI/SQL RAG transaction checks against an explicit disposable DB.

Run this file directly to also write docs/verification/rag-http.json. Requests
use HTTPX ASGITransport (in-process HTTP, not TCP); parsers, splitters, handlers,
transactions and PGVector SQL are real. Only external embeddings/LLM are fake.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI
from langchain_core.runnables import RunnableLambda
from langchain_postgres import PGVector
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import app.modules.teams.dependencies  # Load real auth before isolated DB module replacements.
from app.core.http_errors import install_exception_handlers
from app.modules.auth.security import create_access_token
from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.rag.models import KnowledgeAsset, KnowledgeDocument

try:
    from . import test_rag_atomicity as atomicity
except ImportError:
    import test_rag_atomicity as atomicity


EVIDENCE = {
    "transport": "HTTPX ASGITransport: real FastAPI HTTP routing/multipart/error handling, in-process; no TCP server",
    "real_components": ["HTTP POST/PUT/DELETE/GET", "upload parser", "text splitter", "AsyncSession",
                        "PostgreSQL transactions/row locks", "PGVector table writes"],
    "fakes": ["deterministic local embeddings (2 dimensions)", "unused external LLM"],
    "excluded": ["application startup/lifespan", "real embedding/LLM providers", "TCP/network proxy"],
    "cases": [],
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def summarize(snapshot):
    return {
        "state_sha256": digest(snapshot),
        "document_count": len(snapshot["documents"]),
        "vector_count": len(snapshot["vectors"]),
        "vectors_sha256": digest(snapshot["vectors"]),
        "documents": [{"id": row["id"], "source": row["source"], "version": row["version"],
                       "chunk_count": row["chunk_count"], "body_sha256": digest(row["body"]),
                       "file_bytes_sha256": hashlib.sha256(bytes.fromhex(row["file_hex"])).hexdigest(),
                       "file_byte_count": len(row["file_hex"]) // 2}
                      for row in snapshot["documents"]],
    }


@unittest.skipUnless(atomicity.TEST_URL, "requires GLOBALPILOT_RAG_TEST_DATABASE_URL for a disposable PostgreSQL DB")
class HttpRagChecksTests(unittest.IsolatedAsyncioTestCase):
    clear_brand = atomicity.RagAtomicityTests.clear_brand

    async def asyncSetUp(self):
        url = make_url(atomicity.TEST_URL)
        if url.get_backend_name() != "postgresql" or not (url.database or "").startswith(("test_", "audit_", "refactor_")):
            raise AssertionError("Refusing a non-test database")
        self.application_name = "rag_http_check_" + uuid4().hex
        self.engine = create_async_engine(url, connect_args={"application_name": self.application_name})
        self.sync_engine = create_engine(url)
        self.addAsyncCleanup(self.engine.dispose)
        self.addCleanup(self.sync_engine.dispose)
        self.embeddings = atomicity.LocalEmbeddings()

        async def database_dependency():
            async with AsyncSession(self.engine) as db:
                yield db

        database = types.ModuleType("app.db.database")
        database.async_engine = self.engine
        database.get_async_db = database_dependency
        embeddings = types.ModuleType("app.infra.ai.embeddings")
        embeddings.embeddings = self.embeddings
        llm = types.ModuleType("app.infra.ai.llm")
        llm.llm = RunnableLambda(lambda value: "unused local LLM")
        with patch.dict(sys.modules, {"app.db.database": database, "app.infra.ai.embeddings": embeddings,
                                      "app.infra.ai.llm": llm}):
            self.vector = atomicity.load_isolated("http_rag_vector", "modules/rag/retrieval/vector_store.py")
            with patch.dict(sys.modules, {"app.modules.rag.retrieval.vector_store": self.vector}):
                self.service = atomicity.load_isolated("http_rag_service", "modules/rag/service.py")
                with patch.dict(sys.modules, {"app.modules.rag.service": self.service}):
                    self.routes = atomicity.load_isolated("http_rag_routes", "modules/rag/router.py")
                    self.brand_routes = atomicity.load_isolated("http_brand_routes", "modules/brands/router.py")
        self.store = PGVector(connection=self.engine, embeddings=self.embeddings, use_jsonb=True,
                              collection_name="http_rag_checks", async_mode=True)
        await self.store.__apost_init__()
        self.vector.async_vector_store._store = self.store
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[atomicity.User.__table__, atomicity.Team.__table__, atomicity.TeamMember.__table__, Brand.__table__, KnowledgeDocument.__table__, KnowledgeAsset.__table__]))
            EVIDENCE["postgresql_version"] = (await conn.execute(text("SHOW server_version"))).scalar_one()
            EVIDENCE["vector_version"] = (await conn.execute(text(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"))).scalar_one()
        self.brand_id, self.team_id, self.user_id = uuid4(), uuid4(), uuid4()
        async with AsyncSession(self.engine) as db, db.begin():
            db.add(atomicity.User(id=self.user_id, email=f"{self.user_id}@rag-http.invalid", is_active=True))
            db.add(atomicity.Team(id=self.team_id, name=f"RAG HTTP {self.team_id}"))
            await db.flush()
            db.add(atomicity.TeamMember(team_id=self.team_id, user_id=self.user_id, role="employee", status="active"))
            db.add(Brand(id=self.brand_id, team_id=self.team_id, name=str(self.brand_id)))
        self.addAsyncCleanup(self.clear_brand)
        self.app = FastAPI()
        install_exception_handlers(self.app)
        self.app.include_router(self.routes.router, prefix="/api")
        self.app.include_router(self.brand_routes.router, prefix="/api")
        factory = sessionmaker(self.sync_engine)
        def authenticated_database():
            with factory() as db:
                yield db
        self.app.dependency_overrides[app.modules.teams.dependencies.get_db] = authenticated_database
        token = create_access_token(str(self.user_id))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                        base_url="http://rag-check.invalid", params={"team_id": str(self.team_id)}, headers={"Authorization": f"Bearer {token}"})
        self.addAsyncCleanup(self.client.aclose)
        self.old_bytes = b"old paragraph one\n\nold paragraph two"
        response = await self.request("POST", self.old_bytes, filename="old.txt")
        self.assertEqual(response.status_code, 201, response.text)
        self.initial = response.json()
        self.document_id = self.initial["id"]
        self.assertEqual(self.initial["version"], 1)
        self.assertEqual(self.initial["chunk_count"], 2)
        self.assertIn(f"/api/documents/{self.document_id}/file", response.headers["location"])
        self.events = []

    def note(self, event, **details):
        self.events.append({"event": event, "monotonic_ns": time.monotonic_ns(), **details})

    async def request(self, method, content=None, *, filename="updated.txt", version=1):
        path = "/api/documents" if method == "POST" else f"/api/documents/{self.document_id}"
        params = {"brand_id": str(self.brand_id)}
        if method in ("PUT", "DELETE"):
            params["expected_version"] = version
        kwargs = {"files": {"file": (filename, content, "text/plain")}} if content is not None else {}
        return await self.client.request(method, path, params=params, **kwargs)

    async def snapshot(self, db=None):
        if db is None:
            async with AsyncSession(self.engine) as session:
                return await self.snapshot(session)
        fields = ("id", "source", "body", "file_bytes", "media_type", "chunk_count", "version", "created_at", "updated_at")
        documents = (await db.execute(select(*[getattr(KnowledgeDocument, name) for name in fields]).where(
            KnowledgeDocument.brand_id == self.brand_id).order_by(KnowledgeDocument.id))).mappings().all()
        table = self.store.EmbeddingStore
        vectors = (await db.execute(select(table.id, table.collection_id, table.embedding, table.document,
                                          table.cmetadata).where(table.cmetadata["brand_id"].astext == str(self.brand_id))
                                    .order_by(table.id))).all()
        return {
            "documents": [{**{name: row[name] for name in ("source", "body", "media_type", "chunk_count", "version")},
                           "id": str(row["id"]), "file_hex": (row["file_bytes"] or b"").hex(),
                           "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}
                          for row in documents],
            "vectors": [{"id": row.id, "collection_id": str(row.collection_id),
                         "embedding": row.embedding.tolist(), "document": row.document, "metadata": row.cmetadata}
                        for row in vectors],
        }

    async def assert_original_http_read(self):
        response = await self.client.get(f"/api/documents/{self.document_id}/file", params={"brand_id": str(self.brand_id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.old_bytes)
        listing = await self.client.get("/api/documents", params={"brand_id": str(self.brand_id)})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json(), [self.initial])

    def record(self, name, before, after, statuses, **details):
        EVIDENCE["cases"].append({"name": name, "passed": True, "http_statuses": statuses,
                                  "before": summarize(before), "after": summarize(after),
                                  "events": list(self.events), **details})

    async def failing_request(self, method, point):
        before = await self.snapshot()
        original = self.service.awrite_document_chunks
        observed = {}
        new_content = b"replacement paragraph one\n\nreplacement paragraph two\n\nreplacement paragraph three"

        async def inject(db, *args, **kwargs):
            if point == "after_vector_delete_and_insert":
                await original(db, *args, **kwargs)
            observed["uncommitted"] = await self.snapshot(db)
            try:
                await db.execute(text("SELECT 1 / 0"))
            except Exception as exc:
                observed["sqlstate"] = getattr(getattr(exc, "orig", None), "sqlstate", None)
                raise

        with patch.object(self.service, "awrite_document_chunks", side_effect=inject), \
             self.assertLogs("app.core.http_errors", level="ERROR"):
            response = await self.request(method, new_content, filename="replacement.txt")
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertEqual(observed["sqlstate"], "22012")
        local = observed["uncommitted"]
        replaced = next(row for row in local["documents"] if row["source"] == "replacement.txt")
        self.assertEqual(replaced["body"], new_content.decode())
        self.assertEqual(replaced["file_hex"], new_content.hex())
        self.assertEqual(replaced["version"], 1 if method == "POST" else 2)
        new_vectors = [row for row in local["vectors"] if row["metadata"]["document_id"] == replaced["id"]]
        if point == "after_vector_delete_and_insert":
            self.assertEqual({row["document"] for row in new_vectors}, set(new_content.decode().split("\n\n")))
        else:
            self.assertEqual(local["vectors"], before["vectors"])
        after = await self.snapshot()
        self.assertEqual(after, before)
        await self.assert_original_http_read()
        self.record(f"{method}_{point}_rollback", before, after, [500], injection_sqlstate="22012",
                    uncommitted_before_error=summarize(local),
                    rollback_verified_fields=["body", "file_bytes", "version", "source", "timestamps",
                                              "chunk_count", "vector_ids", "embeddings", "vector_text", "vector_metadata"])

    async def test_upload_sql_failures_roll_back_document_and_vectors(self):
        for point in ("after_document_flush", "after_vector_delete_and_insert"):
            with self.subTest(point=point):
                await self.failing_request("POST", point)

    async def test_put_sql_failures_roll_back_document_and_vectors(self):
        for point in ("after_document_flush", "after_vector_delete_and_insert"):
            with self.subTest(point=point):
                await self.failing_request("PUT", point)

    async def test_delete_sql_failure_restores_deleted_vectors(self):
        before = await self.snapshot()
        original = self.service.awrite_document_chunks
        observed = {}

        async def inject(db, *args, **kwargs):
            await original(db, *args, **kwargs)
            observed["uncommitted"] = await self.snapshot(db)
            try:
                await db.execute(text("SELECT 1 / 0"))
            except Exception as exc:
                observed["sqlstate"] = getattr(getattr(exc, "orig", None), "sqlstate", None)
                raise

        with patch.object(self.service, "awrite_document_chunks", side_effect=inject), \
             self.assertLogs("app.core.http_errors", level="ERROR"):
            response = await self.request("DELETE")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertEqual(observed["sqlstate"], "22012")
        self.assertEqual(observed["uncommitted"]["vectors"], [])
        self.assertEqual(observed["uncommitted"]["documents"], before["documents"])
        after = await self.snapshot()
        self.assertEqual(after, before)
        await self.assert_original_http_read()
        self.record("DELETE_after_vector_delete_rollback", before, after, [500], injection_sqlstate="22012",
                    uncommitted_before_error=summarize(observed["uncommitted"]))

    async def test_delete_sql_failure_after_document_delete_restores_both_tables(self):
        before = await self.snapshot()
        observed = {}
        snapshot = self.snapshot

        class FailAfterDeleteSession(AsyncSession):
            async def delete(self, instance):
                await super().delete(instance)
                await self.flush()  # Send the real business-row DELETE before failing.
                observed["uncommitted"] = await snapshot(self)
                try:
                    await self.execute(text("SELECT 1 / 0"))
                except Exception as exc:
                    observed["sqlstate"] = getattr(getattr(exc, "orig", None), "sqlstate", None)
                    raise

        with patch.object(self.service, "AsyncSession", FailAfterDeleteSession), \
             self.assertLogs("app.core.http_errors", level="ERROR"):
            response = await self.request("DELETE")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertEqual(observed["sqlstate"], "22012")
        self.assertEqual(observed["uncommitted"], {"documents": [], "vectors": []})
        after = await self.snapshot()
        self.assertEqual(after, before)
        await self.assert_original_http_read()
        self.record("DELETE_after_both_table_deletes_rollback", before, after, [500], injection_sqlstate="22012",
                    uncommitted_before_error=summarize(observed["uncommitted"]))

    async def wait_for_row_lock_waiter(self):
        async def wait():
            async with AsyncSession(self.engine) as db:
                while True:
                    rows = (await db.execute(text("""
                        SELECT pid, wait_event_type, wait_event FROM pg_stat_activity
                        WHERE datname=current_database() AND application_name=:application_name
                          AND wait_event_type='Lock'
                          AND (query ILIKE '%knowledge_documents%' OR query ILIKE '%teams%')
                          AND query ILIKE '%FOR UPDATE%'
                    """), {"application_name": self.application_name})).mappings().all()
                    await db.rollback()  # Refresh pg_stat_activity's transaction-local snapshot.
                    if rows:
                        return [dict(row) for row in rows]
                    await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait(), 5)

    def assert_complete_winner(self, snapshot, response, content):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(len(snapshot["documents"]), 1)
        record = snapshot["documents"][0]
        self.assertEqual(record["body"], content.decode())
        self.assertEqual(record["file_hex"], content.hex())
        self.assertEqual(record["version"], 2)
        self.assertEqual(record["chunk_count"], len(snapshot["vectors"]))
        self.assertEqual({row["document"] for row in snapshot["vectors"]}, set(content.decode().split("\n\n")))
        self.assertTrue(all(row["metadata"]["document_id"] == self.document_id for row in snapshot["vectors"]))

    async def test_two_concurrent_puts_have_one_winner_and_one_conflict(self):
        before = await self.snapshot()
        both_prepared, release_embeddings = asyncio.Event(), asyncio.Event()
        winner_holds_lock, release_commit = asyncio.Event(), asyncio.Event()
        original_embed = self.embeddings.aembed_documents
        original_write = self.service.awrite_document_chunks
        arrivals = 0

        async def embedding_barrier(texts):
            nonlocal arrivals
            arrivals += 1
            self.note("embedding_barrier_entered", arrivals=arrivals)
            if arrivals == 2:
                both_prepared.set()
            await asyncio.wait_for(release_embeddings.wait(), 5)
            return await original_embed(texts)

        async def hold_commit(*args, **kwargs):
            result = await original_write(*args, **kwargs)
            self.note("winner_finished_vector_sql_holding_row_lock")
            winner_holds_lock.set()
            await asyncio.wait_for(release_commit.wait(), 5)
            return result

        contents = [b"A one\n\nA two\n\nA three", b"B only"]
        tasks = []
        with patch.object(self.embeddings, "aembed_documents", side_effect=embedding_barrier), \
             patch.object(self.service, "awrite_document_chunks", side_effect=hold_commit):
            try:
                tasks = [asyncio.create_task(self.request("PUT", content)) for content in contents]
                await asyncio.wait_for(both_prepared.wait(), 5)
                self.assertTrue(all(not task.done() for task in tasks))
                self.note("both_http_requests_pending_at_barrier")
                release_embeddings.set()
                await asyncio.wait_for(winner_holds_lock.wait(), 5)
                waiters = await self.wait_for_row_lock_waiter()
                self.note("postgres_row_lock_wait_confirmed", waiters=waiters)
                self.assertTrue(all(not task.done() for task in tasks))
                self.assertEqual(await self.snapshot(), before)
            finally:
                release_embeddings.set()
                release_commit.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 6)
        statuses = [response.status_code for response in responses]
        self.assertCountEqual(statuses, [200, 409])
        self.assertEqual(responses[statuses.index(409)].json()["code"], "CONFLICT")
        after = await self.snapshot()
        winner = statuses.index(200)
        self.assert_complete_winner(after, responses[winner], contents[winner])
        self.record("two_PUT_same_version", before, after, statuses, embedding_barrier_arrivals=arrivals,
                    real_postgres_lock_waiters=waiters)

    async def test_delete_commits_while_put_waits_for_embedding_without_resurrection(self):
        before = await self.snapshot()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.embeddings.aembed_documents

        async def hold_embedding(texts):
            self.note("PUT_entered_embedding_barrier")
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await original(texts)

        with patch.object(self.embeddings, "aembed_documents", side_effect=hold_embedding):
            put = asyncio.create_task(self.request("PUT", b"too late replacement"))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.assertFalse(put.done())
                removed = await self.request("DELETE")
                self.assertEqual(removed.status_code, 200)
                self.assertEqual(removed.json()["deleted_chunks"], 2)
                self.assertFalse(put.done())
                self.note("DELETE_committed_while_PUT_still_pending")
                self.assertEqual(await self.snapshot(), {"documents": [], "vectors": []})
            finally:
                release.set()
                updated = await asyncio.wait_for(put, 6)
        self.assertEqual(updated.status_code, 404)
        self.assertEqual(updated.json()["code"], "NOT_FOUND")
        after = await self.snapshot()
        self.assertEqual(after, {"documents": [], "vectors": []})
        self.record("DELETE_wins_while_PUT_preparing", before, after, [removed.status_code, updated.status_code])

    async def test_put_commit_makes_waiting_delete_conflict_without_deleting_new_vectors(self):
        before = await self.snapshot()
        held, release = asyncio.Event(), asyncio.Event()
        original = self.service.awrite_document_chunks
        new_content = b"winner first\n\nwinner second\n\nwinner third"

        async def hold_commit(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.note("PUT_vector_sql_finished_holding_row_lock")
            held.set()
            await asyncio.wait_for(release.wait(), 5)
            return result

        tasks = []
        with patch.object(self.service, "awrite_document_chunks", side_effect=hold_commit):
            try:
                tasks.append(asyncio.create_task(self.request("PUT", new_content)))
                await asyncio.wait_for(held.wait(), 5)
                tasks.append(asyncio.create_task(self.request("DELETE")))
                waiters = await self.wait_for_row_lock_waiter()
                self.assertTrue(all(not task.done() for task in tasks))
                self.note("DELETE_waiting_on_PUT_row_lock", waiters=waiters)
                self.assertEqual(await self.snapshot(), before)
            finally:
                release.set()
                updated, removed = await asyncio.wait_for(asyncio.gather(*tasks), 6)
        self.assertEqual(removed.status_code, 409)
        self.assertEqual(removed.json()["code"], "CONFLICT")
        after = await self.snapshot()
        self.assert_complete_winner(after, updated, new_content)
        self.record("PUT_wins_DELETE_waits", before, after, [updated.status_code, removed.status_code],
                    real_postgres_lock_waiters=waiters)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sources = ("app/modules/rag/router.py", "app/modules/brands/router.py", "app/modules/rag/schemas.py",
               "app/modules/rag/service.py", "app/modules/rag/retrieval/vector_store.py",
               "app/modules/rag/processing/loaders.py", "app/modules/rag/processing/splitter.py", "app/core/http_errors.py")
    before_sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources}
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(HttpRagChecksTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    after_sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources}
    EVIDENCE.update(generated_at=datetime.now(timezone.utc).isoformat(), tests_run=result.testsRun,
                    passed=result.wasSuccessful() and not result.skipped and before_sources == after_sources,
                    source_sha256=after_sources, sources_unchanged_during_run=before_sources == after_sources,
                    failures=[str(test) for test, _ in result.failures],
                    errors=[str(test) for test, _ in result.errors],
                    skipped=[str(test) for test, _ in result.skipped])
    output = Path(os.environ.get("RAG_HTTP_EVIDENCE_PATH", "docs/verification/rag-http.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(EVIDENCE, indent=2, ensure_ascii=False) + "\n")
    sys.exit(0 if EVIDENCE["passed"] else 1)
