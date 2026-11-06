"""Real PostgreSQL and JWT checks for the targeted-revision HTTP lifecycle.

Opt in with GLOBALPILOT_REVISION_TEST_DATABASE_URL naming a disposable test_,
audit_ or refactor_ database. Each class owns a random schema. External models
are replaced at the graph factory; document reads, authorization and storage
remain real. The application's configured business database is never used.
"""

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic_settings import DotEnvSettingsSource
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from tests.test_team_authorization_pg import isolated_database_url


TEST_DATABASE_URL = os.environ.get("GLOBALPILOT_REVISION_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DATABASE_URL, "requires isolated revision PostgreSQL")
class RevisionAPIPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = isolated_database_url(TEST_DATABASE_URL)
        cls.schema = "revision_api_" + uuid4().hex
        cls.engine = create_engine(
            url,
            echo=False,
            connect_args={
                "options": (
                    f"-csearch_path={cls.schema} "
                    "-cstatement_timeout=15000 -clock_timeout=5000"
                ),
            },
        )
        cls.addClassCleanup(cls.engine.dispose)
        with cls.engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{cls.schema}"'))

        def drop_schema():
            with cls.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))

        cls.addClassCleanup(drop_schema)
        cls.test_url = url.update_query_dict({
            "options": f"-csearch_path={cls.schema}",
        }).render_as_string(hide_password=False)
        config = {
            "DATABASE_URL": cls.test_url,
            "DASHSCOPE_API_KEY": "unused-revision-api-test",
            "DASHSCOPE_BASE_URL": "https://unused.invalid/v1",
            "DASHSCOPE_RERANK_API_KEY": "unused-revision-api-test",
            "DEEPSEEK_API_KEY": "unused-revision-api-test",
            "DEEPSEEK_BASE_URL": "http://127.0.0.1:1",
            "JWT_SECRET_KEY": "revision-api-test-secret-" * 4,
            "JWT_ALGORITHM": "HS256",
        }
        cls.scope = ExitStack()
        cls.addClassCleanup(cls.scope.close)
        cls.scope.enter_context(patch.dict(os.environ, config))
        cls.scope.enter_context(patch.object(
            DotEnvSettingsSource, "_read_env_files", return_value={}
        ))

        class ForbiddenProvider:
            def __call__(self, *args, **kwargs):
                raise AssertionError("Only explicitly injected offline models may run")

            def bind(self, **kwargs):
                return self

            def invoke(self, *args, **kwargs):
                raise AssertionError("Only explicitly injected offline models may run")

        cls.scope.enter_context(patch.dict(sys.modules, {
            "app.infra.ai.llm": SimpleNamespace(llm=ForbiddenProvider()),
        }))

        cls.database = importlib.import_module("app.db.database")
        cls.security = importlib.import_module("app.modules.auth.security")
        cls.dependencies = importlib.import_module("app.modules.teams.dependencies")
        cls.settings = importlib.import_module("app.core.config").settings
        cls.errors = importlib.import_module("app.core.http_errors")
        cls.schemas = importlib.import_module("app.modules.marketing.schemas.revision")
        cls.models = importlib.import_module("app.modules.marketing.models")
        cls.service = importlib.import_module("app.modules.marketing.revision_service")
        cls.routes = importlib.import_module("app.modules.marketing.revision_router")
        cls.workflow_graph = importlib.import_module("app.modules.marketing.workflow.revision.graph")
        cls.execution = importlib.import_module("app.modules.marketing.workflow.execution")
        cls.parent_service = importlib.import_module("app.modules.marketing.service")
        cls.parent_graph_module = importlib.import_module("app.modules.marketing.workflow.graph")
        cls.parent_review = importlib.import_module("app.modules.marketing.workflow.self_review")
        cls.copy_module = importlib.import_module("app.modules.marketing.workflow.copy")
        cls.ContentReview = importlib.import_module("app.modules.marketing.schemas.content_review").ContentReview
        cls.User = importlib.import_module("app.modules.users.models").User
        cls.Brand = importlib.import_module("app.modules.brands.models").Brand
        cls.Brief = importlib.import_module("app.modules.briefs.models").Brief
        cls.KnowledgeDocument = importlib.import_module("app.modules.rag.models").KnowledgeDocument
        team_models = importlib.import_module("app.modules.teams.models")
        cls.Team, cls.TeamMember = team_models.Team, team_models.TeamMember

        cls.sessions = sessionmaker(
            bind=cls.engine, autoflush=False, expire_on_commit=False,
        )
        cls.scope.enter_context(patch.object(cls.database, "sessionLocal", cls.sessions))
        cls.scope.enter_context(patch.object(cls.settings, "database_url", cls.test_url))
        cls.scope.enter_context(patch.object(
            cls.settings, "jwt_secret_key", "revision-api-test-secret-" * 4,
        ))
        cls.scope.enter_context(patch.object(cls.settings, "jwt_algorithm", "HS256"))
        cls.scope.enter_context(patch.object(
            cls.service, "workflow_execution",
            lambda brief_id: cls.execution.workflow_execution(
                brief_id, database_url=cls.test_url,
            ),
        ))

        base = importlib.import_module("app.db.base").Base
        with cls.engine.begin() as conn:
            base.metadata.create_all(conn, tables=[
                cls.User.__table__, cls.Team.__table__, cls.TeamMember.__table__,
                cls.Brand.__table__, cls.Brief.__table__, cls.KnowledgeDocument.__table__,
                cls.models.RevisionDraft.__table__, cls.models.RevisionTask.__table__,
                cls.models.RevisionOperation.__table__, cls.models.RevisionSample.__table__,
            ])
        checkpoint_module = importlib.import_module("app.modules.marketing.workflow.checkpointer")
        cls.pool = ConnectionPool(
            cls.execution.to_psycopg_conninfo(cls.test_url), open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        cls.addClassCleanup(cls.pool.close)
        cls.saver = checkpoint_module.FencedPostgresSaver(cls.pool)
        cls.saver.setup()
        cls.parent_graph = cls.parent_graph_module.builder.compile(checkpointer=cls.saver)
        cls.scope.enter_context(patch.object(cls.parent_service, "graph", cls.parent_graph))

    def setUp(self):
        self.actor_id, self.peer_id, self.outsider_id, self.removed_id = [uuid4() for _ in range(4)]
        self.team_id, self.other_team_id = uuid4(), uuid4()
        self.brand_id, self.foreign_brand_id = uuid4(), uuid4()
        self.brief_id, self.peer_brief_id = uuid4(), uuid4()
        self.evidence_id, self.foreign_evidence_id = uuid4(), uuid4()
        self.platform = "linkedin"
        self.original_body = (
            "Supports 65W output for everyday charging.\n\n"
            "Returns within 30 days.\n\n"
            "Keep this closing unchanged."
        )
        self.original_content = {
            "version": "globalpilot-copy-v2",
            "platforms": {
                self.platform: {
                    "hook": "Original hook",
                    "body": self.original_body,
                    "cta": "Original CTA",
                    "tags": "#original #charging",
                },
                "instagram": {
                    "hook": "Other platform",
                    "body": "Other platform must stay unchanged.",
                    "cta": "Other CTA",
                    "tags": "#other #copy",
                },
            },
        }
        with self.sessions.begin() as db:
            db.add_all([
                self.User(id=user_id, email=f"{user_id}@revision-test.invalid", is_active=True)
                for user_id in (self.actor_id, self.peer_id, self.outsider_id, self.removed_id)
            ])
            db.add_all([
                self.Team(id=self.team_id, name="Revision team"),
                self.Team(id=self.other_team_id, name="Foreign team"),
            ])
            db.flush()
            db.add_all([
                self.TeamMember(team_id=self.team_id, user_id=self.actor_id, role="employee", status="active"),
                self.TeamMember(team_id=self.team_id, user_id=self.peer_id, role="admin", status="active"),
                self.TeamMember(team_id=self.team_id, user_id=self.removed_id, role="employee", status="removed"),
                self.TeamMember(team_id=self.other_team_id, user_id=self.outsider_id, role="admin", status="active"),
                self.Brand(id=self.brand_id, team_id=self.team_id, name="Revision brand"),
                self.Brand(id=self.foreign_brand_id, team_id=self.other_team_id, name="Foreign brand"),
            ])
            db.flush()
            for brief_id, owner in ((self.brief_id, self.actor_id), (self.peer_brief_id, self.peer_id)):
                db.add(self.Brief(
                    id=brief_id, team_id=self.team_id, user_id=owner,
                    brand_id=self.brand_id, goal="Clear product copy",
                    selling_points="65W output", region="eu-na", user_persona="buyer",
                    social_media=[self.platform, "instagram"], workflow_status="completed",
                    workflow_version=1, workflow_checkpoint_id="fixture-parent-checkpoint",
                    content=deepcopy(self.original_content),
                ))
            db.add_all([
                self.KnowledgeDocument(
                    id=self.evidence_id, team_id=self.team_id, brand_id=self.brand_id,
                    source="parameters.txt", version=1, chunk_count=0,
                    body="Supports 65W output. Returns within 30 days.",
                ),
                self.KnowledgeDocument(
                    id=self.foreign_evidence_id, team_id=self.other_team_id,
                    brand_id=self.foreign_brand_id, source="foreign.txt", version=1,
                    chunk_count=0, body="Foreign team private evidence.",
                ),
            ])

        for brief_id, owner in ((self.brief_id, self.actor_id), (self.peer_brief_id, self.peer_id)):
            self.seed_parent_review(brief_id, owner)

        self.reviewer_mode = "passed"
        self.reviser_mode = "changed"
        self.model_reviews = []
        self.model_revisions = []
        real_builder = self.workflow_graph.build_revision_graph

        def fake_builder(**kwargs):
            kwargs.update(reviewer=self.fake_reviewer, reviser=self.fake_reviser)
            return real_builder(**kwargs)

        self.factory_patch = patch.object(self.service, "build_revision_graph", side_effect=fake_builder)
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.human_reviewer_patch = patch.object(
            self.service, "invoke_semantic_reviewer", side_effect=self.fake_reviewer,
        )
        self.human_reviewer_patch.start()
        self.addCleanup(self.human_reviewer_patch.stop)
        self.parent_reviews = []

        def parent_review(state):
            self.parent_reviews.append(deepcopy(state["content"]))
            return {
                "self_review": self.passed_parent_report(state["content"]),
                "self_review_attempts": 0,
            }

        self.parent_review_patch = patch.object(
            self.parent_service, "review_targeted_parent_content", side_effect=parent_review,
        )
        self.parent_review_patch.start()
        self.addCleanup(self.parent_review_patch.stop)

        self.app = FastAPI()
        self.errors.install_exception_handlers(self.app)
        self.app.include_router(self.routes.router, prefix="/api")

        def request_session():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[self.database.get_db] = request_session
        self.assertNotIn(self.security.get_current_user, self.app.dependency_overrides)
        self.client = TestClient(self.app, base_url="https://testserver", raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.dependency_overrides.clear)

    def headers(self, user_id=None):
        token = self.security.create_access_token(user_id or self.actor_id)
        return {"Authorization": f"Bearer {token}"}

    def call(self, method, path, *, expected=200, user_id=None, team_id=None, **kwargs):
        params = {"team_id": str(team_id or self.team_id)}
        params.update(kwargs.pop("params", {}))
        response = self.client.request(
            method, "/api" + path,
            params=params,
            headers=self.headers(user_id), **kwargs,
        )
        self.assertEqual(response.status_code, expected, response.text[:600])
        return response

    def passed_parent_report(self, content):
        return self.ContentReview(
            status="passed", facts=[], errors=[],
            content_hash=self.copy_module.content_hash(content),
            rules_version=self.parent_review.RULES_VERSION,
        ).model_dump(mode="json")

    def seed_parent_review(self, brief_id, owner):
        with self.execution.workflow_execution(brief_id, database_url=self.test_url) as guard:
            brief = self.parent_service._load_execution_brief(guard, owner, team_id=self.team_id)
            values = self.parent_service._initial_payload(brief) | {
                "content": deepcopy(self.original_content),
                "context": "Supports 65W output. Returns within 30 days.",
                "retrieved_context": "Supports 65W output. Returns within 30 days.",
                "strategy": "Use only verified parameters",
                "self_review": self.passed_parent_report(self.original_content),
                "self_review_attempts": 0,
                "revision_count": 0,
            }
            config = self.parent_service.graph_config(brief)
            self.parent_graph.update_state(config, values, as_node="self_review")
            self.parent_graph.invoke(None, config)
            snapshot = self.parent_graph.get_state(config)
            self.assertTrue(self.parent_service.pending_human_review(snapshot))
            self.parent_service.persist_snapshot(brief, snapshot)

    def fake_reviewer(self, messages):
        payload = json.loads(messages[1][1])
        self.model_reviews.append(payload)
        facts = []
        evidence = payload.get("evidence", [])
        for block in payload["blocks"]:
            block_facts = []
            for claim in ("Supports 65W output", "Returns within 30 days"):
                if claim in block["candidate_text"]:
                    source = next((item for item in evidence if claim in item["quote"]), None)
                    block_facts.append({
                        "claim": claim,
                        "verdict": "supported" if source else "unsupported",
                        "references": ([{"evidence_id": source["evidence_id"], "quote": claim}] if source else []),
                        "note": "Checked against the supplied frozen evidence",
                    })
            facts.append({
                "block_id": block["block_id"],
                "tone": {"verdict": "met", "note": "Clear and natural"},
                "facts": block_facts,
            })
        report = {
            "goal": {"verdict": "met", "note": "Goal met"},
            "rules": [{"rule_index": item["rule_index"], "verdict": "met", "note": "Rule met"} for item in payload["brand_rules"]],
            "blocks": facts,
            "uncertainties": [],
        }
        if self.reviewer_mode == "needs_revision":
            report["goal"] = {"verdict": "unmet", "note": "Please make another revision"}
        elif self.reviewer_mode == "needs_clarification":
            report["uncertainties"] = ["Please confirm the intended audience"]
        return SimpleNamespace(
            content=json.dumps(report),
            response_metadata={"finish_reason": "stop"},
        )

    def fake_reviser(self, messages):
        payload = json.loads(messages[1][1])
        self.model_revisions.append(payload)
        if self.reviser_mode == "error":
            raise RuntimeError("private model detail")
        blocks = []
        for index, block in enumerate(payload["blocks"]):
            current = block.get("candidate_text") or block["original_text"]
            if self.reviser_mode == "changed" and index == 0:
                current = "Supports 65W output. " + "Clear charging copy. " * len(self.model_revisions)
            blocks.append({"block_id": block["block_id"], "text": current})
        return self.schemas.RevisionRepairOutput.model_validate({"blocks": blocks})

    def document(self, *, brief_id=None):
        return self.call(
            "GET", f"/briefs/{brief_id or self.brief_id}/revisions/document",
            params={"platform": self.platform, "team_id": str(self.team_id)},
        ).json()

    def create_task(self, *, document=None, evidence_ids=None, max_revisions=3, **changes):
        document = document or self.document()
        payload = {
            "platform": self.platform,
            "expected_draft_id": document["draft_id"],
            "expected_draft_version": document["version"],
            "requirements": {
                "goal": "Make the first paragraph clear and concise",
                "allowed_block_ids": [document["blocks"][0]["block_id"]],
                "must_keep": ["65W", "Returns within 30 days"],
            },
            "brand_rules": ["Use clear, natural language"],
            "evidence_document_ids": [str(self.evidence_id)] if evidence_ids is None else evidence_ids,
            "max_revisions": max_revisions,
            "idempotency_key": "create-" + uuid4().hex,
        }
        payload.update(changes)
        return self.call("POST", f"/briefs/{self.brief_id}/revisions", json=payload).json(), payload

    def command(self, task, **fields):
        return {
            "expected_task_version": task["version"],
            "expected_checkpoint_id": task.get("checkpoint_id"),
            "idempotency_key": "operation-" + uuid4().hex,
        } | fields

    def run_task(self, task, *, payload=None, expected=200):
        return self.call(
            "POST", f"/revision-tasks/{task['task_id']}/run",
            expected=expected, json=payload or self.command(task),
        ).json()

    def fail_after_parent_publication(self):
        actual_publish = self.parent_service.apply_targeted_revision_to_parent

        def publish_then_crash(*args, **kwargs):
            actual_publish(*args, **kwargs)
            raise RuntimeError("simulated crash before child ledger commit")

        return patch.object(
            self.parent_service, "apply_targeted_revision_to_parent",
            side_effect=publish_then_crash,
        )

    def request_in_new_process(self, method, path, *, body=None):
        """Run the actual router in a fresh interpreter against the same DB."""
        script = r'''
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

class ForbiddenProvider:
    def bind(self, **kwargs):
        return self
    def invoke(self, *args, **kwargs):
        raise AssertionError("External inference is forbidden in integration tests")
    __call__ = invoke

with patch.dict(sys.modules, {"app.infra.ai.llm": SimpleNamespace(llm=ForbiddenProvider())}):
    from app.db import database
    database.engine.echo = False
    from app.core.http_errors import install_exception_handlers
    from app.modules.marketing import revision_service
    from app.modules.marketing import revision_router
    from app.modules.marketing.schemas import revision as schemas
    from app.modules.marketing.workflow.revision.graph import build_revision_graph
    from tests.test_marketing_revision_api_pg import RevisionAPIPostgresTests

    # Reuse synthetic model behavior without running setUp or creating a schema.
    fixture = RevisionAPIPostgresTests()
    fixture.schemas = schemas
    fixture.reviewer_mode = "passed"
    fixture.reviser_mode = "changed"
    fixture.model_reviews = []
    fixture.model_revisions = []

    def offline_builder(**kwargs):
        kwargs.update(reviewer=fixture.fake_reviewer, reviser=fixture.fake_reviser)
        return build_revision_graph(**kwargs)

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(revision_router.router, prefix="/api")
    request = json.loads(os.environ["REVISION_TEST_REQUEST"])
    with patch.object(revision_service, "build_revision_graph", side_effect=offline_builder), \
         patch.object(revision_service, "invoke_semantic_reviewer", side_effect=fixture.fake_reviewer), \
         TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        response = client.request(
            request["method"], request["path"],
            params={"team_id": request["team_id"]},
            headers={"Authorization": "Bearer " + os.environ["REVISION_TEST_TOKEN"]},
            json=request["body"],
        )
    print(json.dumps({"status_code": response.status_code, "body": response.json()}))
'''
        env = dict(os.environ)
        env["DATABASE_URL"] = self.test_url
        env["REVISION_TEST_TOKEN"] = self.security.create_access_token(self.actor_id)
        env["REVISION_TEST_REQUEST"] = json.dumps({
            "method": method, "path": "/api" + path,
            "team_id": str(self.team_id), "body": body,
        })
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            env=env, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, "Fresh-process API request failed")
        response = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(response["status_code"], 200, response["body"])
        return response["body"]

    def test_document_is_authorized_and_has_stable_block_ids(self):
        document = self.document()
        repeated = self.document()
        self.assertEqual(document, repeated)
        self.assertEqual(len(document["blocks"]), 3)
        self.assertEqual(document["blocks"][1]["text"], "Returns within 30 days.")
        self.call("GET", f"/briefs/{self.brief_id}/revisions/document", user_id=self.peer_id,
                  params={"platform": self.platform}, expected=404)
        self.call("GET", f"/briefs/{self.peer_brief_id}/revisions/document",
                  params={"platform": self.platform}, expected=404)
        self.call("GET", f"/briefs/{self.brief_id}/revisions/document", user_id=self.outsider_id,
                  params={"platform": self.platform}, expected=403)

    def test_create_run_and_reload_uses_real_sources_and_checkpoint(self):
        task, _ = self.create_task()
        self.assertEqual(task["status"], "created")
        run = self.run_task(task)
        self.assertEqual(run["status"], "awaiting_review")
        self.assertTrue(run["checkpoint_id"])
        self.assertEqual(run["validation"]["status"], "passed")
        self.assertEqual(len(self.model_revisions), 1)
        self.assertEqual(len(self.model_reviews), 1)
        self.assertEqual(run["candidate"]["blocks"][1:], task["draft"]["blocks"][1:])
        self.assertEqual(run["draft"], task["draft"])
        self.assertEqual(run["review_context"]["evidence"][0]["document_id"], str(self.evidence_id))
        restored = self.call("GET", f"/revision-tasks/{task['task_id']}").json()
        self.assertEqual(restored["checkpoint_id"], run["checkpoint_id"])
        self.assertEqual(restored["candidate"], run["candidate"])

    def test_foreign_evidence_cannot_be_added_to_owned_task(self):
        document = self.document()
        payload = {
            "platform": self.platform,
            "expected_draft_id": document["draft_id"],
            "expected_draft_version": document["version"],
            "requirements": {"goal": "Clear", "allowed_block_ids": [document["blocks"][0]["block_id"]]},
            "evidence_document_ids": [str(self.foreign_evidence_id)],
            "idempotency_key": uuid4().hex,
        }
        self.call("POST", f"/briefs/{self.brief_id}/revisions", json=payload, expected=404)

    def test_creation_replay_is_idempotent_and_different_payload_conflicts(self):
        task, payload = self.create_task()
        repeated = self.call("POST", f"/briefs/{self.brief_id}/revisions", json=payload).json()
        self.assertEqual(repeated["task_id"], task["task_id"])
        conflict = deepcopy(payload)
        conflict["requirements"]["goal"] = "A different goal"
        self.call("POST", f"/briefs/{self.brief_id}/revisions", json=conflict, expected=409)

    def test_removed_members_cannot_read_or_run_existing_task(self):
        task, _ = self.create_task()
        with self.sessions.begin() as db:
            member = db.scalar(select(self.TeamMember).where(
                self.TeamMember.team_id == self.team_id,
                self.TeamMember.user_id == self.actor_id,
            ))
            member.status = "removed"
        self.call("GET", f"/revision-tasks/{task['task_id']}", expected=403)
        self.call("POST", f"/revision-tasks/{task['task_id']}/run", json=self.command(task), expected=403)
        self.assertEqual(self.model_revisions, [])

    def test_repair_budget_is_bounded_and_keeps_last_candidate(self):
        self.reviewer_mode = "needs_revision"
        task, _ = self.create_task(max_revisions=3)
        run = self.run_task(task)
        self.assertEqual(run["status"], "budget_exhausted")
        self.assertEqual(run["revision_attempts"], 3)
        self.assertEqual(len(self.model_revisions), 4)
        self.assertEqual(run["validation"]["status"], "needs_revision")
        self.assertTrue(run["issues"])
        self.assertIsNotNone(run["candidate"])

    def test_missing_evidence_pauses_and_clarification_resumes(self):
        with self.sessions.begin() as db:
            db.get(self.KnowledgeDocument, self.evidence_id).body = ""
        task, _ = self.create_task(evidence_ids=[])
        waiting = self.run_task(task)
        self.assertEqual(waiting["status"], "needs_clarification")
        self.assertTrue(waiting["checkpoint_id"])
        self.assertIsNotNone(waiting["interrupt"])
        self.assertEqual(self.model_revisions, [])

        with self.sessions.begin() as db:
            document = db.get(self.KnowledgeDocument, self.evidence_id)
            document.body = "Supports 65W output. Returns within 30 days."
            document.version += 1

        payload = self.command(
            waiting, feedback="Use the attached product parameter sheet",
            evidence_document_ids=[str(self.evidence_id)],
        )
        resumed = self.call(
            "POST", f"/revision-tasks/{task['task_id']}/clarification", json=payload,
        ).json()
        self.assertEqual(resumed["status"], "awaiting_review")
        self.assertEqual(resumed["revision_attempts"], 0)
        self.assertEqual(len(self.model_revisions), 1)
        self.assertEqual(resumed["review_context"]["evidence"][0]["document_id"], str(self.evidence_id))

    def test_run_replay_does_not_generate_twice_and_stale_commands_conflict(self):
        task, _ = self.create_task()
        command = self.command(task)
        run = self.run_task(task, payload=command)
        replay = self.run_task(task, payload=command)
        self.assertEqual(replay, run)
        self.assertEqual(len(self.model_revisions), 1)

        stale = self.command(task)
        self.call("POST", f"/revision-tasks/{task['task_id']}/run", json=stale, expected=409)
        wrong_checkpoint = self.command(run)
        wrong_checkpoint["expected_checkpoint_id"] = "wrong-checkpoint"
        self.call("POST", f"/revision-tasks/{task['task_id']}/review",
                  json=wrong_checkpoint | {"action": "approved"}, expected=409)
        self.assertEqual(len(self.parent_reviews), 0)

    def test_approval_updates_only_allowed_body_and_is_idempotent(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        payload = self.command(run, action="approved")
        approved = self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload).json()
        self.assertEqual(approved["status"], "accepted")
        self.assertEqual(approved["adopted_version"], task["draft"]["version"] + 1)
        with self.sessions() as db:
            brief = db.get(self.Brief, self.brief_id)
            current = brief.content
            self.assertEqual(brief.workflow_status, "awaiting_review")
        self.assertEqual(current["platforms"]["instagram"], self.original_content["platforms"]["instagram"])
        for field in ("hook", "cta", "tags"):
            self.assertEqual(current["platforms"][self.platform][field], self.original_content["platforms"][self.platform][field])
        self.assertTrue(current["platforms"][self.platform]["body"].endswith(
            "\n\nReturns within 30 days.\n\nKeep this closing unchanged."
        ))
        self.assertNotEqual(current["platforms"][self.platform]["body"], self.original_body)
        self.assertEqual(len(self.parent_reviews), 1)

        replay = self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload).json()
        self.assertEqual(replay, approved)
        self.assertEqual(len(self.parent_reviews), 1)
        with self.sessions() as db:
            samples = db.scalars(select(self.models.RevisionSample).where(
                self.models.RevisionSample.task_id == task["task_id"],
            )).all()
        self.assertEqual(len(samples), 1)

    def test_human_final_text_is_validated_and_saved_as_the_adopted_result(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        final_blocks = deepcopy(run["candidate"]["blocks"])
        final_blocks[0]["text"] = "Supports 65W output, with clear wording for daily charging."
        approved = self.call(
            "POST", f"/revision-tasks/{task['task_id']}/review",
            json=self.command(run, action="approved", blocks=final_blocks),
        ).json()
        self.assertEqual(approved["status"], "accepted")
        self.assertEqual(approved["candidate"]["blocks"], final_blocks)
        self.assertEqual(len(self.model_reviews), 2)
        exported = self.call("GET", f"/revision-tasks/{task['task_id']}/sample").json()
        self.assertEqual(exported["original_draft"], task["draft"])
        self.assertEqual(exported["original_requirements"], task["requirements"])
        self.assertEqual(exported["accepted_candidate"]["blocks"], final_blocks)
        self.assertEqual(exported["model_candidate"], run["candidate"])
        self.assertTrue(exported["human_review"]["edited"])
        self.assertEqual(exported["human_review"]["action"], "approved")
        self.assertEqual(exported["human_review"]["actor_user_id"], str(self.actor_id))
        self.call("GET", f"/revision-tasks/{task['task_id']}/sample", user_id=self.peer_id, expected=404)

    def test_rejection_preserves_original_marketing_copy(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        rejected = self.call(
            "POST", f"/revision-tasks/{task['task_id']}/review",
            json=self.command(run, action="rejected", feedback="Do not adopt this wording"),
        ).json()
        self.assertEqual(rejected["status"], "rejected")
        with self.sessions() as db:
            brief = db.get(self.Brief, self.brief_id)
            self.assertEqual(brief.content, self.original_content)
        self.assertEqual(self.parent_reviews, [])

    def test_human_edit_outside_allowed_scope_is_refused(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        final_blocks = deepcopy(run["candidate"]["blocks"])
        final_blocks[1]["text"] = "A forbidden change to the returns policy."
        invalid = self.command(run, action="approved", blocks=final_blocks)
        self.call(
            "POST", f"/revision-tasks/{task['task_id']}/review",
            json=invalid, expected=409,
        )
        with self.sessions() as db:
            self.assertEqual(db.get(self.Brief, self.brief_id).content, self.original_content)
        self.assertEqual(self.parent_reviews, [])
        restored = self.call("GET", f"/revision-tasks/{task['task_id']}").json()
        self.assertEqual(restored["status"], "awaiting_review")
        approved = self.call(
            "POST", f"/revision-tasks/{task['task_id']}/review",
            json=self.command(restored, action="approved"),
        ).json()
        self.assertEqual(approved["status"], "accepted")
        self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=invalid, expected=409)

    def test_newer_parent_content_cannot_be_overwritten_by_old_task_approval(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        changed = deepcopy(self.original_content)
        changed["platforms"][self.platform]["body"] = "A newer human-authored document."
        with self.execution.workflow_execution(self.brief_id, database_url=self.test_url) as guard:
            brief = self.parent_service._load_execution_brief(guard, self.actor_id, team_id=self.team_id)
            config = self.parent_service.graph_config(brief)
            self.parent_graph.update_state(config, {
                "content": changed, "self_review": self.passed_parent_report(changed),
            }, as_node="self_review")
            self.parent_graph.invoke(None, config)
            self.parent_service.persist_snapshot(brief, self.parent_graph.get_state(config))

        self.call("POST", f"/revision-tasks/{task['task_id']}/review",
                  json=self.command(run, action="approved"), expected=409)
        with self.sessions() as db:
            self.assertEqual(db.get(self.Brief, self.brief_id).content, changed)

    def test_simultaneous_run_has_one_executor(self):
        task, _ = self.create_task()
        entered, release = threading.Event(), threading.Event()
        original_reviser = self.fake_reviser

        def blocked_reviser(messages):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test executor was not released")
            return original_reviser(messages)

        self.fake_reviser = blocked_reviser
        payload = self.command(task)
        with ThreadPoolExecutor(2) as workers:
            first = workers.submit(self.run_task, task, payload=payload)
            self.assertTrue(entered.wait(10))
            try:
                self.call("POST", f"/revision-tasks/{task['task_id']}/run",
                          json=self.command(task), expected=409)
            finally:
                release.set()
            completed = first.result(15)
        self.assertEqual(completed["status"], "awaiting_review")
        self.assertEqual(len(self.model_revisions), 1)

    def test_changed_evidence_version_blocks_stale_approval(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        with self.sessions.begin() as db:
            document = db.get(self.KnowledgeDocument, self.evidence_id)
            document.body = "The product now supports a different specification."
            document.version += 1

        self.call("POST", f"/revision-tasks/{task['task_id']}/review",
                  json=self.command(run, action="approved"), expected=409)
        with self.sessions() as db:
            self.assertEqual(db.get(self.Brief, self.brief_id).content, self.original_content)
        self.assertEqual(self.parent_reviews, [])

    def test_generation_failure_is_persisted_without_private_error_text(self):
        task, _ = self.create_task()
        self.reviser_mode = "error"
        with self.assertLogs("app.modules.marketing.workflow.revision.nodes", level="ERROR"):
            failed = self.run_task(task)
        self.assertEqual(failed["status"], "error")
        self.assertIsNone(failed["candidate"])
        self.assertTrue(failed["issues"])
        self.assertNotIn("private model detail", json.dumps(failed))
        restored = self.call("GET", f"/revision-tasks/{task['task_id']}").json()
        self.assertEqual(restored["checkpoint_id"], failed["checkpoint_id"])
        self.assertEqual(restored["issues"], failed["issues"])

    def test_invalid_clarification_does_not_poison_the_next_corrected_command(self):
        with self.sessions.begin() as db:
            db.get(self.KnowledgeDocument, self.evidence_id).body = ""
        task, _ = self.create_task(evidence_ids=[])
        waiting = self.run_task(task)
        bad = self.command(
            waiting, feedback="Use a private foreign source",
            evidence_document_ids=[str(self.foreign_evidence_id)],
        )
        self.call("POST", f"/revision-tasks/{task['task_id']}/clarification", json=bad, expected=404)
        restored = self.call("GET", f"/revision-tasks/{task['task_id']}").json()
        self.assertEqual(restored["status"], "needs_clarification")
        with self.sessions.begin() as db:
            document = db.get(self.KnowledgeDocument, self.evidence_id)
            document.body = "Supports 65W output. Returns within 30 days."
            document.version += 1
        good = self.command(
            restored, feedback="Use the owned product parameter sheet",
            evidence_document_ids=[str(self.evidence_id)],
        )
        resumed = self.call("POST", f"/revision-tasks/{task['task_id']}/clarification", json=good).json()
        self.assertEqual(resumed["status"], "awaiting_review")

    def test_evidence_changed_during_parent_review_is_checked_again_before_publish(self):
        task, _ = self.create_task()
        run = self.run_task(task)

        def change_evidence_during_model_io(state):
            with self.sessions.begin() as db:
                document = db.get(self.KnowledgeDocument, self.evidence_id)
                document.version += 1
                document.body = "The published specification has changed."
            return {
                "self_review": self.passed_parent_report(state["content"]),
                "self_review_attempts": 0,
            }

        with patch.object(self.parent_service, "review_targeted_parent_content", side_effect=change_evidence_during_model_io):
            self.call("POST", f"/revision-tasks/{task['task_id']}/review",
                      json=self.command(run, action="approved"), expected=409)
        with self.sessions() as db:
            self.assertEqual(db.get(self.Brief, self.brief_id).content, self.original_content)
            self.assertIsNone(db.scalar(select(self.models.RevisionSample).where(
                self.models.RevisionSample.task_id == task["task_id"],
            )))

    def test_replay_recovers_after_parent_publication_before_task_ledger_commit(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        payload = self.command(run, action="approved")
        with self.fail_after_parent_publication():
            with self.assertLogs("app.core.http_errors", level="ERROR"):
                self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload, expected=500)

        recovered = self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload).json()
        self.assertEqual(recovered["status"], "accepted")
        self.assertEqual(recovered["adopted_version"], task["draft"]["version"] + 1)
        self.assertEqual(len(self.parent_reviews), 1)
        with self.sessions() as db:
            samples = db.scalars(select(self.models.RevisionSample).where(
                self.models.RevisionSample.task_id == task["task_id"],
            )).all()
        self.assertEqual(len(samples), 1)

    def test_clarification_checkpoint_resumes_in_a_fresh_python_process(self):
        with self.sessions.begin() as db:
            db.get(self.KnowledgeDocument, self.evidence_id).body = ""
        task, _ = self.create_task(evidence_ids=[])
        waiting = self.run_task(task)
        self.assertEqual(waiting["status"], "needs_clarification")
        restored = self.request_in_new_process("GET", f"/revision-tasks/{task['task_id']}")
        self.assertEqual(restored["checkpoint_id"], waiting["checkpoint_id"])
        self.assertEqual(restored["status"], "needs_clarification")
        with self.sessions.begin() as db:
            document = db.get(self.KnowledgeDocument, self.evidence_id)
            document.body = "Supports 65W output. Returns within 30 days."
            document.version += 1
        resumed = self.request_in_new_process(
            "POST", f"/revision-tasks/{task['task_id']}/clarification",
            body=self.command(
                waiting, feedback="Use the newly supplied product sheet",
                evidence_document_ids=[str(self.evidence_id)],
            ),
        )
        self.assertEqual(resumed["status"], "awaiting_review")
        self.assertEqual(resumed["revision_attempts"], 0)
        self.assertNotEqual(resumed["checkpoint_id"], waiting["checkpoint_id"])
        current = self.call("GET", f"/revision-tasks/{task['task_id']}").json()
        self.assertEqual(current["candidate"], resumed["candidate"])

    def test_child_model_io_does_not_allow_updated_evidence_to_be_adopted(self):
        task, _ = self.create_task()
        original_reviser = self.fake_reviser

        def change_evidence_during_generation(messages):
            output = original_reviser(messages)
            with self.sessions.begin() as db:
                document = db.get(self.KnowledgeDocument, self.evidence_id)
                document.body = "The parameter source changed during generation."
                document.version += 1
            return output

        self.fake_reviser = change_evidence_during_generation
        run = self.run_task(task)
        self.call("POST", f"/revision-tasks/{task['task_id']}/review",
                  json=self.command(run, action="approved"), expected=409)
        with self.sessions() as db:
            self.assertEqual(db.get(self.Brief, self.brief_id).content, self.original_content)
            self.assertIsNone(db.scalar(select(self.models.RevisionSample).where(
                self.models.RevisionSample.task_id == task["task_id"],
            )))

    def test_parent_completion_between_publication_crash_and_child_retry_is_preserved(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        payload = self.command(run, action="approved")
        with self.fail_after_parent_publication():
            with self.assertLogs("app.core.http_errors", level="ERROR"):
                self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload, expected=500)

        parent_input = importlib.import_module("app.modules.marketing.schemas.marketing_workflow").MarketingWorkflowReviewInput
        with self.sessions() as db:
            user = db.get(self.User, self.actor_id)
            pending = self.parent_service.get_workflow(db, user, self.brief_id, team_id=self.team_id)
        completed = self.parent_service.review(
            self.actor_id,
            parent_input(
                brief_id=self.brief_id,
                expected_checkpoint_id=pending.checkpoint_id,
                action="approved",
            ),
            team_id=self.team_id,
        )
        self.assertEqual(completed.status, "completed")
        with self.sessions() as db:
            before = db.get(self.Brief, self.brief_id)
            completed_version = before.workflow_version
            completed_checkpoint = before.workflow_checkpoint_id
            completed_output = before.final_output

        recovered = self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload).json()
        self.assertEqual(recovered["status"], "accepted")
        self.assertEqual(recovered["adopted_version"], task["draft"]["version"] + 1)
        with self.sessions() as db:
            brief = db.get(self.Brief, self.brief_id)
            self.assertEqual(brief.workflow_status, "completed")
            self.assertEqual(brief.workflow_version, completed_version)
            self.assertEqual(brief.workflow_checkpoint_id, completed_checkpoint)
            self.assertEqual(brief.final_output, completed_output)
            samples = db.scalars(select(self.models.RevisionSample).where(
                self.models.RevisionSample.task_id == task["task_id"],
            )).all()
        self.assertEqual(len(samples), 1)

    def test_task_is_not_accepted_while_parent_review_is_still_running(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        entered, release = threading.Event(), threading.Event()

        def blocked_parent_review(state):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test parent reviewer was not released")
            return {
                "self_review": self.passed_parent_report(state["content"]),
                "self_review_attempts": 0,
            }

        with patch.object(self.parent_service, "review_targeted_parent_content", side_effect=blocked_parent_review):
            with ThreadPoolExecutor(2) as workers:
                future = workers.submit(
                    self.call, "POST", f"/revision-tasks/{task['task_id']}/review",
                    json=self.command(run, action="approved"),
                )
                self.assertTrue(entered.wait(10))
                try:
                    during = self.call("GET", f"/revision-tasks/{task['task_id']}").json()
                    self.assertIn(during["status"], ("adopting", "running"))
                    self.assertIsNone(during["adopted_version"])
                finally:
                    release.set()
                accepted = future.result(15).json()
        self.assertEqual(accepted["status"], "accepted")
        self.assertIsNotNone(accepted["adopted_version"])

    def test_noop_parent_start_does_not_invalidate_same_checkpoint_and_content(self):
        task, _ = self.create_task()
        before = task["draft"]["source_checkpoint_id"]
        unchanged = self.parent_service.start(self.actor_id, self.brief_id, team_id=self.team_id)
        self.assertEqual(unchanged.checkpoint_id, before)
        self.assertEqual(unchanged.content.model_dump(mode="json"), self.original_content)

        run = self.run_task(task)
        self.assertEqual(run["status"], "awaiting_review")
        accepted = self.call(
            "POST", f"/revision-tasks/{task['task_id']}/review",
            json=self.command(run, action="approved"),
        ).json()
        self.assertEqual(accepted["status"], "accepted")

    def test_fresh_brand_evidence_reaches_parent_review_without_promoting_user_instructions(self):
        new_fact = "Supports USB-C PD 3.0"
        rule_marker = "BRAND_RULE_IS_NOT_A_PRODUCT_FACT"
        feedback_marker = "CLARIFICATION_IS_NOT_PRODUCT_EVIDENCE"
        with self.sessions.begin() as db:
            document = db.get(self.KnowledgeDocument, self.evidence_id)
            document.body += " " + new_fact + "."
            document.version += 1

        original_reviser = self.fake_reviser
        original_reviewer = self.fake_reviewer

        def add_new_fact(messages):
            output = original_reviser(messages)
            output.blocks[0].text += " " + new_fact + "."
            return output

        def review_new_fact(messages):
            response = original_reviewer(messages)
            report = json.loads(response.content)
            payload = json.loads(messages[1][1])
            candidate_by_id = {item["block_id"]: item["candidate_text"] for item in payload["blocks"]}
            evidence = next(item for item in payload["evidence"] if new_fact in item["quote"])
            for block in report["blocks"]:
                if new_fact in candidate_by_id[block["block_id"]]:
                    block["facts"].append({
                        "claim": new_fact, "verdict": "supported",
                        "references": [{"evidence_id": evidence["evidence_id"], "quote": new_fact}],
                        "note": "New fact is supported by current authorized evidence",
                    })
            response.content = json.dumps(report)
            return response

        self.fake_reviser = add_new_fact
        self.fake_reviewer = review_new_fact
        self.reviewer_mode = "needs_clarification"
        task, _ = self.create_task(brand_rules=[rule_marker])
        waiting = self.run_task(task)
        self.assertEqual(waiting["status"], "needs_clarification")
        self.reviewer_mode = "passed"
        reviewed = self.call(
            "POST", f"/revision-tasks/{task['task_id']}/clarification",
            json=self.command(waiting, feedback=feedback_marker),
        ).json()
        self.assertEqual(reviewed["status"], "awaiting_review")
        self.assertIn(new_fact, reviewed["candidate"]["blocks"][0]["text"])
        captured = []

        def inspect_parent_review(state):
            captured.append(deepcopy(state))
            return {
                "self_review": self.passed_parent_report(state["content"]),
                "self_review_attempts": 0,
            }

        with patch.object(self.parent_service, "review_targeted_parent_content", side_effect=inspect_parent_review):
            accepted = self.call(
                "POST", f"/revision-tasks/{task['task_id']}/review",
                json=self.command(reviewed, action="approved"),
            ).json()
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(len(captured), 1)
        for name in ("retrieved_context", "context"):
            fact_context = captured[0][name]
            self.assertIn(new_fact, fact_context)
            self.assertNotIn(rule_marker, fact_context)
            self.assertNotIn(feedback_marker, fact_context)
            self.assertEqual(json.loads(fact_context.splitlines()[-1]), reviewed["review_context"]["evidence"])

        with self.execution.workflow_execution(self.brief_id, database_url=self.test_url):
            snapshot = self.parent_graph.get_state({"configurable": {"thread_id": str(self.brief_id)}})
        retained = json.loads(snapshot.values["retrieved_context"].splitlines()[-1])
        self.assertEqual(retained, reviewed["review_context"]["evidence"])
        self.assertEqual(retained[0]["document_id"], str(self.evidence_id))
        self.assertEqual(retained[0]["document_version"], 2)

    def test_clarification_retry_after_metadata_commit_does_not_duplicate_feedback(self):
        original_reviser = self.fake_reviser

        def ask_during_first_repair(messages):
            output = original_reviser(messages)
            if len(self.model_revisions) == 2:
                return self.schemas.RevisionRepairOutput(
                    clarification_questions=["Please confirm the requested tone"],
                )
            return output

        self.fake_reviser = ask_during_first_repair
        self.reviewer_mode = "needs_revision"
        task, _ = self.create_task()
        waiting = self.run_task(task)
        self.assertEqual(waiting["status"], "needs_clarification")
        self.assertEqual(waiting["revision_attempts"], 1)
        feedback = "Use a calm, factual tone with concise sentences"
        payload = self.command(waiting, feedback=feedback)
        prepare = self.service._prepare_clarification

        def prepare_then_crash(*args, **kwargs):
            prepare(*args, **kwargs)
            raise RuntimeError("simulated crash before Command resume")

        with patch.object(self.service, "_prepare_clarification", side_effect=prepare_then_crash):
            with self.assertLogs("app.core.http_errors", level="ERROR"):
                self.call("POST", f"/revision-tasks/{task['task_id']}/clarification", json=payload, expected=500)
        after_crash = self.call("GET", f"/revision-tasks/{task['task_id']}").json()
        self.assertEqual(after_crash["checkpoint_id"], waiting["checkpoint_id"])
        self.assertEqual(after_crash["requirements"]["goal"].count(feedback), 1)

        self.reviewer_mode = "passed"
        resumed = self.call("POST", f"/revision-tasks/{task['task_id']}/clarification", json=payload).json()
        self.assertEqual(resumed["status"], "awaiting_review")
        self.assertEqual(resumed["requirements"]["goal"].count(feedback), 1)
        self.assertEqual(resumed["original_requirements"], task["requirements"])
        self.assertEqual(resumed["revision_attempts"], 1)

    def test_saved_human_answer_cannot_be_replaced_after_next_checkpoint_fails(self):
        task, _ = self.create_task()
        run = self.run_task(task)
        original_feedback = "Adopt this exact reviewed wording"
        payload = self.command(run, action="approved", feedback=original_feedback)
        original_put = self.service.RevisionPostgresSaver.put

        def fail_after_saved_answer(saver, config, checkpoint, metadata, versions):
            if checkpoint["channel_values"].get("review_decision") == "approved":
                raise RuntimeError("simulated checkpoint outage after saved human answer")
            return original_put(saver, config, checkpoint, metadata, versions)

        with patch.object(self.service.RevisionPostgresSaver, "put", new=fail_after_saved_answer):
            with self.assertLogs("app.core.http_errors", level="ERROR"):
                self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload, expected=500)

        with self.execution.workflow_execution(self.brief_id, database_url=self.test_url) as guard:
            row = self.service._load_task(guard, task["task_id"], self.actor_id, self.team_id)
            graph = self.service._graph_for(guard, row)
            snapshot = graph.get_state(self.service._graph_config(row))
            saved_answers = [item for item in snapshot.tasks if item.name == "human_review"]
            self.assertEqual(self.service._checkpoint_id(snapshot), run["checkpoint_id"])
            self.assertEqual(len(saved_answers), 1)
            self.assertIsNotNone(saved_answers[0].result)
            self.assertEqual(saved_answers[0].result["review_decision"], "approved")
            self.assertIsNone(self.service._interrupt_value(snapshot))

        replacement = self.command(run, action="rejected", feedback="Replace the earlier decision")
        self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=replacement, expected=409)
        recovered = self.call("POST", f"/revision-tasks/{task['task_id']}/review", json=payload).json()
        self.assertEqual(recovered["status"], "accepted")
        sample = self.call("GET", f"/revision-tasks/{task['task_id']}/sample").json()
        self.assertEqual(sample["human_review"]["action"], "approved")
        self.assertEqual(sample["human_review"]["feedback"], original_feedback)
        with self.sessions() as db:
            samples = db.scalars(select(self.models.RevisionSample).where(
                self.models.RevisionSample.task_id == task["task_id"],
            )).all()
        self.assertEqual(len(samples), 1)


if __name__ == "__main__":
    unittest.main()
