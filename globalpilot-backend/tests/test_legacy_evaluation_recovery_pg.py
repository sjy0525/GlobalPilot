"""Offline-only recovery tests using the isolated evaluation PostgreSQL fixture."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

from app.modules.evaluation.engine.legacy_recovery import ActiveEvaluationLease, WorkersNotStopped, recover_legacy_evaluations
from app.modules.evaluation.models import EvalExecutorLease, EvalRun, EvalTask

try:
    from . import test_evaluation_concurrency_pg as evaluation
except ImportError:
    import test_evaluation_concurrency_pg as evaluation


def load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "recover_legacy_evaluations.py"
    spec = importlib.util.spec_from_file_location("legacy_evaluation_recovery_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.environ.get("EVAL_TEST_DATABASE_URL"), "Requires an isolated PostgreSQL test URL")
class LegacyEvaluationRecoveryTests(unittest.TestCase):
    setUpClass = classmethod(evaluation.EvaluationConcurrencyTests.setUpClass.__func__)
    tearDownClass = classmethod(evaluation.EvaluationConcurrencyTests.tearDownClass.__func__)
    setUp = evaluation.EvaluationConcurrencyTests.setUp
    scores = staticmethod(evaluation.EvaluationConcurrencyTests.scores)
    start = evaluation.EvaluationConcurrencyTests.start
    expire = evaluation.EvaluationConcurrencyTests.expire

    def make_legacy_run(self):
        with self.sessions.begin() as db:
            run = db.get(EvalRun, self.run_ids[0])
            run.status = "running"
            run.provenance = {"preserve": "original"}
            unfinished = db.get(EvalTask, self.task_ids[0][0])
            unfinished.execution_status = "pending"
            completed = db.get(EvalTask, self.task_ids[0][1])
            completed.generated_content = {"historical": "keep"}
            completed.human_note = "keep completed review"

    def test_confirmation_is_required_before_cli_or_helper_writes(self):
        self.make_legacy_run()
        cli = load_cli()
        stderr = io.StringIO()
        with patch.object(cli, "_run_recovery") as execute, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                cli.main(["--database-url", "postgresql://user:secret@invalid/offline"])
        self.assertEqual(stopped.exception.code, 2)
        execute.assert_not_called()
        self.assertNotIn("secret", stderr.getvalue())
        with self.assertRaises(WorkersNotStopped):
            with self.sessions.begin() as db:
                recover_legacy_evaluations(db)
        with self.sessions() as db:
            self.assertEqual(db.get(EvalRun, self.run_ids[0]).status, "running")
            self.assertEqual(db.get(EvalTask, self.task_ids[0][0]).execution_status, "pending")
            self.assertIsNone(db.get(EvalExecutorLease, "global"))

    def test_live_lease_refuses_recovery_and_preserves_legacy_work(self):
        self.make_legacy_run()
        active, token = self.start()
        with self.assertRaises(ActiveEvaluationLease):
            with self.sessions.begin() as db:
                recover_legacy_evaluations(db, workers_stopped=True)
        with self.sessions() as db:
            self.assertEqual(db.get(EvalRun, active.id).status, "queued")
            self.assertEqual(db.get(EvalExecutorLease, "global").owner, token.owner)
            self.assertEqual(db.get(EvalRun, self.run_ids[0]).status, "running")
            self.assertEqual(db.get(EvalTask, self.task_ids[0][0]).execution_status, "pending")

    def test_cli_recovers_expired_and_legacy_runs_without_deleting_history(self):
        self.make_legacy_run()
        expired, _token = self.start()
        self.expire()
        url = make_url(os.environ["EVAL_TEST_DATABASE_URL"]).update_query_dict(
            {"options": f"-csearch_path={self.schema}"},
        )
        cli = load_cli()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main([
                "--confirm-workers-stopped", "--database-url", url.render_as_string(hide_password=False),
            ])
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), {
            "legacy_runs_recovered": 1, "expired_lease_recovered": True,
        })
        with self.sessions() as db:
            legacy = db.get(EvalRun, self.run_ids[0])
            self.assertEqual(legacy.status, "failed")
            self.assertEqual(legacy.provenance, {"preserve": "original"})
            self.assertEqual(legacy.summary["error_count"], 1)
            self.assertEqual(legacy.summary["qualified_count"], 1)
            self.assertEqual(legacy.summary["total"], 2)
            self.assertEqual(db.get(EvalTask, self.task_ids[0][0]).execution_status, "error")
            completed = db.get(EvalTask, self.task_ids[0][1])
            self.assertEqual(completed.execution_status, "completed")
            self.assertEqual(completed.generated_content, {"historical": "keep"})
            self.assertEqual(completed.human_note, "keep completed review")
            self.assertEqual(db.get(EvalRun, expired.id).status, "failed")
            self.assertEqual(db.get(EvalRun, expired.id).summary["error_count"], 2)
            self.assertEqual(db.get(EvalRun, self.run_ids[1]).status, "completed")
            self.assertEqual(db.query(EvalRun).count(), 3)
            self.assertEqual(db.query(EvalTask).count(), 6)
            self.assertIsNone(db.get(EvalExecutorLease, "global").owner)
        with self.sessions.begin() as db:
            repeated = recover_legacy_evaluations(db, workers_stopped=True)
        self.assertEqual(repeated.legacy_runs_recovered, 0)
        self.assertFalse(repeated.expired_lease_recovered)


if __name__ == "__main__":
    unittest.main()
