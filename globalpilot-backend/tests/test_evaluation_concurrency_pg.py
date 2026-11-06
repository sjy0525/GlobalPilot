"""Real PostgreSQL concurrency checks, with no model/provider calls.

Opt in with EVAL_TEST_DATABASE_URL pointing at a disposable PostgreSQL database.
Each invocation creates and drops only its own randomly named schema. Without
that explicit URL the integration suite is skipped; it never uses .env's DB.
"""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.modules.evaluation.engine.lease import LeaseLost, lock_owned_lease, lock_slot, renew_lease
from app.modules.evaluation.engine.scoring import DIMENSIONS
from app.modules.brands.models import Brand
from app.modules.evaluation.models import EvalCase, EvalExecutorLease, EvalRun, EvalTask, EvalTestSet
from app.modules.users.models import User
from app.modules.teams.models import Team, TeamMember
from app.modules.evaluation.schemas import EvalReviewInput, EvalRunCreate


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, APP_ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.environ.get("EVAL_TEST_DATABASE_URL"), "Requires an isolated PostgreSQL test URL")
class EvaluationConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "eval_test_" + uuid4().hex
        cls.admin = create_engine(os.environ["EVAL_TEST_DATABASE_URL"])
        if cls.admin.dialect.name != "postgresql":
            raise RuntimeError("These tests require PostgreSQL")
        with cls.admin.begin() as db:
            db.execute(text(f"CREATE SCHEMA {cls.schema}"))
        cls.engine = create_engine(
            os.environ["EVAL_TEST_DATABASE_URL"],
            connect_args={"options": f"-csearch_path={cls.schema} -cstatement_timeout=10000 -clock_timeout=5000"},
        )
        cls.tables = [
            User.__table__, Team.__table__, TeamMember.__table__, Brand.__table__, EvalTestSet.__table__, EvalCase.__table__,
            EvalRun.__table__, EvalTask.__table__, EvalExecutorLease.__table__,
        ]
        Base.metadata.create_all(cls.engine, tables=cls.tables)
        cls.sessions = sessionmaker(bind=cls.engine, autoflush=False)

        # Load actual services and runner, substituting only database ownership
        # and external workflow providers. Never initialize an application DB.
        database = types.ModuleType("app.db.database")
        database.sessionLocal = cls.sessions
        database.get_db = lambda: None
        nodes = types.ModuleType("app.modules.marketing.workflow.nodes")
        nodes.NO_KNOWLEDGE = "no knowledge"
        for name in ("generate_content", "generate_strategy", "research_node", "retrieve_context"):
            setattr(nodes, name, Mock(side_effect=AssertionError("External providers must not run")))
        with patch.dict(sys.modules, {"app.db.database": database, "app.modules.marketing.workflow.nodes": nodes}):
            cls.runner = load_module("eval_concurrency_runner", "modules/evaluation/engine/runner.py")
            with patch.dict(sys.modules, {"app.modules.evaluation.engine.runner": cls.runner}):
                cls.service = load_module("eval_concurrency_service", "modules/evaluation/service.py")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as db:
            db.execute(text(f"DROP SCHEMA {cls.schema} CASCADE"))
        cls.admin.dispose()

    def setUp(self):
        with self.engine.begin() as db:
            for table in reversed(Base.metadata.sorted_tables):
                if table in self.tables:
                    db.execute(table.delete())
        with self.sessions.begin() as db:
            user = User(email="eval-audit@example.invalid")
            test_set = EvalTestSet(version=self.service.TEST_SET_VERSION, name="audit", description="audit")
            team = Team(name="Evaluation test team")
            db.add_all([user, team, test_set])
            db.flush()
            self.team_id = team.id
            db.add(TeamMember(team_id=team.id, user_id=user.id, role="admin", status="active"))
            db.add(Brand(name="audit", team_id=team.id))
            db.flush()
            self.user_id = user.id
            self.test_set_id = test_set.id
            self.case_ids = []
            for ordinal in (1, 2):
                case = EvalCase(
                    test_set_id=test_set.id, code=f"C{ordinal}", ordinal=ordinal, title="audit",
                    user_request="audit", brand_profile="audit", brand_name="audit", platforms=[],
                    must_include=[], forbidden_claims=[], region="audit", persona="audit",
                )
                db.add(case)
                db.flush()
                self.case_ids.append(case.id)
            self.run_ids = []
            self.task_ids = []
            for number in (1, 2):
                run = EvalRun(team_id=self.team_id, 
                    user_id=user.id, test_set_id=test_set.id, label=f"run {number}",
                    status="completed", provenance={},
                )
                db.add(run)
                db.flush()
                self.run_ids.append(run.id)
                tasks = []
                for case_id in self.case_ids:
                    scores = self.scores()
                    task = EvalTask(
                        run_id=run.id, case_id=case_id, execution_status="completed",
                        scores=scores, auto_scores=scores, qualified=True,
                    )
                    db.add(task)
                    db.flush()
                    tasks.append(task.id)
                self.task_ids.append(tasks)
        self.launch = Mock()
        for target, name, value in (
            (self.service, "launch", self.launch),
            (self.service, "workflow_version", lambda: "test-workflow"),
            (self.service, "provenance_snapshot", lambda db, version, **scope: {"test_set_version": version}),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def scores():
        return {key: {"verdict": "pass", "reason": "audit", "source": "model"} for key in DIMENSIONS}

    @staticmethod
    def together(*calls):
        barrier = threading.Barrier(len(calls))

        def invoke(call):
            barrier.wait(timeout=5)
            return call()

        with ThreadPoolExecutor(max_workers=len(calls)) as workers:
            futures = [workers.submit(invoke, call) for call in calls]
            return [future.result(timeout=15) for future in futures]

    def align_run_locks(self):
        # Requests now serialize at the stable team parent before run locks.
        return nullcontext()

    def start(self):
        result = self.service.start_run(self.user_id, EvalRunCreate(workflow_version="test-workflow"), team_id=self.team_id)
        return result, self.launch.call_args.args[0]

    def expire(self):
        with self.sessions.begin() as db:
            db.execute(update(EvalExecutorLease).values(lease_until=func.clock_timestamp() - text("interval '1 second'")))

    def test_disjoint_score_patches_preserve_both_changes(self):
        task_id = self.task_ids[0][0]
        with self.align_run_locks():
            self.together(
                lambda: self.service.review_task(self.user_id, task_id, EvalReviewInput(facts="fail", note="facts"), team_id=self.team_id),
                lambda: self.service.review_task(self.user_id, task_id, EvalReviewInput(tone="fail", note="tone"), team_id=self.team_id),
            )
        with self.sessions() as db:
            task = db.get(EvalTask, task_id)
            self.assertEqual(task.scores["facts"]["verdict"], "fail")
            self.assertEqual(task.scores["tone"]["verdict"], "fail")
            self.assertFalse(task.qualified)
            self.assertEqual(task.auto_scores["facts"]["verdict"], "pass")

    def test_different_tasks_leave_report_consistent_with_all_commits(self):
        with self.align_run_locks():
            self.together(*[
                lambda task_id=task_id: self.service.review_task(
                    self.user_id, task_id, EvalReviewInput(facts="fail", note="audit"),
                    team_id=self.team_id,
                ) for task_id in self.task_ids[0]
            ])
        with self.sessions() as db:
            run = db.get(EvalRun, self.run_ids[0])
            self.assertEqual(run.summary["qualified_count"], 0)
            self.assertEqual(run.summary["fact_error_count"], 2)
            self.assertIn("C1", run.report_markdown)
            self.assertIn("C2", run.report_markdown)

    def test_report_rebuild_and_review_use_the_same_run_lock(self):
        with self.align_run_locks():
            self.together(
                lambda: self.service.rebuild_report(self.user_id, self.run_ids[0], team_id=self.team_id),
                lambda: self.service.review_task(
                    self.user_id, self.task_ids[0][0], EvalReviewInput(facts="fail", note="audit"),
                    team_id=self.team_id,
                ),
            )
        with self.sessions() as db:
            self.assertEqual(db.get(EvalRun, self.run_ids[0]).summary["fact_error_count"], 1)

    def test_compare_uses_one_snapshot_when_review_commits_between_its_queries(self):
        left_id, right_id = self.run_ids
        self.service.rebuild_report(self.user_id, left_id, team_id=self.team_id)
        self.service.rebuild_report(self.user_id, right_id, team_id=self.team_id)
        original = self.service._tasks_by_code
        modified = False

        def review_before_tasks(db, run_id):
            nonlocal modified
            if run_id == left_id and not modified:
                modified = True
                # compare has already read both parent run summaries. This
                # separate transaction changes both task and summary atomically.
                self.service.review_task(
                    self.user_id, self.task_ids[0][0], EvalReviewInput(facts="fail", note="new review"),
                    team_id=self.team_id,
                )
            return original(db, run_id)

        with patch.object(self.service, "_tasks_by_code", side_effect=review_before_tasks):
            with self.sessions() as db:
                user = db.get(User, self.user_id)
                # Exercise expiration of an existing authentication identity.
                result = self.service.compare_runs(db, user, left_id, right_id, team_id=self.team_id)
                self.assertEqual(db.scalar(text("SHOW transaction_isolation")), "repeatable read")
                self.assertEqual(db.scalar(text("SHOW transaction_read_only")), "on")
        self.assertTrue(modified)
        self.assertEqual(result.left.summary["qualified_count"], 2)
        self.assertEqual(sum(row.left.qualified for row in result.rows), 2)
        with self.sessions() as db:
            current = self.service.get_run(db, types.SimpleNamespace(id=self.user_id), left_id, team_id=self.team_id)
        self.assertEqual(current.summary["qualified_count"], 1)

    def test_read_snapshot_options_reset_when_connection_returns_to_pool(self):
        engine = create_engine(
            os.environ["EVAL_TEST_DATABASE_URL"], pool_size=1, max_overflow=0,
            connect_args={"options": f"-csearch_path={self.schema}"},
        )
        factory = sessionmaker(bind=engine, autoflush=False)
        try:
            with patch.object(self.runner, "sessionLocal", factory):
                with factory() as db:
                    user = db.get(User, self.user_id)
                    result = self.service.get_run(db, user, self.run_ids[0], team_id=self.team_id)
                    self.assertEqual(result.id, self.run_ids[0])
                    physical_connection = db.scalar(text("SELECT pg_backend_pid()"))
                    self.assertEqual(db.scalar(text("SHOW transaction_isolation")), "repeatable read")
                    self.assertEqual(db.scalar(text("SHOW transaction_read_only")), "on")
            with factory.begin() as db:
                self.assertEqual(db.scalar(text("SELECT pg_backend_pid()")), physical_connection)
                self.assertEqual(db.scalar(text("SHOW transaction_isolation")), "read committed")
                self.assertEqual(db.scalar(text("SHOW transaction_read_only")), "off")
                db.execute(update(EvalRun).where(EvalRun.id == self.run_ids[0]).values(label="write still works"))
        finally:
            engine.dispose()

    def test_run_list_batches_counts_and_keeps_empty_runs_and_user_scope(self):
        with self.sessions.begin() as db:
            other_user = User(email="other-eval@example.invalid")
            db.add(other_user)
            db.flush()
            empty_run = EvalRun(team_id=self.team_id, 
                user_id=self.user_id, test_set_id=self.test_set_id,
                label="empty", status="queued", provenance={},
            )
            hidden_run = EvalRun(team_id=self.team_id, 
                user_id=other_user.id, test_set_id=self.test_set_id,
                label="another user", status="completed", provenance={},
            )
            db.add_all([empty_run, hidden_run])
            db.flush()
            empty_id, hidden_id = empty_run.id, hidden_run.id
            for task_id, status in zip(
                self.task_ids[0] + self.task_ids[1], ("pending", "running", "error", "timeout"),
            ):
                db.execute(update(EvalTask).where(EvalTask.id == task_id).values(execution_status=status))
            # History length must not increase the number of count queries.
            for number in range(25):
                run = EvalRun(team_id=self.team_id, 
                    user_id=self.user_id, test_set_id=self.test_set_id,
                    label=f"history {number}", status="completed", provenance={},
                )
                db.add(run)
                db.flush()
                db.add(EvalTask(run_id=run.id, case_id=self.case_ids[0], execution_status="completed"))

        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT") and (
                "FROM eval_runs" in statement or "FROM eval_tasks" in statement
            ):
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with self.sessions() as db:
                rows = self.service.list_runs(db, types.SimpleNamespace(id=self.user_id), team_id=self.team_id)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        by_id = {row.id: row for row in rows}
        self.assertEqual(len(statements), 2)
        self.assertEqual(len(rows), 28)
        self.assertNotIn(hidden_id, by_id)
        self.assertEqual((by_id[empty_id].task_total, by_id[empty_id].task_done), (0, 0))
        self.assertEqual((by_id[self.run_ids[0]].task_total, by_id[self.run_ids[0]].task_done), (2, 0))
        self.assertEqual((by_id[self.run_ids[1]].task_total, by_id[self.run_ids[1]].task_done), (2, 2))
        self.assertTrue(all(row.task_total == row.task_done == 1 for row in rows if row.label.startswith("history")))

    def test_empty_run_history_does_not_query_task_counts(self):
        with self.sessions.begin() as db:
            user = User(email="no-evals@example.invalid")
            db.add(user)
            db.flush()
            user_id = user.id
            db.add(TeamMember(team_id=self.team_id, user_id=user_id, role="employee", status="active"))
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT") and "FROM eval_tasks" in statement:
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with self.sessions() as db:
                self.assertEqual(self.service.list_runs(db, types.SimpleNamespace(id=user_id), team_id=self.team_id), [])
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(statements, [])

    def test_two_successful_baseline_switches_leave_exactly_one(self):
        results = self.together(*[
            lambda run_id=run_id: self.service.set_baseline(self.user_id, run_id, team_id=self.team_id) for run_id in self.run_ids
        ])
        self.assertTrue(all(result.is_baseline for result in results))
        with self.sessions() as db:
            rows = db.scalars(select(EvalRun).where(EvalRun.is_baseline.is_(True))).all()
            self.assertEqual(len(rows), 1)
        # Repeating the current selection also exercises clear-before-set order.
        self.assertTrue(self.service.set_baseline(self.user_id, rows[0].id, team_id=self.team_id).is_baseline)

    def test_database_rejects_two_baselines_even_without_service_locks(self):
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as db:
                db.execute(update(EvalRun).values(is_baseline=True))

    def test_global_claim_allows_only_one_run_and_no_orphan_tasks(self):
        def start_or_conflict():
            try:
                return self.start()[0]
            except HTTPException as exc:
                return exc.status_code

        results = self.together(start_or_conflict, start_or_conflict)
        self.assertEqual(sum(result == 409 for result in results), 1)
        self.assertEqual(self.launch.call_count, 1)
        with self.sessions() as db:
            self.assertEqual(db.query(EvalRun).count(), 3)
            self.assertEqual(db.query(EvalTask).count(), 6)
            lease = db.get(EvalExecutorLease, "global")
            self.assertIsNotNone(lease.owner)

    def test_claim_failure_rolls_back_run_and_tasks(self):
        with patch.object(self.service, "claim_locked_slot", side_effect=RuntimeError("claim failed")):
            with self.assertRaisesRegex(RuntimeError, "claim failed"):
                self.start()
        with self.sessions() as db:
            self.assertEqual(db.query(EvalRun).count(), 2)
            self.assertEqual(db.query(EvalTask).count(), 4)
            self.assertIsNone(db.get(EvalExecutorLease, "global"))

    def test_launch_failure_marks_tasks_failed_and_releases_lease(self):
        self.launch.side_effect = RuntimeError("thread start failed")
        with self.assertRaisesRegex(RuntimeError, "thread start failed"):
            self.start()
        with self.sessions() as db:
            failed = db.scalars(select(EvalRun).where(EvalRun.status == "failed")).one()
            self.assertEqual(failed.summary["error_count"], 2)
            self.assertIsNone(db.get(EvalExecutorLease, "global").owner)

    def test_startup_recovery_leaves_another_workers_live_run_untouched(self):
        run, token = self.start()
        with self.sessions() as db:
            self.service.recover_interrupted_runs(db)
        with self.sessions() as db:
            self.assertEqual(db.get(EvalRun, run.id).status, "queued")
            self.assertEqual(db.get(EvalExecutorLease, "global").owner, token.owner)

    def test_expired_lease_recovers_on_read_and_cannot_be_renewed(self):
        run, token = self.start()
        self.expire()
        with self.assertRaises(LeaseLost):
            with self.sessions.begin() as db:
                renew_lease(db, token)
        with self.sessions() as db:
            result = self.service.get_run(db, types.SimpleNamespace(id=self.user_id), run.id, team_id=self.team_id)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.summary["error_count"], 2)
        with self.sessions() as db:
            self.assertIsNone(db.get(EvalExecutorLease, "global").owner)

    def test_lazy_recovery_releases_authentication_connection_in_a_one_slot_pool(self):
        engine = create_engine(
            os.environ["EVAL_TEST_DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=0.2,
            connect_args={"options": f"-csearch_path={self.schema}"},
        )
        factory = sessionmaker(bind=engine, autoflush=False)
        try:
            with patch.object(self.runner, "sessionLocal", factory):
                with factory() as db:
                    # The ORM authentication read has already begun a transaction.
                    user = db.get(User, self.user_id)
                    result = self.service.get_run(db, user, self.run_ids[0], team_id=self.team_id)
                    self.assertEqual(result.id, self.run_ids[0])
        finally:
            engine.dispose()

    def test_expired_worker_cannot_write_after_a_new_owner_claims(self):
        old, token = self.start()
        self.expire()
        new, replacement = self.start()
        self.assertGreater(replacement.generation, token.generation)
        with self.assertRaises(LeaseLost):
            self.runner._fail_run(token, "late old error")
        with self.assertRaises(LeaseLost):
            self.runner._finalize(token)
        with self.sessions() as db:
            self.assertEqual(db.get(EvalRun, old.id).status, "failed")
            self.assertEqual(db.get(EvalRun, new.id).status, "queued")
            self.assertEqual(db.get(EvalExecutorLease, "global").owner, replacement.owner)

    def test_in_flight_old_generation_result_is_discarded_after_takeover(self):
        old, token = self.start()
        with self.sessions.begin() as db:
            lock_owned_lease(db, token)
            run = db.scalar(select(EvalRun).where(EvalRun.id == old.id).with_for_update())
            run.status = "running"
            task_id = db.scalar(select(EvalTask.id).where(EvalTask.run_id == old.id).limit(1))
        entered, release = threading.Event(), threading.Event()

        def late_model_result(payload):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release model result")
            return {}  # Fencing must reject this before applying any outcome.

        with patch.object(self.runner, "_run_case", side_effect=late_model_result):
            with ThreadPoolExecutor(max_workers=1) as worker:
                future = worker.submit(self.runner._execute_task, token, task_id)
                try:
                    self.assertTrue(entered.wait(5))
                    self.expire()
                    new, replacement = self.start()
                finally:
                    release.set()
                with self.assertRaises(LeaseLost):
                    future.result(timeout=5)
        with self.sessions() as db:
            self.assertEqual(db.get(EvalTask, task_id).execution_status, "error")
            self.assertIsNone(db.get(EvalTask, task_id).generated_content)
            self.assertEqual(db.get(EvalExecutorLease, "global").run_id, new.id)

    def test_heartbeat_renews_with_the_same_owner_and_generation(self):
        _run, token = self.start()
        with self.sessions() as db:
            before = db.get(EvalExecutorLease, "global").lease_until
        stop, lost, renewed = threading.Event(), threading.Event(), threading.Event()
        real_renew = self.runner.renew_lease

        def observed_renew(db, lease_token):
            real_renew(db, lease_token)
            renewed.set()

        with patch.object(self.runner, "HEARTBEAT_SECONDS", 0.01), \
             patch.object(self.runner, "renew_lease", side_effect=observed_renew):
            thread = threading.Thread(target=self.runner._heartbeat, args=(token, stop, lost))
            thread.start()
            try:
                self.assertTrue(renewed.wait(5))
            finally:
                stop.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(lost.is_set())
        with self.sessions() as db:
            lease = db.get(EvalExecutorLease, "global")
            self.assertGreater(lease.lease_until, before)
            self.assertEqual(lease.generation, token.generation)

    def test_worker_completion_releases_lease_and_reports_all_tasks(self):
        run, token = self.start()
        scores = self.scores()
        outcome = {
            "execution_status": "completed", "empty_output": False, "timed_out": False,
            "qualified": True, "content": {}, "strategy": "audit", "retrieved": "audit",
            "duration_ms": 1, "api_cost_usd": 0, "judge_cost_usd": 0, "usage": {},
            "error": None, "scores": scores,
        }
        with patch.object(self.runner, "_run_case", return_value=outcome):
            self.runner.execute(token)
        with self.sessions() as db:
            final = db.get(EvalRun, run.id)
            self.assertEqual(final.status, "completed")
            self.assertEqual(final.summary["qualified_count"], 2)
            self.assertEqual(final.summary["total"], 2)
            self.assertIsNone(db.get(EvalExecutorLease, "global").owner)


if __name__ == "__main__":
    unittest.main()
