"""Real TCP HTTP + PostgreSQL behavior checks with deterministic external models.

Set EVAL_TEST_DATABASE_URL and WORKFLOW_TEST_DATABASE_URL to disposable test
databases. Run `python -m tests.test_http_evaluation_checks_pg` to also write
docs/verification/evaluation-workflow.json (or BEHAVIOR_EVIDENCE_PATH).
No configured business database, app startup, JWT provider or external model is
used. The actual routers, services, ORM, graph and PostgreSQL saver do run.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import types
import unittest
from uuid import UUID
from unittest.mock import patch

from fastapi import Depends, FastAPI, Query
import httpx
from sqlalchemy import func, select
import uvicorn

from app.core.http_errors import install_exception_handlers
from app.modules.briefs.models import Brief
from app.modules.evaluation.models import EvalExecutorLease, EvalRun, EvalTask
from app.modules.users.models import User
from app.modules.teams.authorization import require_active_team_member
from app.modules.marketing.workflow.execution import WorkflowExecution
from tests import test_evaluation_concurrency_pg as eval_fixture
from tests import test_workflow_concurrency_pg as workflow_fixture


ROOT = Path(__file__).resolve().parents[1]
RECORDS = []


def dependency_must_be_overridden():
    raise AssertionError("Only the explicit test database may be used")


def load_routes(name, path, service_name, service):
    database = types.ModuleType("app.db.database")
    database.get_db = dependency_must_be_overridden
    security = types.ModuleType("app.modules.auth.security")
    security.get_current_user = dependency_must_be_overridden_user
    authorization = types.ModuleType("app.modules.teams.dependencies")
    authorization.get_current_member_from_query = dependency_must_be_overridden_member
    with patch.dict(sys.modules, {
        "app.db.database": database, "app.modules.auth.security": security, service_name: service,
        "app.modules.teams.dependencies": authorization,
    }):
        return workflow_fixture.load_module(name, path)


def dependency_must_be_overridden_user():
    raise AssertionError("Only the synthetic test actor may be used")


def dependency_must_be_overridden_member():
    raise AssertionError("Only the isolated test team may be used")


class TCPHTTPChecks:
    def open_http(self):
        self.record = {"test": self.id(), "requests": [], "observations": {}}
        RECORDS.append(self.record)
        self.record_lock = threading.Lock()
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(self.routes.router, prefix="/api")

        def session():
            with self.sessions() as db:
                yield db

        def actor(db=Depends(self.routes.get_db)):
            # Exercise the real authentication-session/transaction release
            # boundary; only token verification is replaced.
            return db.get(User, self.user_id)

        def member(team_id: UUID = Query(...), db=Depends(self.routes.get_db)):
            return require_active_team_member(db, team_id=team_id, user_id=self.user_id)

        app.dependency_overrides[self.routes.get_db] = session
        app.dependency_overrides[self.routes.get_current_user] = actor
        app.dependency_overrides[self.routes.get_current_member_from_query] = member
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        self.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, lifespan="off", access_log=False,
            log_level="critical", log_config=None, timeout_graceful_shutdown=5,
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def close():
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            if thread.is_alive():
                raise AssertionError("Test HTTP server did not stop")

        self.addCleanup(close)
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.started, "Test HTTP listener did not start")

    def request(self, method, path, **kwargs):
        started = time.perf_counter()
        kwargs["params"] = {"team_id": str(self.team_id), **kwargs.get("params", {})}
        with httpx.Client(base_url=self.base_url, timeout=15, trust_env=False) as client:
            response = client.request(method, path, **kwargs)
        with self.record_lock:
            self.record["requests"].append({
                "method": method, "path": path, "status": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            })
        return response

    def ok(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def simultaneous(self, *calls):
        barrier = threading.Barrier(len(calls))

        def execute(call):
            barrier.wait(timeout=5)
            return call()

        with ThreadPoolExecutor(len(calls)) as workers:
            futures = [workers.submit(execute, call) for call in calls]
            return [future.result(timeout=20) for future in futures]


@unittest.skipUnless(os.environ.get("EVAL_TEST_DATABASE_URL"), "requires isolated EVAL_TEST_DATABASE_URL")
class HTTPEvaluationChecks(TCPHTTPChecks, unittest.TestCase):
    scores = staticmethod(eval_fixture.EvaluationConcurrencyTests.scores)

    @classmethod
    def setUpClass(cls):
        eval_fixture.EvaluationConcurrencyTests.setUpClass.__func__(cls)
        cls.routes = load_routes(
            "http_eval_check_routes", "app/modules/evaluation/router.py",
            "app.modules.evaluation.service", cls.service,
        )

    @classmethod
    def tearDownClass(cls):
        eval_fixture.EvaluationConcurrencyTests.tearDownClass.__func__(cls)

    def setUp(self):
        eval_fixture.EvaluationConcurrencyTests.setUp(self)
        self.open_http()

    def aligned_run_locks(self):
        # Writes now serialize at the team parent before taking the run lock.
        return nullcontext()

    def run_snapshot(self):
        with self.sessions() as db:
            run = db.get(EvalRun, self.run_ids[0])
            task = db.get(EvalTask, self.task_ids[0][0])
            return {
                "scores": task.scores, "qualified": task.qualified,
                "failure_reasons": task.failure_reasons, "human_note": task.human_note,
                "summary": run.summary, "report": run.report_markdown,
            }

    def test_two_http_score_patches_preserve_disjoint_fields(self):
        path = f"/api/evals/tasks/{self.task_ids[0][0]}"
        with self.aligned_run_locks():
            results = self.simultaneous(
                lambda: self.request("PATCH", path, json={"facts": "fail", "note": "facts A"}),
                lambda: self.request("PATCH", path, json={"tone": "fail", "note": "tone B"}),
            )
        self.assertEqual([r.status_code for r in results], [200, 200])
        tasks = self.ok("GET", f"/api/evals/runs/{self.run_ids[0]}/tasks")
        task = next(row for row in tasks if row["id"] == str(self.task_ids[0][0]))
        self.assertEqual(task["scores"]["facts"]["verdict"], "fail")
        self.assertEqual(task["scores"]["tone"]["verdict"], "fail")
        self.assertFalse(task["qualified"])
        self.assertEqual(task["auto_scores"]["facts"]["verdict"], "pass")
        self.record["observations"] = {
            "responses": [200, 200], "persisted_facts": "fail", "persisted_tone": "fail",
            "qualified": False, "automatic_score_preserved": True,
            "synchronization": "both HTTP requests reach parent run lock before either acquires it",
        }

    def test_two_http_task_reviews_leave_one_consistent_summary(self):
        with self.aligned_run_locks():
            results = self.simultaneous(*[
                lambda task_id=task_id: self.request(
                    "PATCH", f"/api/evals/tasks/{task_id}", json={"facts": "fail", "note": "cross-task review"},
                ) for task_id in self.task_ids[0]
            ])
        self.assertEqual([r.status_code for r in results], [200, 200])
        run = self.ok("GET", f"/api/evals/runs/{self.run_ids[0]}")
        tasks = self.ok("GET", f"/api/evals/runs/{self.run_ids[0]}/tasks")
        self.assertEqual(run["summary"]["qualified_count"], sum(task["qualified"] for task in tasks))
        self.assertEqual(run["summary"]["qualified_count"], 0)
        self.assertEqual(run["summary"]["fact_error_count"], 2)
        self.assertTrue(all(case in run["report_markdown"] for case in ("C1", "C2")))
        self.record["observations"] = {"responses": [200, 200], "qualified_count": 0, "fact_error_count": 2, "report_contains_both_cases": True}

    def test_two_http_baseline_switches_leave_exactly_one(self):
        barrier = threading.Barrier(2)
        original = self.routes.set_baseline

        def aligned(*args, **kwargs):
            barrier.wait(timeout=5)
            return original(*args, **kwargs)

        with patch.object(self.routes, "set_baseline", side_effect=aligned):
            results = self.simultaneous(*[
                lambda run_id=run_id: self.request("POST", f"/api/evals/runs/{run_id}/baseline")
                for run_id in self.run_ids
            ])
        self.assertEqual([r.status_code for r in results], [200, 200])
        runs = self.ok("GET", "/api/evals/runs")
        selected = [run["id"] for run in runs if run["is_baseline"]]
        self.assertEqual(len(selected), 1)
        self.assertIn(selected[0], [str(run_id) for run_id in self.run_ids])
        self.record["observations"] = {"responses": [200, 200], "final_baseline_count": 1, "final_baseline_id": selected[0]}

    def test_http_review_failure_after_flushed_writes_rolls_back_task_and_summary(self):
        self.ok("POST", f"/api/evals/runs/{self.run_ids[0]}/report")
        before = self.run_snapshot()
        original = self.service.refresh_run_artifacts
        observations = {}

        def fail_after_database_updates(db, run):
            original(db, run)
            db.flush()
            observations["own_transaction_facts"] = db.get(EvalTask, self.task_ids[0][0]).scores["facts"]["verdict"]
            observations["independent_reader_sees_old_commit"] = self.run_snapshot() == before
            raise RuntimeError("injected failure after task and summary SQL updates")

        with patch.object(self.service, "refresh_run_artifacts", side_effect=fail_after_database_updates), \
                self.assertLogs("app.core.http_errors", level="ERROR"):
            response = self.request("PATCH", f"/api/evals/tasks/{self.task_ids[0][0]}", json={"facts": "fail", "note": "must roll back"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], "INTERNAL_ERROR")
        self.assertEqual(observations["own_transaction_facts"], "fail")
        self.assertTrue(observations["independent_reader_sees_old_commit"])
        self.assertEqual(self.run_snapshot(), before)
        self.record["observations"] = {**observations, "http_status": 500, "all_task_and_run_fields_equal_before": True}

    def test_http_baseline_failure_rolls_back_both_clear_and_select(self):
        self.ok("POST", f"/api/evals/runs/{self.run_ids[0]}/baseline")
        with patch.object(self.service, "_run_out", side_effect=RuntimeError("injected failure after baseline SQL")), \
                self.assertLogs("app.core.http_errors", level="ERROR"):
            response = self.request("POST", f"/api/evals/runs/{self.run_ids[1]}/baseline")
        self.assertEqual(response.status_code, 500)
        runs = self.ok("GET", "/api/evals/runs")
        self.assertEqual([row["id"] for row in runs if row["is_baseline"]], [str(self.run_ids[0])])
        self.record["observations"] = {"http_status": 500, "old_baseline_restored": True, "final_baseline_count": 1}

    def test_http_run_creation_failure_rolls_back_run_tasks_and_claim(self):
        with self.sessions() as db:
            before = (db.scalar(select(func.count()).select_from(EvalRun)), db.scalar(select(func.count()).select_from(EvalTask)))
        original = self.service.claim_locked_slot
        observed = {}

        def fail_after_claim(db, lease, run_id):
            token = original(db, lease, run_id)
            observed["run_and_tasks_flushed"] = db.get(EvalRun, token.run_id) is not None and db.scalar(
                select(func.count()).select_from(EvalTask).where(EvalTask.run_id == token.run_id)
            ) == 2
            observed["lease_owner_written"] = lease.owner is not None
            raise RuntimeError("injected failure after claim flush")

        with patch.object(self.service, "claim_locked_slot", side_effect=fail_after_claim), \
                self.assertLogs("app.core.http_errors", level="ERROR"):
            response = self.request("POST", "/api/evals/runs", json={"workflow_version": "test-workflow", "label": "must not survive"})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(all(observed.values()))
        with self.sessions() as db:
            after = (db.scalar(select(func.count()).select_from(EvalRun)), db.scalar(select(func.count()).select_from(EvalTask)))
            lease = db.get(EvalExecutorLease, "global")
            self.assertTrue(lease is None or lease.owner is None)
        self.assertEqual(after, before)
        self.assertFalse(self.launch.called)
        self.record["observations"] = {**observed, "http_status": 500, "run_task_counts_unchanged": True, "no_active_lease_or_launch": True}


@unittest.skipUnless(os.environ.get("WORKFLOW_TEST_DATABASE_URL"), "requires isolated WORKFLOW_TEST_DATABASE_URL")
class HTTPWorkflowChecks(TCPHTTPChecks, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow_fixture.WorkflowServiceConcurrencyTests.setUpClass.__func__(cls)
        cls.routes = load_routes(
            "http_workflow_check_routes", "app/modules/marketing/router.py",
            "app.modules.marketing.service", cls.service,
        )

    @classmethod
    def tearDownClass(cls):
        workflow_fixture.WorkflowServiceConcurrencyTests.tearDownClass.__func__(cls)

    def setUp(self):
        workflow_fixture.WorkflowServiceConcurrencyTests.setUp(self)
        self.open_http()

    def start_http(self):
        return self.ok("POST", "/api/briefs/workflow", json={"brief_id": str(self.brief_id)})

    def review_body(self, workflow, action="rejected", feedback="Revise A"):
        return {"brief_id": str(self.brief_id), "expected_checkpoint_id": workflow["checkpoint_id"], "action": action, "feedback": feedback if action == "rejected" else None}

    def projection(self):
        with self.sessions() as db:
            brief = db.get(Brief, self.brief_id)
            return {name: getattr(brief, name) for name in ("workflow_status", "workflow_version", "workflow_checkpoint_id", "content", "strategy", "final_output", "revision_count")}

    def test_concurrent_http_reviews_return_one_conflict_without_losing_feedback(self):
        initial = self.start_http()
        entered, release = threading.Event(), threading.Event()
        invoke = self.graph.invoke

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("Test reviewer was not released")
            return invoke(*args, **kwargs)

        with patch.object(self.graph, "invoke", side_effect=delayed), ThreadPoolExecutor(2) as workers:
            first = workers.submit(self.request, "POST", "/api/briefs/workflow/review", json=self.review_body(initial, feedback="Feedback A"))
            self.assertTrue(entered.wait(5))
            try:
                second = self.request("POST", "/api/briefs/workflow/review", json=self.review_body(initial, feedback="Feedback B"))
            finally:
                release.set()
            first = first.result(timeout=15)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(self.graph.get_state(self.config).values["feedback_log"], ["Feedback A"])
        current = self.ok("GET", "/api/briefs/workflow", params={"brief_id": str(self.brief_id)})
        self.assertEqual(current["revision_count"], 1)
        self.record["observations"] = {"responses": [200, 409], "feedback_log": ["Feedback A"], "revision_count": 1, "synchronization": "first HTTP reviewer holds the real PG execution lock while second HTTP request contends"}

    def test_stale_http_approval_cannot_approve_the_revised_draft(self):
        initial = self.start_http()
        passed_preflight, release = threading.Event(), threading.Event()
        original = self.routes.review

        def pause_old_approval(user_id, payload, *, team_id):
            if payload.action == "approved" and payload.expected_checkpoint_id == initial["checkpoint_id"]:
                # The actual router has already validated the old checkpoint;
                # another request will change it before the write lock is taken.
                passed_preflight.set()
                if not release.wait(8):
                    raise TimeoutError("Stale approval was not released")
            return original(user_id, payload, team_id=team_id)

        with patch.object(self.routes, "review", side_effect=pause_old_approval), ThreadPoolExecutor(1) as workers:
            approval = workers.submit(
                self.request, "POST", "/api/briefs/workflow/review", json=self.review_body(initial, "approved"),
            )
            self.assertTrue(passed_preflight.wait(5))
            try:
                revised = self.ok("POST", "/api/briefs/workflow/review", json=self.review_body(initial))
            finally:
                release.set()
            stale = approval.result(timeout=15)
        self.assertNotEqual(initial["checkpoint_id"], revised["checkpoint_id"])
        self.assertEqual(stale.status_code, 409, stale.text)
        current = self.ok("GET", "/api/briefs/workflow", params={"brief_id": str(self.brief_id)})
        self.assertEqual(current["checkpoint_id"], revised["checkpoint_id"])
        self.assertEqual(current["status"], "awaiting_review")
        approved = self.ok("POST", "/api/briefs/workflow/review", json=self.review_body(revised, "approved"))
        self.assertEqual(approved["status"], "completed")
        self.record["observations"] = {"old_approval_passed_http_preflight_before_concurrent_revision": True, "stale_approval_status": 409, "stale_request_left_latest_draft_awaiting_review": True, "fresh_approval_status": 200, "final_status": "completed"}

    def test_projection_sql_failure_keeps_checkpoint_and_http_retry_repairs_projection(self):
        initial = self.start_http()
        before = self.projection()
        original_transaction = WorkflowExecution.transaction
        injected = []

        class ProjectionCursor:
            def __init__(self, cursor):
                self.cursor, self.updated = cursor, False

            def execute(self, statement, *args, **kwargs):
                result = self.cursor.execute(statement, *args, **kwargs)
                if isinstance(statement, str) and "UPDATE briefs SET workflow_status" in statement:
                    self.updated = True
                return result

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        @contextmanager
        def fail_before_commit(guard):
            with original_transaction(guard) as cursor:
                proxy = ProjectionCursor(cursor)
                yield proxy
                if proxy.updated:
                    injected.append(True)
                    raise RuntimeError("injected projection failure after UPDATE before COMMIT")

        with patch.object(WorkflowExecution, "transaction", fail_before_commit), \
                self.assertLogs("app.core.http_errors", level="ERROR"):
            failed = self.request("POST", "/api/briefs/workflow/review", json=self.review_body(initial, "approved"))
        self.assertEqual(failed.status_code, 500, failed.text)
        self.assertEqual(injected, [True])
        self.assertEqual(self.projection(), before)
        head = self.graph.get_state(self.config)
        self.assertEqual(head.values["decision"], "approved")
        self.assertTrue(head.values["final_output"])
        current = self.ok("GET", "/api/briefs/workflow", params={"brief_id": str(self.brief_id)})
        self.assertEqual(current["status"], "completed")
        self.assertNotEqual(current["checkpoint_id"], before["workflow_checkpoint_id"])
        with patch.object(self.graph, "invoke", side_effect=AssertionError("Recovery must not regenerate completed work")):
            recovered = self.start_http()
        after = self.projection()
        self.assertEqual(recovered["checkpoint_id"], current["checkpoint_id"])
        self.assertEqual(after["workflow_status"], "completed")
        self.assertEqual(after["workflow_checkpoint_id"], current["checkpoint_id"])
        self.assertEqual(after["workflow_version"], before["workflow_version"] + 1)
        self.record["observations"] = {
            "failed_http_status": 500, "projection_update_executed_then_rolled_back": True,
            "projection_equal_before_failure": True, "checkpoint_decision_and_final_output_preserved": True,
            "get_returns_completed_checkpoint": True, "retry_repairs_projection_without_graph_invoke": True,
            "boundary": "Checkpoint progress is committed separately; only the failed projection transaction rolls back.",
        }

    def test_http_recovery_preserves_decision_saved_before_checkpoint_failure(self):
        initial = self.start_http()
        original = self.saver.put

        def fail_after_review(config, checkpoint, metadata, versions):
            if checkpoint["channel_values"].get("decision") == "approved":
                raise RuntimeError("injected checkpoint failure after review task result")
            return original(config, checkpoint, metadata, versions)

        with patch.object(self.saver, "put", side_effect=fail_after_review), \
                self.assertLogs("app.core.http_errors", level="ERROR"):
            failed = self.request("POST", "/api/briefs/workflow/review", json=self.review_body(initial, "approved"))
        self.assertEqual(failed.status_code, 500, failed.text)
        opposite = self.request("POST", "/api/briefs/workflow/review", json=self.review_body(initial, feedback="Do not override approval"))
        self.assertEqual(opposite.status_code, 409, opposite.text)
        recovered = self.start_http()
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.graph.get_state(self.config).values["decision"], "approved")
        self.record["observations"] = {"checkpoint_failure_http_status": 500, "opposite_review_status": 409, "recovery_status": "completed", "original_saved_decision": "approved"}


if __name__ == "__main__":
    if not all(os.environ.get(name) for name in ("EVAL_TEST_DATABASE_URL", "WORKFLOW_TEST_DATABASE_URL")):
        raise SystemExit("Both explicit disposable database URLs are required to generate HTTP evidence")
    program = unittest.main(exit=False, verbosity=2)
    result = program.result
    failed = {case.id() for case, _trace in result.failures + result.errors}
    for record in RECORDS:
        record["passed"] = record["test"] not in failed
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "transport": "real HTTP/1.1 TCP loopback, uvicorn and httpx; not TestClient or ASGITransport",
        "database": "explicit disposable PostgreSQL URLs only; synthetic actors and data",
        "tests_run": result.testsRun, "successful": result.wasSuccessful(), "skipped": len(result.skipped),
        "limitations": [
            "Single ASGI process with concurrent HTTP requests and separate real database sessions; not a multi-process throughput test.",
            "JWT verification is replaced; each request still loads a real User using the same request Session as the router.",
            "Application startup and unrelated routers are excluded; the actual error handlers, routes and services are used.",
            "Workflow nodes/graph/checkpoint storage are real; retrieval and external LLM responses are deterministic fakes.",
            "Injected exceptions and barriers establish ordering, not probabilities or production latency. Request durations include deliberate blocking.",
            "Workflow checkpoint commits are not rolled back when a later projection commit fails; recovery is verified explicitly.",
        ],
        "cases": RECORDS,
    }
    path = Path(os.environ.get("BEHAVIOR_EVIDENCE_PATH", ROOT / "docs/verification/evaluation-workflow.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Behavior evidence: {path}")
    sys.exit(0 if result.wasSuccessful() else 1)
