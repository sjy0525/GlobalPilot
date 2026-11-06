"""Real PostgreSQL/JWT team isolation for the seven existing RAG endpoints.

Only external embedding/rerank/LLM providers are replaced. Membership, ownership,
HTTP dependencies, document/chunk transactions, row locks and vectors are real.
"""
import asyncio
import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import jwt
from fastapi import FastAPI, HTTPException
from langchain_core.documents import Document
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from app.core.http_errors import install_exception_handlers
from app.modules.brands.models import Brand
from app.modules.rag.models import KnowledgeDocument
from app.modules.teams.models import Team, TeamMember
from app.modules.users.models import User

from tests import test_rag_atomicity as atomicity


@unittest.skipUnless(atomicity.TEST_URL, "requires isolated GLOBALPILOT_RAG_TEST_DATABASE_URL")
class RagTeamScopeTests(unittest.IsolatedAsyncioTestCase):
    clear_brand = atomicity.RagAtomicityTests.clear_brand

    async def asyncSetUp(self):
        await atomicity.RagAtomicityTests.asyncSetUp(self)
        self.database = importlib.import_module("app.db.database")
        self.security = importlib.import_module("app.modules.auth.security")
        with patch.dict(sys.modules, {"app.modules.rag.service": self.service}):
            self.routes = atomicity.load_isolated("team_scope_rag_routes", "modules/rag/router.py")
            self.brand_routes = atomicity.load_isolated("team_scope_brand_routes", "modules/brands/router.py")
        self.foreign_team, self.foreign_brand, self.foreign_document = uuid4(), uuid4(), uuid4()
        self.outsider = uuid4()
        async with AsyncSession(self.engine) as db, db.begin():
            db.add(Team(id=self.foreign_team, name=f"Foreign {self.foreign_team}"))
            db.add(User(id=self.outsider, email=f"{self.outsider}@rag-test.invalid", is_active=True))
            await db.execute(update(Brand).where(Brand.id == self.brand_id).values(name="Audio"))
            await db.flush()
            db.add(TeamMember(team_id=self.foreign_team, user_id=self.user_id, role="employee", status="active"))
            db.add(Brand(id=self.foreign_brand, team_id=self.foreign_team, name="Audio"))
            await db.flush()
            db.add(KnowledgeDocument(id=self.foreign_document, team_id=self.foreign_team,
                                     brand_id=self.foreign_brand, source="foreign.txt", body="FOREIGN SECRET",
                                     file_bytes=b"FOREIGN SECRET", version=1, chunk_count=1))
        self.addAsyncCleanup(self.clear_extra)
        factory = sessionmaker(self.sync_engine, expire_on_commit=False)

        def sync_db():
            with factory() as db:
                yield db

        async def async_db():
            async with AsyncSession(self.engine) as db:
                yield db

        self.app = FastAPI()
        install_exception_handlers(self.app)
        self.app.include_router(self.routes.router, prefix="/api")
        self.app.include_router(self.brand_routes.router, prefix="/api")
        self.app.dependency_overrides[self.database.get_db] = sync_db
        self.app.dependency_overrides[self.database.get_async_db] = async_db
        for item in (
            patch.object(self.database, "sessionLocal", factory),
            patch.object(self.database, "async_sessionLocal", async_sessionmaker(self.engine)),
            patch.object(self.service, "async_vector_store", self.store),
            patch.object(self.service, "arerank_documents", AsyncMock(side_effect=lambda _q, hits, **kw: hits)),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://rag-team.invalid")
        self.addAsyncCleanup(self.client.aclose)
        self.token = self.security.create_access_token(str(self.user_id))

    async def clear_extra(self):
        async with AsyncSession(self.engine) as db, db.begin():
            await db.execute(delete(self.store.EmbeddingStore).where(
                self.store.EmbeddingStore.cmetadata["brand_id"].astext == str(self.foreign_brand)))
            await db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.brand_id == self.foreign_brand))
            await db.execute(delete(Brand).where(Brand.id == self.foreign_brand))
            await db.execute(delete(TeamMember).where(TeamMember.team_id == self.foreign_team))
            await db.execute(delete(Team).where(Team.id == self.foreign_team))
            await db.execute(delete(User).where(User.id == self.outsider))

    def cases(self, *, brand_id=None, document_id=None):
        brand = str(brand_id or self.brand_id)
        document = str(document_id or self.document_id)
        return [
            ("GET", "/api/brands", {}),
            ("GET", "/api/documents", {"params": {"brand_id": brand}}),
            ("POST", "/api/documents", {"params": {"brand_id": brand}, "files": {"file": ("new.txt", b"new")}}),
            ("GET", f"/api/documents/{document}/file", {"params": {"brand_id": brand}}),
            ("PUT", f"/api/documents/{document}", {"params": {"brand_id": brand, "expected_version": 1},
                                                       "files": {"file": ("new.txt", b"new")}}),
            ("DELETE", f"/api/documents/{document}", {"params": {"brand_id": brand, "expected_version": 1}}),
            ("POST", "/api/query", {"json": {"brand_id": brand, "question": "old0"}}),
        ]

    async def request(self, method, path, kwargs=None, *, team=None, token=True):
        kwargs = dict(kwargs or {})
        kwargs["params"] = {"team_id": str(team or self.team_id), **kwargs.get("params", {})}
        if token:
            kwargs["headers"] = {"Authorization": f"Bearer {self.token if token is True else token}"}
        return await self.client.request(method, path, **kwargs)

    async def test_all_seven_endpoints_require_real_jwt(self):
        for method, path, kwargs in self.cases():
            with self.subTest(method=method, path=path):
                response = await self.request(method, path, kwargs, token=False)
                self.assertEqual(response.status_code, 401, response.text)

    async def test_all_seven_endpoints_reject_account_without_team_membership(self):
        token = self.security.create_access_token(str(self.outsider))
        for method, path, kwargs in self.cases():
            with self.subTest(method=method, path=path):
                response = await self.request(method, path, kwargs, token=token)
                self.assertEqual(response.status_code, 403, response.text)

    async def test_expired_refresh_and_unknown_user_tokens_cannot_access(self):
        now = datetime.now(timezone.utc)
        payloads = [
            {"sub": str(self.user_id), "token_type": "access", "exp": now - timedelta(minutes=1)},
            {"sub": str(self.user_id), "token_type": "refresh", "exp": now + timedelta(minutes=1)},
            {"sub": str(uuid4()), "token_type": "access", "exp": now + timedelta(minutes=1)},
        ]
        for payload in payloads:
            token = jwt.encode(payload, self.security.settings.jwt_secret_key, algorithm=self.security.settings.jwt_algorithm)
            response = await self.request("GET", "/api/brands", token=token)
            self.assertEqual(response.status_code, 401, response.text)

    async def test_team_id_is_required_and_validated_in_openapi_and_http(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        missing = await self.client.get("/api/brands", headers=headers)
        invalid = await self.client.get("/api/brands", headers=headers, params={"team_id": "bad"})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(invalid.status_code, 422)
        for method, path, _ in self.cases():
            schema_path = path.replace(str(self.document_id), "{document_id}")
            parameters = self.app.openapi()["paths"][schema_path][method.lower()]["parameters"]
            self.assertTrue(any(p["name"] == "team_id" and p["in"] == "query" and p["required"] for p in parameters))

    async def test_brands_with_same_name_are_isolated_for_dual_team_member(self):
        own = await self.request("GET", "/api/brands")
        foreign = await self.request("GET", "/api/brands", team=self.foreign_team)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 200)
        self.assertEqual([item["id"] for item in own.json()], [str(self.brand_id)])
        self.assertEqual([item["id"] for item in foreign.json()], [str(self.foreign_brand)])
        self.assertEqual(own.json()[0]["team_id"], str(self.team_id))

    async def test_cross_team_brand_is_hidden_for_all_six_brand_operations_before_provider(self):
        with patch.object(self.routes, "_read_upload", AsyncMock(side_effect=AssertionError("must not parse foreign data"))), \
             patch.object(self.service, "get_async_vector_store", AsyncMock(side_effect=AssertionError("must not call provider"))):
            for method, path, kwargs in self.cases(brand_id=self.foreign_brand, document_id=self.foreign_document)[1:]:
                with self.subTest(method=method, path=path):
                    response = await self.request(method, path, kwargs)
                    self.assertEqual(response.status_code, 404, response.text)

    async def test_foreign_document_id_is_hidden_even_with_valid_own_brand(self):
        cases = self.cases(document_id=self.foreign_document)
        for index in (3, 4, 5):
            method, path, kwargs = cases[index]
            response = await self.request(method, path, kwargs)
            self.assertEqual(response.status_code, 404, response.text)
        async with AsyncSession(self.engine) as db:
            foreign = await db.get(KnowledgeDocument, self.foreign_document)
            self.assertEqual(foreign.file_bytes, b"FOREIGN SECRET")
            self.assertEqual(foreign.version, 1)

    async def test_employee_can_create_list_download_update_and_delete_own_team_shared_documents(self):
        created = await self.request("POST", "/api/documents", {
            "params": {"brand_id": str(self.brand_id)}, "files": {"file": ("shared.txt", b"shared")},
        })
        self.assertEqual(created.status_code, 201, created.text)
        doc_id = created.json()["id"]
        self.assertEqual(created.json()["team_id"], str(self.team_id))
        self.assertIn(f"team_id={self.team_id}", created.headers["location"])
        listing = await self.request("GET", "/api/documents", {"params": {"brand_id": str(self.brand_id)}})
        self.assertEqual({row["id"] for row in listing.json()}, {self.document_id, doc_id})
        downloaded = await self.client.get(created.headers["location"], headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(downloaded.content, b"shared")
        updated = await self.request("PUT", f"/api/documents/{doc_id}", {
            "params": {"brand_id": str(self.brand_id), "expected_version": 1}, "files": {"file": ("updated.txt", b"updated")},
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["version"], 2)
        stale = await self.request("DELETE", f"/api/documents/{doc_id}", {
            "params": {"brand_id": str(self.brand_id), "expected_version": 1},
        })
        self.assertEqual(stale.status_code, 409)
        deleted = await self.request("DELETE", f"/api/documents/{doc_id}", {
            "params": {"brand_id": str(self.brand_id), "expected_version": 2},
        })
        self.assertEqual(deleted.status_code, 200)
        async with AsyncSession(self.engine) as db:
            self.assertIsNone(await db.get(KnowledgeDocument, UUID(doc_id)))
            chunks = (await db.scalars(select(self.store.EmbeddingStore.id).where(
                self.store.EmbeddingStore.cmetadata["document_id"].astext == doc_id))).all()
            self.assertEqual(chunks, [])

    async def test_same_jwt_stops_all_seven_requests_after_member_removed(self):
        async with AsyncSession(self.engine) as db, db.begin():
            await db.execute(update(TeamMember).where(TeamMember.team_id == self.team_id,
                                                      TeamMember.user_id == self.user_id).values(status="removed"))
        for method, path, kwargs in self.cases():
            response = await self.request(method, path, kwargs)
            self.assertEqual(response.status_code, 403, response.text)
        unaffected = await self.request("GET", "/api/brands", team=self.foreign_team)
        self.assertEqual(unaffected.status_code, 200)

    async def test_disabled_account_is_rejected_with_existing_jwt(self):
        async with AsyncSession(self.engine) as db, db.begin():
            await db.execute(update(User).where(User.id == self.user_id).values(is_active=False))
        for method, path, kwargs in self.cases():
            response = await self.request(method, path, kwargs)
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["code"], "ACCOUNT_DISABLED")

    async def test_role_downgrade_retains_business_access_without_changing_team_scope(self):
        for role in ("admin", "employee"):
            async with AsyncSession(self.engine) as db, db.begin():
                await db.execute(update(TeamMember).where(TeamMember.team_id == self.team_id,
                                                          TeamMember.user_id == self.user_id).values(role=role))
            response = await self.request("GET", "/api/documents", {"params": {"brand_id": str(self.brand_id)}})
            self.assertEqual(response.status_code, 200, response.text)
            hidden = await self.request("GET", "/api/documents", {"params": {"brand_id": str(self.foreign_brand)}})
            self.assertEqual(hidden.status_code, 404)

    async def test_membership_removed_during_embedding_prevents_new_document_and_vectors(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.embeddings.aembed_documents

        async def slow_embed(texts):
            entered.set()
            await release.wait()
            return await original(texts)

        with patch.object(self.embeddings, "aembed_documents", side_effect=slow_embed):
            pending = asyncio.create_task(self.request("POST", "/api/documents", {
                "params": {"brand_id": str(self.brand_id)}, "files": {"file": ("blocked.txt", b"blocked")},
            }))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                async with AsyncSession(self.engine) as db, db.begin():
                    await db.execute(update(TeamMember).where(TeamMember.team_id == self.team_id,
                                                              TeamMember.user_id == self.user_id).values(status="removed"))
            finally:
                release.set()
            response = await pending
        self.assertEqual(response.status_code, 403, response.text)
        async with AsyncSession(self.engine) as db:
            documents = (await db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.brand_id == self.brand_id))).all()
            self.assertEqual([str(doc.id) for doc in documents], [self.document_id])
            vectors = (await db.scalars(select(self.store.EmbeddingStore.document).where(
                self.store.EmbeddingStore.cmetadata["brand_id"].astext == str(self.brand_id)))).all()
            self.assertCountEqual(vectors, ["old0", "old1"])

    async def test_legacy_null_team_documents_and_brands_are_not_exposed(self):
        legacy_brand, legacy_document = uuid4(), uuid4()
        async with AsyncSession(self.engine) as db, db.begin():
            db.add(Brand(id=legacy_brand, name=f"legacy-{legacy_brand}"))
            await db.flush()
            db.add(KnowledgeDocument(id=legacy_document, brand_id=self.brand_id,
                                     source="legacy.txt", body="legacy secret", version=1))
        try:
            own = await self.request("GET", "/api/documents", {"params": {"brand_id": str(self.brand_id)}})
            self.assertEqual([row["id"] for row in own.json()], [self.document_id])
            hidden = await self.request("GET", f"/api/documents/{legacy_document}/file", {"params": {"brand_id": str(self.brand_id)}})
            brand = await self.request("GET", "/api/documents", {"params": {"brand_id": str(legacy_brand)}})
            self.assertEqual(hidden.status_code, 404)
            self.assertEqual(brand.status_code, 404)
        finally:
            async with AsyncSession(self.engine) as db, db.begin():
                await db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id == legacy_document))
                await db.execute(delete(Brand).where(Brand.id == legacy_brand))

    async def test_database_rejects_document_team_brand_mismatch(self):
        with self.assertRaises(IntegrityError):
            async with AsyncSession(self.engine) as db, db.begin():
                db.add(KnowledgeDocument(team_id=self.team_id, brand_id=self.foreign_brand,
                                         source="wrong.txt", body="wrong", version=1))

    async def test_search_ignores_other_team_legacy_and_orphan_chunks(self):
        async with AsyncSession(self.engine) as db, db.begin():
            collection = await self.store.aget_collection(db)
            rows = []
            for metadata, content in [
                ({"brand_id": str(self.brand_id), "document_id": self.document_id}, "LEGACY SECRET"),
                ({"team_id": str(self.foreign_team), "brand_id": str(self.brand_id), "document_id": self.document_id}, "FOREIGN SECRET"),
                ({"team_id": str(self.team_id), "brand_id": str(self.brand_id), "document_id": str(uuid4())}, "ORPHAN SECRET"),
            ]:
                rows.append({"id": str(uuid4()), "collection_id": collection.uuid, "embedding": [1.0, 4.0],
                             "document": content, "cmetadata": metadata})
            await db.execute(insert(self.store.EmbeddingStore).values(rows))
        response = await self.request("POST", "/api/query", {"json": {"brand_id": str(self.brand_id), "question": "old0"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertCountEqual([row["content"] for row in response.json()["sources"]], ["old0", "old1"])
        self.assertNotIn("SECRET", response.text)

    async def test_direct_services_require_scope_and_refresh_membership(self):
        with self.assertRaises(TypeError):
            await self.service.aask("question", self.brand_id)
        async with AsyncSession(self.engine) as db, db.begin():
            await db.execute(update(TeamMember).where(TeamMember.team_id == self.team_id,
                                                      TeamMember.user_id == self.user_id).values(status="removed"))
        async with AsyncSession(self.engine) as db:
            with self.assertRaises(HTTPException) as caught:
                await self.service.alist_documents(db, self.brand_id, team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(caught.exception.status_code, 403)
        with self.assertRaises(HTTPException) as caught:
            await self.service.aask("question", self.brand_id, team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_low_level_vector_helpers_require_team_and_reject_mislabeled_chunks(self):
        with self.assertRaises(TypeError):
            self.vector.brand_filter(str(self.brand_id))
        bad = Document(page_content="bad", metadata={"team_id": str(self.foreign_team),
                       "brand_id": str(self.brand_id), "document_id": self.document_id})
        async with AsyncSession(self.engine) as db:
            with self.assertRaises(ValueError):
                await self.vector.awrite_document_chunks(db, self.store, str(self.brand_id), self.document_id,
                                                        [bad], [[1.0, 1.0]], team_id=str(self.team_id))

    async def test_member_removed_during_rerank_cannot_receive_context(self):
        async def revoke_during_rerank(_question, hits, **kwargs):
            async with AsyncSession(self.engine) as db, db.begin():
                await db.execute(update(TeamMember).where(
                    TeamMember.team_id == self.team_id, TeamMember.user_id == self.user_id,
                ).values(status="removed"))
            return hits

        with patch.object(self.service, "arerank_documents", AsyncMock(side_effect=revoke_during_rerank)):
            response = await self.request("POST", "/api/query", {
                "json": {"brand_id": str(self.brand_id), "question": "old0"},
            })
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn("sources", response.json())

    async def test_member_removed_during_llm_cannot_receive_answer_or_sources(self):
        async def revoke_during_llm(_payload):
            async with AsyncSession(self.engine) as db, db.begin():
                await db.execute(update(TeamMember).where(
                    TeamMember.team_id == self.team_id, TeamMember.user_id == self.user_id,
                ).values(status="removed"))
            return "must not return this private answer"

        with patch.object(self.service, "chain", type("FakeChain", (), {"ainvoke": AsyncMock(side_effect=revoke_during_llm)})()):
            response = await self.request("POST", "/api/query", {
                "json": {"brand_id": str(self.brand_id), "question": "old0"},
            })
        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn("sources", response.json())
        self.assertNotIn("private answer", response.text)

    async def test_sync_search_rechecks_membership_after_rerank(self):
        def revoke(_question, hits, **kwargs):
            with Session(self.sync_engine) as db, db.begin():
                db.execute(update(TeamMember).where(
                    TeamMember.team_id == self.team_id, TeamMember.user_id == self.user_id,
                ).values(status="removed"))
            return hits

        with patch.object(self.service, "vector_store", self.sync_store), \
             patch.object(self.service, "rerank_documents", side_effect=revoke):
            with self.assertRaises(HTTPException) as caught:
                await asyncio.to_thread(self.service.search_documents, "old0", self.brand_id,
                                        team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(caught.exception.status_code, 403)

    async def test_sync_ask_rechecks_membership_after_llm(self):
        def revoke(_payload):
            with Session(self.sync_engine) as db, db.begin():
                db.execute(update(TeamMember).where(
                    TeamMember.team_id == self.team_id, TeamMember.user_id == self.user_id,
                ).values(status="removed"))
            return "private answer"

        with patch.object(self.service, "vector_store", self.sync_store), \
             patch.object(self.service, "rerank_documents", side_effect=lambda _q, hits, **kw: hits), \
             patch.object(self.service, "chain", type("FakeChain", (), {"invoke": staticmethod(revoke)})()):
            with self.assertRaises(HTTPException) as caught:
                await asyncio.to_thread(self.service.ask, "old0", self.brand_id,
                                        team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual(caught.exception.status_code, 403)
