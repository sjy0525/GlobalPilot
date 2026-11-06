"""Real workflow stream routes with fake checkpoints/persistence, no startup or DB."""
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.http_errors import AppHTTPException, install_exception_handlers
from app.modules.marketing.schemas.marketing_workflow import MarketingWorkflowMiddleOutput
from app.modules.briefs.models import Brief  # Keep ORM registration outside sys.modules patches.


def fake_module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def isolated_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkflowStreamErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.graph = Mock()
        database = fake_module("app.db.database", get_db=lambda: None,
                               sessionLocal=Mock(side_effect=AssertionError("Real persistence is forbidden")))
        graph_module = fake_module("app.modules.marketing.workflow.graph", graph=cls.graph,
                                   MAX_SELF_REVIEW_ATTEMPTS=3, revisions_exhausted=lambda count: count >= 3)
        nodes = fake_module("app.modules.marketing.workflow.nodes", NO_KNOWLEDGE="没有品牌资料",
                            MAX_REVISION_PREFIX="已达到最大修改次数", SELF_REVIEW_EXHAUSTED_PREFIX="模型自审多次未通过")
        security = fake_module("app.modules.auth.security", get_current_user=lambda: None)
        with patch.dict(sys.modules, {
            "app.db.database": database,
            "app.modules.marketing.workflow.graph": graph_module,
            "app.modules.marketing.workflow.nodes": nodes,
            "app.modules.auth.security": security,
        }):
            cls.service = isolated_module("_workflow_stream_error_service", root / "app/modules/marketing/service.py")
            with patch.dict(sys.modules, {"app.modules.marketing.service": cls.service}):
                cls.routes = isolated_module("_workflow_stream_error_routes", root / "app/modules/marketing/router.py")
        cls.app = FastAPI()
        install_exception_handlers(cls.app)
        cls.app.include_router(cls.routes.router, prefix="/api")

    def setUp(self):
        self.graph.reset_mock(return_value=True, side_effect=True)
        self.brief = types.SimpleNamespace(id=uuid4(), team_id=uuid4(), user_id=uuid4(), brand_id=uuid4(), goal="Goal", selling_points="Blue",
                                           region="other", user_persona="Buyers", social_media=["x"],
                                           purchase_channels=None, competitor_url=None)
        self.snapshot = types.SimpleNamespace(interrupts=(True,), values={"team_id": self.brief.team_id, "actor_user_id": self.brief.user_id, "revision_count": 0},
                                              config={"configurable": {"checkpoint_id": "checkpoint-0"}},
                                              next=("retrieve_context",))
        self.graph.get_state.return_value = self.snapshot
        self.graph.stream.return_value = iter(())
        workflow = MarketingWorkflowMiddleOutput(brief_id=self.brief.id, status="completed", final_output="Done")
        patches = [
            patch.object(self.routes, "load_workflow", return_value=(self.brief, {}, self.snapshot)),
            patch.object(self.service, "persist_snapshot", return_value=workflow),
            patch.object(self.service, "review_passed", return_value=True),
            patch.object(self.service, "workflow_execution", side_effect=lambda _: nullcontext(object())),
            patch.object(self.service, "_load_execution_brief", return_value=self.brief),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.app.dependency_overrides[self.routes.get_db] = lambda: Mock()
        self.app.dependency_overrides[self.routes.get_current_member_from_query] = lambda: types.SimpleNamespace(team_id=self.brief.team_id, user_id=self.brief.user_id)
        self.app.dependency_overrides[self.routes.get_current_user] = lambda: types.SimpleNamespace(id=uuid4())
        self.addCleanup(self.app.dependency_overrides.clear)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def request(self, action=None):
        body = {"brief_id": str(self.brief.id)}
        path = "/api/briefs/workflow/stream"
        if action is not None:
            path = "/api/briefs/workflow/review/stream"
            body["action"] = action
            body["expected_checkpoint_id"] = "checkpoint-0"
        return self.client.post(path, json=body, params={"team_id": str(self.brief.team_id)})

    def events(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/x-ndjson", response.headers["content-type"])
        return [event for line in response.text.splitlines() if (event := json.loads(line)).get("type")]

    def test_missing_brief_is_404_before_streaming(self):
        self.routes.load_workflow.side_effect = AppHTTPException(404, "营销任务不存在", "BRIEF_NOT_FOUND")
        response = self.request()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "BRIEF_NOT_FOUND")
        self.graph.stream.assert_not_called()

    def test_review_not_pending_is_409_before_streaming(self):
        self.snapshot.interrupts = ()
        response = self.request("approved")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "当前不在待审流程")
        self.graph.stream.assert_not_called()

    def test_unreviewed_copy_is_409_before_streaming(self):
        self.service.review_passed.return_value = False
        response = self.request("approved")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "文案尚未通过模型自审，请先退回修改")
        self.graph.stream.assert_not_called()

    def test_approval_changed_after_preflight_becomes_409_error_event(self):
        self.service.review_passed.side_effect = [True, False]
        events = self.events(self.request("approved"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["status"], 409)
        self.assertTrue(events[0]["code"])
        self.assertEqual(events[0]["detail"], "文案尚未通过模型自审，请先退回修改")
        self.graph.stream.assert_not_called()

    def test_review_no_longer_pending_becomes_409_error_event(self):
        self.graph.get_state.return_value = types.SimpleNamespace(interrupts=(), values={}, config=self.snapshot.config)
        events = self.events(self.request("approved"))
        self.assertEqual(events[0]["status"], 409)
        self.assertEqual(events[0]["detail"], "当前不在待审流程")
        self.graph.stream.assert_not_called()

    def test_checkpoint_failure_becomes_safe_500_error_event(self):
        secret = "postgresql://user:private-password@database"
        self.graph.get_state.side_effect = RuntimeError(secret)
        with self.assertLogs("app.core.http_errors", level="ERROR"):
            events = self.events(self.request("approved"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], 500)
        self.assertEqual(events[0]["code"], "INTERNAL_ERROR")
        self.assertNotIn(secret, events[0]["detail"])
        self.graph.stream.assert_not_called()

    def test_start_runtime_failure_has_structured_error_without_done(self):
        self.snapshot.interrupts = ()
        self.graph.stream.side_effect = RuntimeError("Internal service details")
        with self.assertLogs("app.core.http_errors", level="ERROR"):
            events = self.events(self.request())
        self.assertEqual([event["type"] for event in events], ["node", "error"])
        self.assertEqual(events[-1]["status"], 500)
        self.assertEqual(events[-1]["code"], "INTERNAL_ERROR")
        self.assertNotIn("Internal service details", events[-1]["detail"])

    def test_persistence_http_exception_retains_status_detail_and_code(self):
        self.service.persist_snapshot.side_effect = AppHTTPException(404, "营销任务不存在", "BRIEF_NOT_FOUND")
        events = self.events(self.request())
        self.assertEqual(events, [{"type": "error", "status": 404,
                                   "code": "BRIEF_NOT_FOUND", "detail": "营销任务不存在"}])

    def test_upstream_timeout_keeps_504_inside_the_200_stream(self):
        self.snapshot.interrupts = ()
        self.graph.stream.side_effect = httpx.ReadTimeout("Provider internal details")
        with self.assertLogs("app.core.http_errors", level="ERROR"):
            events = self.events(self.request())
        self.assertEqual(events[-1]["status"], 504)
        self.assertEqual(events[-1]["code"], "UPSTREAM_TIMEOUT")
        self.assertNotIn("Provider internal details", events[-1]["detail"])

    def test_start_success_still_emits_done(self):
        events = self.events(self.request())
        self.assertEqual([event["type"] for event in events], ["done"])
        self.assertEqual(events[0]["workflow"]["status"], "completed")

    def test_research_runs_between_retrieval_and_strategy_in_stream(self):
        self.snapshot.interrupts = ()
        report = {"status": "not_needed", "summary": "Brand facts are sufficient.",
                  "source_urls": [], "uncertainties": []}
        validation = {"passed": True, "issues": []}
        self.graph.stream.return_value = iter([
            {"retrieve_context": {"context": "Brand facts"}},
            {"research_agent": {"research": report, "research_validation": validation}},
            {"generate_strategy": {"strategy": "Plan"}},
        ])
        events = self.events(self.request())
        nodes = [(event["node"], event["phase"]) for event in events if event["type"] == "node"]
        self.assertEqual(nodes[:6], [
            ("retrieve_context", "running"), ("retrieve_context", "done"),
            ("research_agent", "running"), ("research_agent", "done"),
            ("generate_strategy", "running"), ("generate_strategy", "done"),
        ])
        research_event = next(event for event in events if event.get("node") == "research_agent"
                              and event["phase"] == "done")
        self.assertEqual(research_event["research"], report)
        self.assertEqual(research_event["research_validation"], validation)

    def test_empty_retrieval_does_not_announce_research(self):
        self.assertEqual(self.service.next_node_after("retrieve_context", {"context": ""}), "no_context")

    def test_response_restores_research_from_same_checkpoint(self):
        report = {"status": "insufficient", "summary": "No local evidence was found.",
                  "source_urls": ["https://library.example.edu/chargers"],
                  "uncertainties": ["Campus rules are unknown."]}
        validation = {"passed": False, "issues": ["More evidence is needed."], "model_calls": 3}
        self.snapshot.values.update({"research": report, "research_validation": validation})
        response = self.service.to_response(self.brief, self.snapshot)
        self.assertEqual(response.research.model_dump(), report)
        self.assertEqual(response.research_validation, validation)
        restored = MarketingWorkflowMiddleOutput.model_validate_json(response.model_dump_json())
        self.assertEqual(restored.research.source_urls, report["source_urls"])
        self.assertEqual(restored.checkpoint_id, "checkpoint-0")

    def test_older_checkpoint_without_research_still_restores(self):
        response = self.service.to_response(self.brief, self.snapshot)
        self.assertIsNone(response.research)
        self.assertIsNone(response.research_validation)

    def test_review_success_still_emits_done(self):
        events = self.events(self.request("approved"))
        self.assertEqual([event["type"] for event in events], ["node", "done"])
        self.assertEqual(events[-1]["workflow"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
