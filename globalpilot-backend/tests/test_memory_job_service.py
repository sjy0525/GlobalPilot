"""Offline memory outbox authorization, tracing and failure-state checks.

The service and ORM statements are real; application configuration, provider
construction and memory Store connections are isolated. No database or model
request is permitted, and the worker thread is never started.
"""

import importlib.util
from contextlib import nullcontext
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from langchain_core.messages import ToolMessage
from sqlalchemy.dialects import postgresql

from app.modules.memory.models import MemoryJob  # Register outside sys.modules patches.
from app.modules.memory.schemas.brand_memory import BrandMemory, MemoryScope, MemorySource


ROOT = Path(__file__).resolve().parents[1]


def fake_module(name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    return module


def load_source(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def namespace(scope):
    return ("brand_preferences_v2", str(scope.team_id), str(scope.user_id), str(scope.brand_profile_id))


class MemoryJobServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.open_store = Mock(side_effect=AssertionError("Store IO forbidden"))
        cls.settings = SimpleNamespace(
            database_url="postgresql://unused.invalid/memory-tests",
            deepseek_model="offline-test-model", deepseek_api_key="offline-key",
            deepseek_base_url="https://model.example.invalid/v1",
        )
        memory_store = fake_module(
            "app.modules.memory.store", open_memory_store=cls.open_store,
            memory_namespace=namespace, get_memory=Mock(), save_memory=Mock(),
        )
        with patch.dict(sys.modules, {
            "app.core.config": fake_module("app.core.config", settings=cls.settings),
            "app.modules.memory.store": memory_store,
        }):
            cls.service = load_source("_offline_memory_job_service", "app/modules/memory/services/memory_job.py")
            cls.memory_service = load_source(
                "_offline_memory_lock_service", "app/modules/memory/services/brand_memory.py",
            )

    def setUp(self):
        self.open_store.reset_mock(return_value=True, side_effect=True)
        self.open_store.side_effect = AssertionError("Store IO forbidden")
        self.brief = SimpleNamespace(
            id=uuid4(), team_id=uuid4(), user_id=uuid4(), brand_id=uuid4(), region="eu-na",
            social_media=["linkedin"],
        )
        owned = patch.object(self.service, "require_owned_team_brief", return_value=self.brief)
        self.require_owned = owned.start()
        self.addCleanup(owned.stop)
        self.event = {
            "source_checkpoint_id": "accepted-review-checkpoint",
            "feedback": "  以后所有文案简洁克制。  ", "action": "rejected",
        }
        self.connection = Mock(closed=False, broken=False)
        self.connection.execute.return_value.fetchone.return_value = {"id": uuid4()}
        self.job = {
            "id": uuid4(), "brief_id": self.brief.id, "user_id": self.brief.user_id,
            "brand_id": self.brief.brand_id,
            "source_checkpoint_id": "accepted-review-checkpoint",
            "payload": {"feedback": "以后所有文案简洁克制。", "regions": "eu-na",
                        "platforms": ["linkedin"]},
        }

    def test_enqueue_binds_scope_and_context_from_server_brief_and_is_idempotent(self):
        cursor = Mock()
        hostile_fields = self.event | {
            "user_id": str(uuid4()), "brand_id": str(uuid4()), "regions": "other",
            "platforms": ["youtube"],
        }
        for action in ("approved", "rejected"):
            self.service.enqueue_memory_feedback_events(cursor, self.brief, [hostile_fields | {"action": action}])
        self.assertEqual(cursor.execute.call_count, 2)
        for call in cursor.execute.call_args_list:
            sql, params = call.args
            self.assertIn("ON CONFLICT (brief_id, source_checkpoint_id) DO NOTHING", sql)
            self.assertEqual(params[1:5], (
                self.brief.id, self.brief.user_id, self.brief.brand_id,
                "accepted-review-checkpoint",
            ))
            self.assertEqual(params[5].obj, {
                "feedback": "以后所有文案简洁克制。", "regions": "eu-na",
                "platforms": ["linkedin"],
            })
        uniques = [constraint for constraint in MemoryJob.__table__.constraints
                   if getattr(constraint, "name", None) == "uq_memory_job_feedback"]
        self.assertEqual(len(uniques), 1)
        self.assertEqual(set(uniques[0].columns.keys()), {"brief_id", "source_checkpoint_id"})

    def test_enqueue_ignores_empty_unaccepted_or_unattributed_events(self):
        cursor = Mock()
        bad = [None, "not an event", {}, self.event | {"action": "invalid"},
               self.event | {"feedback": " \n "},
               self.event | {"source_checkpoint_id": " "},
               self.event | {"source_checkpoint_id": "x" * 129}]
        self.service.enqueue_memory_feedback_events(cursor, self.brief, bad)
        cursor.execute.assert_not_called()

    def test_unauthorized_brief_returns_404_without_job_or_store_access(self):
        db = Mock()
        self.require_owned.side_effect = HTTPException(404, "营销任务不存在")
        user = SimpleNamespace(id=uuid4())
        with self.assertRaises(HTTPException) as caught:
            self.service.get_brief_memories(db, user, self.brief.id, team_id=self.brief.team_id)
        self.assertEqual(caught.exception.status_code, 404)
        self.require_owned.assert_called_once_with(
            db, team_id=self.brief.team_id, actor_user_id=user.id, brief_id=self.brief.id,
        )
        db.scalars.assert_not_called()
        self.open_store.assert_not_called()

    def test_authorized_read_releases_orm_transaction_before_scoped_store_pagination(self):
        db = Mock()
        db.scalar.return_value = self.brief
        db.scalars.return_value.all.return_value = []
        source = MemorySource(brief_id=self.brief.id, checkpoint_id="source", quote="以后简洁。")
        memories = [BrandMemory(content=f"偏好 {index}", regions="eu-na", sources=[source])
                    for index in range(101)]
        store = Mock()
        store.search.side_effect = [
            [SimpleNamespace(value=memory.model_dump(mode="json")) for memory in memories[:100]],
            [SimpleNamespace(value=memories[100].model_dump(mode="json"))],
        ]

        def opened():
            db.rollback.assert_called_once_with()
            return nullcontext(store)

        self.open_store.side_effect = opened
        result = self.service.get_brief_memories(db, SimpleNamespace(id=self.brief.user_id), self.brief.id, team_id=self.brief.team_id)
        self.assertEqual(len(result.items), 101)
        self.assertEqual(result.jobs, [])
        self.assertEqual(store.search.call_count, 2)
        for index, call in enumerate(store.search.call_args_list):
            self.assertEqual(call.args[0], namespace(MemoryScope(team_id=self.brief.team_id,
                user_id=self.brief.user_id, brand_profile_id=self.brief.brand_id,
            )))
            self.assertEqual(call.kwargs, {
                "filter": {"status": "active", "duration": "long_term"},
                "limit": 100, "offset": index * 100,
            })
        query = db.scalars.call_args.args[0].compile(dialect=postgresql.dialect())
        self.assertIn("memory_jobs.user_id", str(query))
        self.assertIn(self.brief.user_id, query.params.values())

    def test_worker_scope_lock_is_stable_and_distinct_from_tool_transaction_lock(self):
        scope = MemoryScope(team_id=self.brief.team_id, user_id=self.brief.user_id, brand_profile_id=self.brief.brand_id)
        key = self.service.memory_job_lock_key(scope.user_id, scope.brand_profile_id)
        self.assertEqual(key, self.service.memory_job_lock_key(
            str(scope.user_id).upper(), str(scope.brand_profile_id).upper(),
        ))
        self.assertNotEqual(key, self.memory_service.memory_lock_key(scope))
        self.assertNotEqual(key, self.service.memory_job_lock_key(uuid4(), scope.brand_profile_id))
        self.assertNotEqual(key, self.service.memory_job_lock_key(scope.user_id, uuid4()))
        self.assertGreaterEqual(key, -(2 ** 63))
        self.assertLess(key, 2 ** 63)

    def test_trace_counts_confirmed_results_and_rejects_failed_or_unconfirmed_writes(self):
        trace = self.service.MemoryToolTrace(self.connection, self.job["id"])
        cases = [
            ("create_memory", {"memory_id": str(uuid4()), "version": 1}, "completed"),
            ("update_memory", {"ok": True, "memory_id": str(uuid4()), "version": 2}, "completed"),
            ("invalidate_memory", {"ok": False, "error": "version_conflict"}, "error"),
            ("create_memory", {"message": "pretend success"}, "error"),
            ("search_memory", {"memories": []}, "completed"),
        ]
        import json
        for name, result, expected in cases:
            run_id = uuid4()
            trace.on_tool_start({"name": name}, "{}", run_id=run_id, inputs={"test": name})
            trace.on_tool_end(ToolMessage(content=json.dumps(result), tool_call_id=str(run_id)), run_id=run_id)
            self.assertEqual(trace.calls[-1]["status"], expected)
            self.assertEqual(trace.calls[-1]["result"], result)
        self.assertEqual(self.service.summarize_tool_calls(trace.calls), "新增 1 条；更新 1 条")
        self.assertEqual(self.connection.execute.call_count, len(cases) * 2)

    def test_trace_records_provider_failure_and_lost_running_ownership_propagates(self):
        trace = self.service.MemoryToolTrace(self.connection, self.job["id"])
        run_id = uuid4()
        trace.on_tool_start({"name": "search_memory"}, "{}", run_id=run_id)
        trace.on_tool_error(TimeoutError("secret endpoint"), run_id=run_id)
        self.assertEqual(trace.calls[-1]["status"], "error")
        self.assertEqual(trace.calls[-1]["error"], "TimeoutError")
        self.assertNotIn("secret", str(trace.calls[-1]))
        self.connection.execute.return_value.fetchone.return_value = None
        with self.assertRaisesRegex(RuntimeError, "no longer owns"):
            trace.on_tool_start({"name": "create_memory"}, "{}", run_id=uuid4())

    def run_job(self, actor):
        provider = Mock(return_value=Mock())
        invoke = Mock(side_effect=actor)
        with patch.dict(sys.modules, {
            "langchain_deepseek": fake_module("langchain_deepseek", ChatDeepSeek=provider),
            "app.modules.memory.agent": fake_module(
                "app.modules.memory.agent", run_memory_agent=invoke,
            ),
        }):
            with patch.object(self.service, "_load_job_brief", return_value={"team_id": self.brief.team_id}):
                self.service._run_job(self.connection, self.job)
        invoke.assert_called_once()
        scope, source = invoke.call_args.args
        self.assertEqual(scope, MemoryScope(team_id=self.brief.team_id, user_id=self.brief.user_id, brand_profile_id=self.brief.brand_id))
        self.assertEqual(source, MemorySource(
            brief_id=self.brief.id, checkpoint_id="accepted-review-checkpoint", quote="以后所有文案简洁克制。",
        ))
        self.assertEqual(invoke.call_args.kwargs["regions"], "eu-na")
        self.assertEqual(invoke.call_args.kwargs["platforms"], ["linkedin"])
        sql, params = self.connection.execute.call_args.args
        self.assertIn("AND status = 'running'", sql)
        self.assertEqual(params[-1], self.job["id"])
        return params

    @staticmethod
    def finish_tool(trace, name="create_memory", result=None):
        run_id = uuid4()
        trace.on_tool_start({"name": name}, "{}", run_id=run_id)
        trace.on_tool_end(result or {"memory_id": str(uuid4()), "version": 1}, run_id=run_id)

    def test_no_write_decision_and_successful_write_both_complete(self):
        params = self.run_job(lambda *args, **kwargs: None)
        self.assertEqual(params[0], "completed")
        self.assertEqual(params[1], "本次未写入长期偏好")
        self.assertIsNone(params[2])
        self.connection.reset_mock()
        self.connection.execute.return_value.fetchone.return_value = {"id": self.job["id"]}
        params = self.run_job(lambda *args, **kwargs: self.finish_tool(kwargs["callbacks"][0]))
        self.assertEqual(params[0], "completed")
        self.assertEqual(params[1], "新增 1 条")

    def test_model_failure_before_any_write_is_failed_without_automatic_retry(self):
        def actor(*args, **kwargs):
            raise TimeoutError("secret provider details")
        with self.assertLogs(self.service.logger, level="ERROR"):
            params = self.run_job(actor)
        self.assertEqual(params[0], "failed")
        self.assertNotIn("secret", params[2])

    def test_search_failure_without_mutating_tool_is_failed(self):
        def actor(*args, **kwargs):
            trace = kwargs["callbacks"][0]
            run_id = uuid4()
            trace.on_tool_start({"name": "search_memory"}, "{}", run_id=run_id)
            trace.on_tool_error(TimeoutError(), run_id=run_id)
        params = self.run_job(actor)
        self.assertEqual(params[0], "failed")

    def test_failure_after_confirmed_write_requires_review_without_retry(self):
        def actor(*args, **kwargs):
            self.finish_tool(kwargs["callbacks"][0])
            raise TimeoutError("after committed write")
        with self.assertLogs(self.service.logger, level="ERROR"):
            params = self.run_job(actor)
        self.assertEqual(params[0], "needs_review")
        self.assertEqual(params[1], "新增 1 条")
        self.assertIn("不会自动重跑", params[2])

    def test_interrupted_mutating_tool_requires_review_even_without_returned_result(self):
        def actor(*args, **kwargs):
            kwargs["callbacks"][0].on_tool_start({"name": "update_memory"}, "{}", run_id=uuid4())
            raise ConnectionError("result unknown")
        with self.assertLogs(self.service.logger, level="ERROR"):
            params = self.run_job(actor)
        self.assertEqual(params[0], "needs_review")

    def test_claim_only_pending_jobs_and_respects_scope_lock_and_review_block(self):
        first = self.job
        second = self.job | {"id": uuid4(), "brand_id": uuid4()}
        selected = Mock()
        selected.fetchall.return_value = [first, second]
        unavailable = Mock()
        unavailable.fetchone.return_value = {"acquired": False}
        available = Mock()
        available.fetchone.return_value = {"acquired": True}
        self.connection.transaction.return_value = nullcontext()
        self.connection.execute.side_effect = [selected, unavailable, available, Mock()]
        claimed = self.service._claim_job(self.connection)
        self.assertEqual(claimed, (
            second, self.service.memory_job_lock_key(second["user_id"], second["brand_id"]),
        ))
        sql = self.connection.execute.call_args_list[0].args[0]
        self.assertIn("j.status = 'pending'", sql)
        self.assertIn("'running', 'needs_review'", sql)
        self.assertIn("SKIP LOCKED", sql)
        updates = [call for call in self.connection.execute.call_args_list
                   if "SET status = 'running'" in call.args[0]]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].args[1], (second["id"],))


if __name__ == "__main__":
    unittest.main()
