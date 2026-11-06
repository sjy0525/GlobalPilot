"""Real PostgreSQL/PGVector/HTTP evidence lifecycle in an explicit test database.

Parser normalization is supplied as a fixed contract fixture. Embedding, rerank
and generation providers are local doubles. Real Docling/OCR corpus checks live
in scripts/check_structured_pdf_corpus.py and are reported separately.
"""

import copy
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker

import app.modules.teams.dependencies
from app.core.http_errors import install_exception_handlers
from app.modules.auth.security import create_access_token
from app.modules.rag.models import KnowledgeAsset, KnowledgeDocument
from tests import test_rag_atomicity as atomicity
from tests.test_structured_rag import PNG, document_fixture


@unittest.skipUnless(atomicity.TEST_URL, "requires isolated GLOBALPILOT_RAG_TEST_DATABASE_URL")
class StructuredRagPostgresTests(unittest.IsolatedAsyncioTestCase):
    clear_brand = atomicity.RagAtomicityTests.clear_brand

    async def asyncSetUp(self):
        await atomicity.RagAtomicityTests.asyncSetUp(self)
        self.database = importlib.import_module("app.db.database")
        factory = sessionmaker(self.sync_engine, expire_on_commit=False)

        def sync_db():
            with factory() as db:
                yield db

        async def async_db():
            async with AsyncSession(self.engine) as db:
                yield db

        with patch.dict(sys.modules, {"app.modules.rag.service": self.service}):
            self.routes = atomicity.load_isolated("structured_rag_routes", "modules/rag/router.py")
        self.app = FastAPI()
        install_exception_handlers(self.app)
        self.app.include_router(self.routes.router, prefix="/api")
        self.app.dependency_overrides[self.database.get_db] = sync_db
        self.app.dependency_overrides[self.database.get_async_db] = async_db
        self.app.dependency_overrides[app.modules.teams.dependencies.get_db] = sync_db
        for item in (
            patch.object(self.database, "sessionLocal", factory),
            patch.object(self.database, "async_sessionLocal", async_sessionmaker(self.engine)),
            patch.object(self.service, "async_vector_store", self.store),
            patch.object(self.service, "arerank_documents", AsyncMock(side_effect=lambda _q, hits, **kw: hits[:kw["top_n"]])),
            patch.object(self.service, "chain", RunnableLambda(lambda payload: payload["context"])),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                        base_url="http://structured-rag.invalid", params={"team_id": str(self.team_id)},
                                        headers={"Authorization": f"Bearer {create_access_token(str(self.user_id))}"})
        self.addAsyncCleanup(self.client.aclose)
        # Keep every query confined to the structured evidence under test.
        async with AsyncSession(self.engine) as db:
            await self.service.adelete_document(db, self.brand_id, UUID(self.document_id), 1,
                                                team_id=self.team_id, actor_user_id=self.user_id)
        self.structure, self.assets = document_fixture()

    async def upload(self, *, document_id=None, version=1, structure=None, assets=None):
        structure = copy.deepcopy(self.structure if structure is None else structure)
        assets = self.assets if assets is None else assets
        parsed = {"source": "specifications.pdf", "text": "\n\n".join(item["text"] for item in structure["contents"]),
                  "structured": structure, "assets": assets}
        path = "/api/documents" if document_id is None else f"/api/documents/{document_id}"
        params = {"brand_id": str(self.brand_id)}
        if document_id is not None:
            params["expected_version"] = version
        with patch.object(self.routes, "aload_document_upload", AsyncMock(return_value=parsed)):
            return await self.client.request("POST" if document_id is None else "PUT", path, params=params,
                                             files={"file": ("specifications.pdf", b"retained-original-pdf", "application/pdf")})

    async def get(self, document_id, suffix, **params):
        return await self.client.get(f"/api/documents/{document_id}/{suffix}", params={"brand_id": str(self.brand_id), **params})

    async def query(self):
        return await self.client.post("/api/query", json={"brand_id": str(self.brand_id), "question": "What is the maximum output?"})

    async def test_http_upload_preserves_structure_original_assets_and_content_locations(self):
        response = await self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        document_id = response.json()["id"]
        original = await self.get(document_id, "file")
        self.assertEqual(original.content, b"retained-original-pdf")
        structure = await self.get(document_id, "structure")
        self.assertEqual(structure.status_code, 200, structure.text)
        self.assertEqual(structure.json()["structured"]["contents"], self.structure["contents"])
        evidence = await self.get(document_id, "contents/table-001", expected_version=1)
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertEqual(evidence.json()["page_no"], 2)
        self.assertEqual(evidence.json()["document_version"], 1)
        self.assertEqual(evidence.json()["table"], self.structure["contents"][2]["table"])
        self.assertEqual(evidence.json()["provenance"][0]["coord_origin"], "BOTTOMLEFT")
        asset = await self.get(document_id, "assets/picture-001", expected_version=1)
        self.assertEqual(asset.status_code, 200, asset.text)
        self.assertEqual(asset.content, PNG)
        self.assertEqual(asset.headers["content-type"], "image/png")
        self.assertEqual((await self.get(document_id, "contents/absent")).status_code, 404)
        self.assertEqual((await self.get(document_id, "assets/absent")).status_code, 404)

    async def test_query_rehydrates_authoritative_evidence_instead_of_vector_text_or_page(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]
        table = self.store.EmbeddingStore
        async with AsyncSession(self.engine) as db, db.begin():
            rows = (await db.execute(select(table.id, table.cmetadata).where(table.cmetadata["document_id"].astext == identifier))).all()
            for row in rows:
                metadata = {**row.cmetadata, "page_no": 999, "page_numbers": [999], "content_id": "forged-evidence"}
                await db.execute(update(table).where(table.id == row.id).values(document="FORGED VECTOR BODY", cmetadata=metadata))
        response = await self.query()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["sources"])
        self.assertNotIn("FORGED VECTOR BODY", payload["answer"])
        self.assertNotIn("页码：999", payload["answer"])
        for source in payload["sources"]:
            self.assertEqual(source["document_id"], identifier)
            self.assertEqual(source["document_version"], 1)
            self.assertIn(source["page_no"], [1, 2])
            self.assertNotEqual(source["content_id"], "forged-evidence")
            self.assertTrue(source["evidence"])
            for evidence in source["evidence"]:
                returned = await self.get(identifier, f"contents/{evidence['content_id']}", expected_version=1)
                self.assertEqual(returned.status_code, 200, returned.text)
                self.assertEqual(returned.json(), evidence)

    async def test_assets_structure_and_vectors_roll_back_together_after_sql_failure(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]
        before = (await self.get(identifier, "structure")).json()
        replacement = copy.deepcopy(self.structure)
        replacement["contents"][1]["text"] = "REPLACEMENT OUTPUT 999W"
        original = self.service.awrite_document_chunks

        async def fail_after_vectors(db, *args, **kwargs):
            await original(db, *args, **kwargs)
            from sqlalchemy import text
            await db.execute(text("SELECT 1 / 0"))

        with patch.object(self.service, "awrite_document_chunks", side_effect=fail_after_vectors):
            response = await self.upload(document_id=identifier, structure=replacement,
                                         assets=[{**self.assets[0], "data": b"replacement-asset"}])
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual((await self.get(identifier, "structure")).json(), before)
        self.assertEqual((await self.get(identifier, "assets/picture-001", expected_version=1)).content, PNG)
        result = await self.query()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn("REPLACEMENT OUTPUT", result.json()["answer"])

    async def test_replacement_rejects_old_evidence_versions_and_delete_removes_assets(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]
        replacement = copy.deepcopy(self.structure)
        replacement["contents"] = replacement["contents"][:2]
        updated = await self.upload(document_id=identifier, structure=replacement, assets=[])
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["version"], 2)
        self.assertEqual((await self.get(identifier, "contents/text-001", expected_version=1)).status_code, 409)
        self.assertEqual((await self.get(identifier, "file", expected_version=1)).status_code, 409)
        self.assertEqual((await self.get(identifier, "assets/picture-001", expected_version=1)).status_code, 404)
        self.assertEqual((await self.get(identifier, "assets/picture-001", expected_version=2)).status_code, 404)
        removed = await self.client.delete(f"/api/documents/{identifier}", params={"brand_id": str(self.brand_id), "expected_version": 2})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual((await self.get(identifier, "structure")).status_code, 404)
        async with AsyncSession(self.engine) as db:
            self.assertFalse(list((await db.scalars(select(KnowledgeAsset).where(KnowledgeAsset.document_id == UUID(identifier)))).all()))

    async def test_table_applicability_caption_reaches_embedding_and_answer_context(self):
        table = copy.deepcopy(self.structure["contents"][2])
        table["caption"] = "Measurements at 240 V AC only; values do not apply at 120 V AC."
        self.structure["contents"] = [table]
        with patch.object(self.embeddings, "aembed_documents", wraps=self.embeddings.aembed_documents) as embed:
            uploaded = await self.upload(assets=[])
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]
        embed.assert_awaited_once()
        embedding_texts = embed.await_args.args[0]
        self.assertEqual(len(embedding_texts), 1)
        self.assertIn(table["caption"], embedding_texts[0])
        self.assertIn(table["markdown"], embedding_texts[0])
        response = await self.query()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(table["caption"], response.json()["answer"])
        self.assertTrue(response.json()["sources"])
        for source in response.json()["sources"]:
            self.assertEqual(source["caption"], table["caption"])
            self.assertEqual(source["content"], table["markdown"])
            self.assertEqual(source["evidence"][0]["caption"], table["caption"])
        original = await self.get(identifier, "contents/table-001", expected_version=1)
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(original.json()["markdown"], table["markdown"])
        self.assertEqual(original.json()["table"], table["table"])

    async def test_retrieval_loads_structure_without_original_pdf_or_full_body(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]
        hits = [(Document(page_content="candidate", metadata={
            "team_id": str(self.team_id), "brand_id": str(self.brand_id), "document_id": identifier,
        }), 0.1)]
        statement = self.service._visible_hit_statement(hits, team_id=self.team_id, brand_id=self.brand_id)
        sql = str(statement.compile(dialect=self.engine.dialect))
        self.assertNotIn("knowledge_documents.file_bytes", sql)
        self.assertNotIn("knowledge_documents.body", sql)
        self.assertIn("knowledge_documents.structured", sql)
        async with AsyncSession(self.engine) as db:
            record = (await db.scalars(statement)).one()
            self.assertEqual(record.id, UUID(identifier))
            self.assertEqual(record.version, 1)
            self.assertTrue(record.structured["retrieval_units"])
            self.assertIn("file_bytes", inspect(record).unloaded)
            self.assertIn("body", inspect(record).unloaded)

    async def test_rerank_rejects_replaced_evidence_before_generation(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]

        async def replace_during_rerank(_question, candidates, *, top_n):
            self.assertTrue(candidates, "The race must start after retrieving version 1")
            self.assertTrue(all(doc.metadata["document_version"] == 1 for doc, _ in candidates))
            replacement = copy.deepcopy(self.structure)
            replacement["contents"][1]["text"] = "Updated maximum output is 90 watts."
            updated = await self.upload(document_id=identifier, structure=replacement)
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["version"], 2)
            return candidates[:top_n]

        rerank = AsyncMock(side_effect=replace_during_rerank)
        generate = AsyncMock(return_value="An answer must never be generated from superseded evidence.")
        with patch.object(self.service, "arerank_documents", rerank), patch.object(
            self.service, "chain", types.SimpleNamespace(ainvoke=generate)
        ):
            response = await self.query()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("更新或删除", response.json()["detail"])
        rerank.assert_awaited_once()
        generate.assert_not_awaited()
        current = await self.get(identifier, "contents/text-001", expected_version=2)
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["text"], "Updated maximum output is 90 watts.")

    async def test_generation_rejects_evidence_replaced_during_model_call(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]

        async def generate(_payload):
            replacement = copy.deepcopy(self.structure)
            replacement["contents"][1]["text"] = "Updated maximum output is 90 watts."
            updated = await self.upload(document_id=identifier, structure=replacement)
            self.assertEqual(updated.status_code, 200, updated.text)
            return "A stale answer based on the old version."

        with patch.object(self.service, "chain", types.SimpleNamespace(ainvoke=AsyncMock(side_effect=generate))):
            response = await self.query()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("更新或删除", response.json()["detail"])

    async def test_outdated_vector_version_never_reaches_generation(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]
        table = self.store.EmbeddingStore
        async with AsyncSession(self.engine) as db, db.begin():
            rows = (await db.execute(select(table.id, table.cmetadata).where(table.cmetadata["document_id"].astext == identifier))).all()
            for row in rows:
                await db.execute(update(table).where(table.id == row.id).values(cmetadata={**row.cmetadata, "document_version": 0}))
        generate = AsyncMock(side_effect=AssertionError("Stale evidence must not be generated"))
        with patch.object(self.service, "chain", types.SimpleNamespace(ainvoke=generate)):
            response = await self.query()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sources"], [])
        generate.assert_not_awaited()

    async def test_another_team_member_cannot_read_structure_content_or_assets(self):
        uploaded = await self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        identifier = uploaded.json()["id"]
        foreign_team, foreign_brand = uuid4(), uuid4()
        async with AsyncSession(self.engine) as db, db.begin():
            db.add(atomicity.Team(id=foreign_team, name="Foreign structured team"))
            await db.flush()
            db.add(atomicity.TeamMember(team_id=foreign_team, user_id=self.user_id, role="employee", status="active"))
            db.add(atomicity.Brand(id=foreign_brand, team_id=foreign_team, name="Foreign structured brand"))
        try:
            for suffix in ("structure", "contents/text-001", "assets/picture-001"):
                response = await self.client.get(f"/api/documents/{identifier}/{suffix}",
                                                 params={"brand_id": str(foreign_brand), "team_id": str(foreign_team)})
                self.assertEqual(response.status_code, 404, response.text)
        finally:
            async with AsyncSession(self.engine) as db, db.begin():
                await db.execute(delete(atomicity.Brand).where(atomicity.Brand.id == foreign_brand))
                await db.execute(delete(atomicity.TeamMember).where(atomicity.TeamMember.team_id == foreign_team))
                await db.execute(delete(atomicity.Team).where(atomicity.Team.id == foreign_team))


if __name__ == "__main__":
    unittest.main()
