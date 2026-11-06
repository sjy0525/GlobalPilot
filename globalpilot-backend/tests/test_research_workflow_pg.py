"""Opt-in research integration against disposable PostgreSQL and pgvector.

Set GLOBALPILOT_RESEARCH_TEST_DATABASE_URL to a PostgreSQL database whose name
starts with test_ or audit_. SQL, document/vector writes, RAG filtering,
PostgresSaver checkpoints, workflow nodes and brief projections are real.
Embeddings, reranking, chat responses, web search and audit are local fixtures.
The application config/.env and provider clients are never imported.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy  # Load native dependencies before temporary sys.modules isolation.
import langchain_text_splitters
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langchain_postgres import PGVector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.briefs.models import Brief
from app.modules.rag.models import KnowledgeAsset, KnowledgeDocument
from app.modules.users.models import User
from app.modules.teams.models import Team, TeamMember
from app.modules.marketing.schemas.copy import COPY_VERSION
from app.modules.marketing.schemas.marketing_workflow import MarketingWorkflowReviewInput


DATABASE_URL = os.environ.get("GLOBALPILOT_RESEARCH_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
BRAND_BODY = "Our product is blue. This catalog belongs to Brand Alpha."
FOREIGN_BODY = "FOREIGN BRAND SECRET: our product is red."
WEB_URL = "https://library.example.test/charger-loans"
WEB_QUOTE = "The library offers USB-C charger loans."
RESEARCH_SUMMARY = "该校图书馆提供USB-C充电器借用服务。"
RAW_ISSUE = f"UNTRUSTED_ISSUE: {RESEARCH_SUMMARY} {WEB_URL}"
STRATEGY = "Use verified blue product facts for the requested audience."
CONTENT = {"platforms": [{
    "platform": "linkedin", "hook": "Blue product", "body": "Our product is blue.",
    "cta": "Learn more", "tags": "#brand #blue",
}]}


def fake_module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def load_source(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LocalEmbeddings(Embeddings):
    """Constant vectors test actual SQL/brand isolation, not retrieval quality."""

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


class ToolCallingLocalModel(FakeMessagesListChatModel):
    calls: int = 0

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        self.calls += 1
        return super()._generate(*args, **kwargs)


@unittest.skipUnless(DATABASE_URL, "GLOBALPILOT_RESEARCH_TEST_DATABASE_URL is not set")
class ResearchWorkflowPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(DATABASE_URL)
        if url.get_backend_name() != "postgresql" or not (url.database or "").startswith(("test_", "audit_")):
            raise AssertionError("Refusing research integration tests against a non-test database")
        url = url.set(drivername="postgresql+psycopg")
        cls.sqlalchemy_url = url.render_as_string(hide_password=False)
        cls.engine = create_engine(url)
        cls.addClassCleanup(cls.engine.dispose)
        cls.sessions = sessionmaker(cls.engine, autoflush=False)
        Base.metadata.create_all(cls.engine, tables=[
            User.__table__, Team.__table__, TeamMember.__table__, Brand.__table__, Brief.__table__, KnowledgeDocument.__table__, KnowledgeAsset.__table__,
        ])
        cls.prompts = []
        cls.embeddings = LocalEmbeddings()
        settings = types.SimpleNamespace(
            database_url=cls.sqlalchemy_url, rag_chunk_size=500, rag_chunk_overlap=80,
            rag_fetch_k=20, rag_recall_k=10, rag_search_k=3, rag_max_distance=0.64,
        )
        # All config/provider boundaries are isolated before loading app sources.
        llm_module = fake_module("app.infra.ai.llm", llm=ToolCallingLocalModel(
            responses=[AIMessage(content="unused default agent")],
        ))

        @tool
        def unused_search(query: str) -> dict:
            """Reject accidental use of the default external-search boundary."""
            raise AssertionError("Only an explicitly injected offline search is allowed")

        async def async_rerank(question, candidates, top_n):
            return candidates[:top_n]

        cls.modules = patch.dict(sys.modules, {
            "app.core.config": fake_module("app.core.config", settings=settings),
            "app.db.database": fake_module("app.db.database", async_engine=None, sessionLocal=cls.sessions, get_db=lambda: None),
            "app.infra.ai.embeddings": fake_module("app.infra.ai.embeddings", embeddings=cls.embeddings),
            "app.infra.ai.llm": llm_module,
            "app.modules.rag.retrieval.rerank": fake_module(
                "app.modules.rag.retrieval.rerank", arerank_documents=async_rerank,
                rerank_documents=lambda question, candidates, top_n: candidates[:top_n],
            ),
            "app.modules.marketing.workflow.research_tools": fake_module(
                "app.modules.marketing.workflow.research_tools", search_web=unused_search,
                ResearchSearchError=type("OfflineSearchError", (RuntimeError,), {}),
            ),
        })
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)

        cls.execution = load_source("_research_pg_execution", "app/modules/marketing/workflow/execution.py")
        sys.modules["app.modules.marketing.workflow.execution"] = cls.execution
        cls.checkpoint_module = load_source("_research_pg_checkpointer", "app/modules/marketing/workflow/checkpointer.py")
        cls.pool = ConnectionPool(
            cls.execution.to_psycopg_conninfo(cls.sqlalchemy_url), min_size=1, max_size=4,
            open=True, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        cls.addClassCleanup(cls.pool.close)
        cls.pool.wait(timeout=10)
        cls.saver = cls.checkpoint_module.FencedPostgresSaver(cls.pool)
        cls.saver.setup()
        cls.checkpoint_module.checkpointer = cls.saver
        sys.modules["app.modules.marketing.workflow.checkpointer"] = cls.checkpoint_module

        cls.store = PGVector(
            connection=cls.engine, embeddings=cls.embeddings,
            collection_name=f"research_workflow_tests_{uuid4().hex}", use_jsonb=True,
        )
        cls.addClassCleanup(cls.store.delete_collection)
        cls.vector = load_source("_research_pg_vector", "app/modules/rag/retrieval/vector_store.py")
        cls.vector.vector_store._store = cls.store
        sys.modules["app.modules.rag.retrieval.vector_store"] = cls.vector
        splitter = load_source("_research_pg_splitter", "app/modules/rag/processing/splitter.py")
        sys.modules["app.modules.rag.processing.splitter"] = splitter
        cls.rag = load_source("_research_pg_rag", "app/modules/rag/service.py")
        sys.modules["app.modules.rag.service"] = cls.rag
        cls.agent_module = load_source("_research_pg_agent", "app/modules/marketing/workflow/research_agent.py")
        sys.modules["app.modules.marketing.workflow.research_agent"] = cls.agent_module

        def marketing_model(value, **kwargs):
            text = value.to_string()
            cls.prompts.append(text)
            if "独立的文案审查员" in text:
                return AIMessage(content=json.dumps({"facts": [{
                    "claim": "Our product is blue.", "verdict": "supported",
                    "evidence": "Our product is blue.", "suggestion": "",
                }]}))
            if "跨境社媒文案" in text:
                return AIMessage(content=json.dumps(CONTENT))
            return AIMessage(content=STRATEGY)

        llm_module.llm = RunnableLambda(marketing_model)
        review = load_source("_research_pg_review", "app/modules/marketing/workflow/self_review.py")
        sys.modules["app.modules.marketing.workflow.self_review"] = review
        cls.nodes = load_source("_research_pg_nodes", "app/modules/marketing/workflow/nodes.py")
        sys.modules["app.modules.marketing.workflow.nodes"] = cls.nodes
        cls.graph_module = load_source("_research_pg_graph", "app/modules/marketing/workflow/graph.py")
        sys.modules["app.modules.marketing.workflow.graph"] = cls.graph_module
        cls.service = load_source("_research_pg_service", "app/modules/marketing/service.py")
        cls.service.workflow_execution = lambda brief_id: cls.execution.workflow_execution(
            brief_id, database_url=cls.sqlalchemy_url,
        )

    def setUp(self):
        type(self).prompts.clear()
        self.search_queries = []
        self.audit_passed = True
        self.audit_global_issue = False
        self.user_id, self.brand_id, self.foreign_brand_id = uuid4(), uuid4(), uuid4()
        self.team_id = uuid4()
        self.brief_id = uuid4()
        self.config = {"configurable": {"thread_id": str(self.brief_id)}}
        self.payload = {
            "brief_id": self.brief_id, "brand_id": self.brand_id,
            "team_id": self.team_id, "actor_user_id": self.user_id,
            "goal": "Find one university library offering USB-C charger loans.",
            "selling_points": "blue", "region": "test-region", "user_persona": "campus-buyers",
            "social_media": ["linkedin"], "purchase_channels": "official-store", "competitor_url": None,
        }
        with self.sessions.begin() as db:
            db.add(User(id=self.user_id, email=f"{self.user_id}@example.invalid"))
            db.add(Team(id=self.team_id, name="Research team"))
            db.flush()
            db.add(TeamMember(team_id=self.team_id, user_id=self.user_id, role="employee", status="active"))
            db.add_all([Brand(id=value, team_id=self.team_id, name=f"research-test-{value}") for value in (
                self.brand_id, self.foreign_brand_id,
            )])
            db.flush()
            db.add(Brief(id=self.brief_id, user_id=self.user_id, **{
                key: value for key, value in self.payload.items() if key not in {"brief_id", "actor_user_id"}
            }))
        self.addCleanup(self.clear_rows)
        with self.sessions() as db:
            self.document = self.rag.index_text(
                db, self.brand_id, BRAND_BODY, "brand-alpha.txt", file_bytes=BRAND_BODY.encode(),
            team_id=self.team_id, actor_user_id=self.user_id,)
            self.foreign_document = self.rag.index_text(
                db, self.foreign_brand_id, FOREIGN_BODY, "brand-beta.txt", file_bytes=FOREIGN_BODY.encode(),
            team_id=self.team_id, actor_user_id=self.user_id,)

        @tool
        def search_web(query: str) -> dict:
            """Return a deterministic university-library search receipt."""
            self.search_queries.append(query)
            return {"query": query, "results": [{
                "title": "Library equipment loans", "url": WEB_URL, "content": WEB_QUOTE,
            }]}

        def reviewer(payload):
            return {
                "task_answered": self.audit_passed,
                "checks": [{
                    "index": sentence["index"], "verdict": "supported", "source": "web",
                    "source_url": WEB_URL, "quote": WEB_QUOTE, "reason": "原文支持借用服务。",
                } for sentence in payload["sentences"]],
                "issues": [RAW_ISSUE] if self.audit_global_issue else [],
                "limitations": [RAW_ISSUE] if not self.audit_passed and not self.audit_global_issue else [],
            }

        self.research_model = ToolCallingLocalModel(responses=[
            AIMessage(content="", tool_calls=[{
                "name": "search_web", "args": {"query": "university library USB-C charger loans"},
                "id": "search-1",
            }]),
            AIMessage(content="", tool_calls=[{
                "name": "ResearchReport", "id": "report-1", "args": {
                    "status": "completed", "summary": RESEARCH_SUMMARY, "source_urls": [WEB_URL],
                    "uncertainties": ["仅适用于这所学校。"],
                },
            }]),
        ])
        self.nodes.research_agent = self.agent_module.build_research_agent(
            model=self.research_model, search_tool=search_web, reviewer=reviewer,
        )
        self.graph = self.graph_module.builder.compile(checkpointer=self.saver)
        self.service.graph = self.graph

    def clear_rows(self):
        with self.execution.workflow_execution(self.brief_id, database_url=self.sqlalchemy_url):
            self.saver.delete_thread(str(self.brief_id))
        with self.sessions.begin() as db:
            db.execute(delete(self.store.EmbeddingStore).where(
                self.store.EmbeddingStore.cmetadata["brand_id"].astext.in_(
                    [str(self.brand_id), str(self.foreign_brand_id)],
                ),
            ))
            db.execute(delete(Brief).where(Brief.id == self.brief_id))
            db.execute(delete(KnowledgeDocument).where(
                KnowledgeDocument.brand_id.in_([self.brand_id, self.foreign_brand_id]),
            ))
            db.execute(delete(Brand).where(Brand.id.in_([self.brand_id, self.foreign_brand_id])))
            db.execute(delete(TeamMember).where(TeamMember.team_id == self.team_id))
            db.execute(delete(Team).where(Team.id == self.team_id))
            db.execute(delete(User).where(User.id == self.user_id))

    def strategy_prompt(self):
        return next(text for text in reversed(self.prompts) if "跨境营销策略顾问" in text)

    @staticmethod
    def prompt_research(text):
        return json.JSONDecoder().raw_decode(text.split("调研报告（JSON）：", 1)[1].lstrip())[0]

    def test_real_document_indexing_and_rag_are_brand_isolated(self):
        own = self.rag.search_documents("product information", self.brand_id, team_id=self.team_id, actor_user_id=self.user_id)
        foreign = self.rag.search_documents("product information", self.foreign_brand_id, team_id=self.team_id, actor_user_id=self.user_id)
        self.assertEqual([doc.page_content for doc, _ in own], [BRAND_BODY])
        self.assertEqual([doc.page_content for doc, _ in foreign], [FOREIGN_BODY])
        self.assertEqual(own[0][0].metadata["document_id"], self.document["id"])
        with self.sessions() as db:
            record = db.get(KnowledgeDocument, self.document["id"])
            self.assertEqual(record.body, BRAND_BODY)
            self.assertEqual(record.file_bytes, BRAND_BODY.encode())
            self.assertEqual(record.chunk_count, 1)
            count = db.scalar(select(func.count()).select_from(self.store.EmbeddingStore).where(
                self.store.EmbeddingStore.cmetadata["brand_id"].astext == str(self.brand_id),
            ))
            self.assertEqual(count, 1)

    def test_research_reload_revision_and_brief_projection(self):
        initial = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(initial.status, "awaiting_review")
        snapshot = self.graph.get_state(self.config)
        self.assertEqual(snapshot.values["research"]["summary"], RESEARCH_SUMMARY)
        self.assertEqual(snapshot.values["research"]["source_urls"], [WEB_URL])
        self.assertTrue(snapshot.values["research_validation"]["passed"])
        self.assertIn(BRAND_BODY, snapshot.values["retrieved_context"])
        self.assertNotIn(FOREIGN_BODY, snapshot.values["retrieved_context"])
        prompt = self.strategy_prompt()
        for value in (self.payload["goal"], "blue", "test-region", "campus-buyers", "linkedin", "official-store"):
            self.assertIn(value, prompt)
        self.assertEqual(self.prompt_research(prompt)["summary"], RESEARCH_SUMMARY)
        self.assertIn(WEB_URL, prompt)
        self.assertNotIn(FOREIGN_BODY, prompt)

        # A new graph and saver read the persisted PostgreSQL checkpoint.
        fresh_saver = self.checkpoint_module.FencedPostgresSaver(self.pool)
        fresh_graph = self.graph_module.builder.compile(checkpointer=fresh_saver)
        reloaded = fresh_graph.get_state(self.config)
        self.assertEqual(reloaded.values["research"], snapshot.values["research"])
        self.assertEqual(reloaded.values["research_validation"], snapshot.values["research_validation"])
        self.service.graph = fresh_graph
        self.assertEqual(self.service.start(self.user_id, self.brief_id, team_id=self.team_id).checkpoint_id, initial.checkpoint_id)
        revised = self.service.review(self.user_id, MarketingWorkflowReviewInput(
            brief_id=self.brief_id, expected_checkpoint_id=initial.checkpoint_id,
            action="rejected", feedback="Tighten the hook",
        ), team_id=self.team_id)
        self.assertEqual(revised.revision_count, 1)
        self.assertEqual(len(self.search_queries), 1)
        self.assertEqual(self.research_model.calls, 2)
        self.assertEqual(fresh_graph.get_state(self.config).values["research"], snapshot.values["research"])
        final = self.service.review(self.user_id, MarketingWorkflowReviewInput(
            brief_id=self.brief_id, expected_checkpoint_id=revised.checkpoint_id, action="approved",
        ), team_id=self.team_id)
        with self.sessions() as db:
            brief = db.get(Brief, self.brief_id)
            self.assertEqual(brief.workflow_status, "completed")
            self.assertEqual(brief.workflow_checkpoint_id, final.checkpoint_id)
            self.assertEqual(brief.strategy, final.strategy)
            self.assertEqual(brief.strategy, fresh_graph.get_state(self.config).values["strategy"])
            self.assertTrue(brief.strategy.startswith(STRATEGY))
            self.assertIn(WEB_URL, brief.strategy)
            self.assertEqual(brief.content["version"], COPY_VERSION)
            self.assertIn("Our product is blue.", brief.final_output)
            self.assertGreaterEqual(brief.workflow_version, 3)

    def test_research_checkpoint_resumes_without_researching_again(self):
        interrupted = self.graph_module.builder.compile(
            checkpointer=self.saver, interrupt_before=["generate_strategy"],
        )
        with self.execution.workflow_execution(self.brief_id, database_url=self.sqlalchemy_url):
            interrupted.invoke(self.payload, self.config)
        stopped = interrupted.get_state(self.config)
        self.assertEqual(stopped.next, ("generate_strategy",))
        self.assertEqual(stopped.values["research"]["summary"], RESEARCH_SUMMARY)
        self.assertFalse(any("跨境营销策略顾问" in text for text in self.prompts))
        self.service.graph = self.graph_module.builder.compile(
            checkpointer=self.checkpoint_module.FencedPostgresSaver(self.pool),
        )
        resumed = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(resumed.status, "awaiting_review")
        self.assertEqual(len(self.search_queries), 1)
        self.assertEqual(self.research_model.calls, 2)
        self.assertEqual(self.service.graph.get_state(self.config).values["research"], stopped.values["research"])

    def test_empty_brand_context_does_not_research(self):
        with self.sessions.begin() as db:
            db.execute(delete(self.store.EmbeddingStore).where(
                self.store.EmbeddingStore.cmetadata["brand_id"].astext == str(self.brand_id),
            ))
        response = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(response.status, "no_knowledge")
        self.assertEqual(self.search_queries, [])
        self.assertEqual(self.research_model.calls, 0)
        self.assertFalse(self.prompts)
        self.assertNotIn("research", self.graph.get_state(self.config).values)

    def test_unpassed_research_remains_in_checkpoint_but_not_strategy_evidence(self):
        self.audit_passed = False
        response = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(response.status, "awaiting_review")
        snapshot = self.graph.get_state(self.config)
        self.assertFalse(snapshot.values["research_validation"]["passed"])
        self.assertEqual(snapshot.values["research"]["summary"], RESEARCH_SUMMARY)
        self.assertEqual(snapshot.values["research"]["source_urls"], [WEB_URL])
        prompt = self.strategy_prompt()
        self.assertNotIn(RESEARCH_SUMMARY, prompt)
        self.assertNotIn(WEB_URL, prompt)
        self.assertNotIn(RAW_ISSUE, prompt)
        safe_report = self.prompt_research(prompt)
        self.assertFalse(safe_report.get("source_urls"))
        self.assertIn(BRAND_BODY, prompt)

    def test_global_audit_issue_discards_summary_and_sources_before_checkpoint(self):
        self.audit_global_issue = True
        response = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(response.status, "awaiting_review")
        snapshot = self.graph.get_state(self.config)
        validation = snapshot.values["research_validation"]
        self.assertFalse(validation["passed"])
        self.assertTrue(validation["globally_blocked"])
        self.assertEqual(validation["rejected_indices"], [0])
        self.assertIn(RAW_ISSUE, validation["issues"])
        self.assertNotIn(RESEARCH_SUMMARY, snapshot.values["research"]["summary"])
        self.assertEqual(snapshot.values["research"]["source_urls"], [])
        prompt = self.strategy_prompt()
        for rejected_value in (RESEARCH_SUMMARY, WEB_URL, RAW_ISSUE):
            self.assertNotIn(rejected_value, prompt)


if __name__ == "__main__":
    unittest.main()
