"""Opt-in real service/graph/PostgreSQL regressions, with deterministic models.

WORKFLOW_TEST_DATABASE_URL must point to a disposable test database. No test
uses the application's configured business database or calls a model provider.
"""
import importlib.util
import json
import os
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.briefs.models import Brief
from app.modules.users.models import User
from app.modules.teams.models import Team, TeamMember
from app.modules.memory.models import MemoryJob  # noqa: F401 -- register feedback-job DDL before fixture module isolation
from app.modules.marketing.schemas.marketing_workflow import MarketingWorkflowReviewInput
from app.modules.marketing.workflow.checkpointer import FencedPostgresSaver
from app.modules.marketing.workflow.execution import to_psycopg_conninfo, workflow_execution
from tests import test_self_review as review_fixture

URL = os.environ.get("WORKFLOW_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(URL, "WORKFLOW_TEST_DATABASE_URL is not set")
class WorkflowServiceConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(URL)
        cls.sessions = sessionmaker(cls.engine, autoflush=False)
        Base.metadata.create_all(cls.engine)
        cls.pool = ConnectionPool(to_psycopg_conninfo(URL), open=True, kwargs={
            "autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
        })
        cls.saver = FencedPostgresSaver(cls.pool)
        cls.saver.setup()
        # Use the project's actual nodes, prompts and review parsing with only
        # external retrieval/model responses replaced by the offline fixture.
        review_fixture.GraphTests.setUpClass()
        cls.graph = review_fixture.GraphTests.graph_module.builder.compile(checkpointer=cls.saver)
        cls.service = load_module("_pg_workflow_service", "app/modules/marketing/service.py")
        cls.service.graph = cls.graph
        cls.service.workflow_execution = lambda brief_id: workflow_execution(brief_id, database_url=URL)

    @classmethod
    def tearDownClass(cls):
        review_fixture.GraphTests.tearDownClass()
        cls.pool.close()
        cls.engine.dispose()

    def setUp(self):
        self.fixture = review_fixture.GraphTests()
        self.fixture.setUp()
        self.user_id = uuid4()
        self.team_id = self.fixture.input["team_id"]
        self.fixture.input["actor_user_id"] = self.user_id
        self.brief_id = self.fixture.input["brief_id"]
        self.config = {"configurable": {"thread_id": str(self.brief_id)}}
        with self.sessions.begin() as db:
            db.add(User(id=self.user_id, email=f"{self.user_id}@example.invalid"))
            db.add(Team(id=self.team_id, name="Workflow team"))
            db.flush()
            db.add(TeamMember(team_id=self.team_id, user_id=self.user_id, role="employee", status="active"))
            db.add(Brand(id=self.fixture.input["brand_id"], team_id=self.team_id, name=str(uuid4())))
            db.flush()
            db.add(Brief(id=self.brief_id, user_id=self.user_id, **{
                k: v for k, v in self.fixture.input.items() if k not in {"brief_id", "actor_user_id"}
            }))

    def review_input(self, response, action="rejected", feedback="Please revise"):
        return MarketingWorkflowReviewInput(
            brief_id=self.brief_id, expected_checkpoint_id=response.checkpoint_id,
            action=action, feedback=feedback if action == "rejected" else None,
        )

    def test_simultaneous_start_has_one_executor(self):
        entered, release = threading.Event(), threading.Event()
        invoke = self.graph.invoke

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test executor was not released")
            return invoke(*args, **kwargs)

        with patch.object(self.graph, "invoke", side_effect=delayed), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.service.start, self.user_id, self.brief_id, team_id=self.team_id)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(HTTPException) as caught:
                    self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
                self.assertEqual(caught.exception.status_code, 409)
            finally:
                release.set()
            self.assertEqual(first.result(10).status, "awaiting_review")

    def test_simultaneous_review_does_not_lose_a_successful_feedback(self):
        initial = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        entered, release = threading.Event(), threading.Event()
        invoke = self.graph.invoke

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test reviewer was not released")
            return invoke(*args, **kwargs)

        with patch.object(self.graph, "invoke", side_effect=delayed), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.service.review, self.user_id, self.review_input(initial, feedback="Feedback A"), team_id=self.team_id)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(HTTPException) as caught:
                    self.service.review(self.user_id, self.review_input(initial, feedback="Feedback B"), team_id=self.team_id)
                self.assertEqual(caught.exception.status_code, 409)
            finally:
                release.set()
            response = first.result(10)
        self.assertEqual(response.revision_count, 1)
        self.assertEqual(self.graph.get_state(self.config).values["feedback_log"], ["Feedback A"])

    def test_old_page_cannot_approve_a_new_draft(self):
        initial = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        revised = self.service.review(self.user_id, self.review_input(initial), team_id=self.team_id)
        self.assertNotEqual(initial.checkpoint_id, revised.checkpoint_id)
        with self.assertRaises(HTTPException) as caught:
            self.service.review(self.user_id, self.review_input(initial, "approved"), team_id=self.team_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.graph.get_state(self.config).interrupts)

    def test_stale_snapshot_cannot_regress_completed_projection(self):
        initial = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        stale = self.graph.get_state(self.config)
        self.service.review(self.user_id, self.review_input(initial, "approved"), team_id=self.team_id)
        with workflow_execution(self.brief_id, database_url=URL) as guard:
            brief = self.service._load_execution_brief(guard, self.user_id, team_id=self.team_id)
            with self.assertRaises(HTTPException) as caught:
                self.service.persist_snapshot(brief, stale)
            self.assertEqual(caught.exception.status_code, 409)
        with self.sessions() as db:
            brief = db.get(Brief, self.brief_id)
            self.assertEqual(brief.workflow_status, "completed")
            self.assertIsNotNone(brief.final_output)

    def test_projection_compare_and_swap_rejects_an_outdated_version(self):
        self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        with workflow_execution(self.brief_id, database_url=URL) as guard:
            brief = self.service._load_execution_brief(guard, self.user_id, team_id=self.team_id)
            snapshot = self.graph.get_state(self.config)
            with guard.transaction() as cursor:
                cursor.execute("UPDATE briefs SET workflow_version = workflow_version + 1 WHERE id = %s", (self.brief_id,))
            with self.assertRaises(HTTPException) as caught:
                self.service.persist_snapshot(brief, snapshot)
            self.assertEqual(caught.exception.status_code, 409)

    def test_saved_decision_is_recovered_instead_of_accepting_a_different_resume(self):
        initial = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        original = self.saver.put
        def fail_after_review(config, checkpoint, metadata, versions):
            values = checkpoint["channel_values"]
            if values.get("decision") == "approved":
                raise RuntimeError("Simulated checkpoint outage after review task result")
            return original(config, checkpoint, metadata, versions)

        with patch.object(self.saver, "put", side_effect=fail_after_review):
            with self.assertRaises(RuntimeError):
                self.service.review(self.user_id, self.review_input(initial, "approved"), team_id=self.team_id)
        interrupted = self.graph.get_state(self.config)
        # Latest-state reads omit answered interrupts once the task result is
        # saved. The explicit checkpoint retains the original question, while
        # its pending writes prove the approval already ran successfully.
        historical = self.graph.get_state(interrupted.config)
        self.assertTrue(historical.interrupts)
        saved_reviews = [task for task in interrupted.tasks if task.name == "human_review"]
        self.assertEqual(len(saved_reviews), 1)
        self.assertIsNotNone(saved_reviews[0].result)
        self.assertEqual(saved_reviews[0].result["decision"], "approved")
        self.assertFalse(self.service.pending_human_review(interrupted))
        with self.assertRaises(HTTPException) as caught:
            self.service.review(self.user_id, self.review_input(initial), team_id=self.team_id)
        self.assertEqual(caught.exception.status_code, 409)
        recovered = self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(self.graph.get_state(self.config).values["decision"], "approved")
        with self.sessions() as db:
            self.assertEqual(db.get(Brief, self.brief_id).workflow_status, "completed")

    def test_partial_stream_close_releases_execution_lock(self):
        iterator = self.service.iter_start_stream(self.user_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(json.loads(next(iterator).splitlines()[0])["type"], "node")
        with self.assertRaises(HTTPException):
            self.service.start(self.user_id, self.brief_id, team_id=self.team_id)
        iterator.close()
        self.assertEqual(self.service.start(self.user_id, self.brief_id, team_id=self.team_id).status, "awaiting_review")

    def test_real_stream_routes_pass_the_seen_checkpoint_and_reject_missing_version(self):
        import sys
        with patch.dict(sys.modules, {"app.modules.marketing.service": self.service}):
            routes = load_module("_pg_workflow_routes", "app/modules/marketing/router.py")
        app = FastAPI()
        app.include_router(routes.router, prefix="/api")
        app.dependency_overrides[routes.get_current_user] = lambda: types.SimpleNamespace(id=self.user_id)
        app.dependency_overrides[routes.get_current_member_from_query] = lambda: types.SimpleNamespace(team_id=self.team_id, user_id=self.user_id)

        def session():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[routes.get_db] = session
        with TestClient(app) as client:
            client.params = {"team_id": str(self.team_id)}
            response = client.post("/api/briefs/workflow/stream", json={"brief_id": str(self.brief_id)})
            self.assertEqual(response.status_code, 200)
            frames = [json.loads(line) for line in response.text.splitlines() if json.loads(line).get("type")]
            workflow = frames[-1]["workflow"]
            self.assertTrue(workflow["checkpoint_id"])
            missing = client.post("/api/briefs/workflow/review", json={"brief_id": str(self.brief_id), "action": "approved"})
            self.assertEqual(missing.status_code, 422)
            approved = client.post("/api/briefs/workflow/review/stream", json={
                "brief_id": str(self.brief_id), "action": "approved",
                "expected_checkpoint_id": workflow["checkpoint_id"],
            })
            frames = [json.loads(line) for line in approved.text.splitlines() if json.loads(line).get("type")]
            self.assertEqual(frames[-1]["workflow"]["status"], "completed")
