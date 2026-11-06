"""Accepted review feedback reaches the memory outbox without model or DB IO.

The human-review node and its LangGraph checkpoint are real.  Workflow route
helpers use an isolated graph, and projection tests observe the owned cursor and
transaction boundary rather than opening the application's configured database.
"""

import importlib.util
import json
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.modules.briefs.models import Brief  # Register outside temporary sys.modules patches.
from app.modules.marketing.schemas.marketing_workflow import (
    MarketingWorkflowMiddleOutput,
    MarketingWorkflowReviewInput,
)
from app.modules.marketing.workflow.state import MarketingState


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


class HumanReviewMemoryEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing these nodes must never create a provider client or RAG store.
        with patch.dict(sys.modules, {
            "app.infra.ai.llm": fake_module("app.infra.ai.llm", llm=Mock()),
            "app.modules.rag.service": fake_module(
                "app.modules.rag.service",
                search_documents=Mock(side_effect=AssertionError("RAG IO forbidden")),
            ),
            "app.modules.marketing.workflow.research_agent": fake_module(
                "app.modules.marketing.workflow.research_agent", research_agent=Mock(),
            ),
        }):
            cls.nodes = load_source(
                "_memory_feedback_nodes", "app/modules/marketing/workflow/nodes.py",
            )

    def setUp(self):
        tracing = patch.dict(os.environ, {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
        })
        tracing.start()
        self.addCleanup(tracing.stop)
        self.state = {"revision_count": 1, "content": None, "strategy": "Plan"}

    def resume(self, action="rejected", feedback="以后所有文案都简洁克制。", **extra):
        payload = {
            "action": action, "feedback": feedback,
            "source_checkpoint_id": "reviewed-checkpoint", **extra,
        }
        with patch.object(self.nodes, "interrupt", return_value=payload), \
             patch.object(self.nodes, "review_passed", return_value=True):
            return self.nodes.human_review(self.state)

    def test_temporary_and_long_term_feedback_are_forwarded_without_classification(self):
        for feedback in ("这次改得活泼一点。", "以后所有文案都简洁克制。"):
            with self.subTest(feedback=feedback):
                updates = self.resume(feedback=f"  {feedback}  ")
                self.assertEqual(updates["accepted_memory_events"], [{
                    "source_checkpoint_id": "reviewed-checkpoint",
                    "feedback": feedback, "action": "rejected",
                }])
                self.assertEqual(updates["feedback_log"], [feedback])
                self.assertEqual(updates["revision_count"], 2)

    def test_approval_with_feedback_also_creates_event(self):
        updates = self.resume(action="approved")
        self.assertEqual(updates["decision"], "approved")
        self.assertEqual(updates["accepted_memory_events"][0]["action"], "approved")
        self.assertNotIn("revision_count", updates)

    def test_blank_or_missing_feedback_never_creates_event(self):
        for feedback in (None, "", "  \n "):
            with self.subTest(feedback=feedback):
                updates = self.resume(action="approved", feedback=feedback)
                self.assertFalse(updates.get("accepted_memory_events"))
                self.assertIsNone(updates["human_feedback"])

    def test_legacy_resume_without_source_keeps_feedback_but_creates_no_event(self):
        for checkpoint in (None, ""):
            with self.subTest(checkpoint=checkpoint):
                updates = self.resume(source_checkpoint_id=checkpoint)
                self.assertFalse(updates.get("accepted_memory_events"))
                self.assertEqual(updates["human_feedback"], "以后所有文案都简洁克制。")
                self.assertEqual(updates["feedback_log"], ["以后所有文案都简洁克制。"])

    def test_failed_approval_cannot_accept_memory_feedback(self):
        with patch.object(self.nodes, "interrupt", return_value={
            "action": "approved", "feedback": "以后都简洁。",
            "source_checkpoint_id": "reviewed-checkpoint",
        }), patch.object(self.nodes, "review_passed", return_value=False):
            with self.assertRaisesRegex(ValueError, "自审"):
                self.nodes.human_review(self.state)

    def test_decision_feedback_and_event_accumulate_in_one_real_checkpoint(self):
        previous = {
            "source_checkpoint_id": "older-checkpoint",
            "feedback": "以前的意见", "action": "rejected",
        }
        builder = StateGraph(MarketingState)
        builder.add_node("human_review", self.nodes.human_review)
        builder.add_edge(START, "human_review")
        builder.add_edge("human_review", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": str(uuid4())}}
        graph.invoke(self.state | {
            "feedback_log": ["以前的意见"], "accepted_memory_events": [previous],
        }, config)
        interrupted = graph.get_state(config)
        self.assertTrue(interrupted.interrupts)
        graph.invoke(Command(resume={
            "action": "rejected", "feedback": "以后都简洁。",
            "source_checkpoint_id": interrupted.config["configurable"]["checkpoint_id"],
        }), config)
        accepted = graph.get_state(config)
        self.assertFalse(accepted.interrupts)
        self.assertEqual(accepted.values["decision"], "rejected")
        self.assertEqual(accepted.values["human_feedback"], "以后都简洁。")
        self.assertEqual(accepted.values["feedback_log"], ["以前的意见", "以后都简洁。"])
        self.assertEqual(accepted.values["accepted_memory_events"], [previous, {
            "source_checkpoint_id": interrupted.config["configurable"]["checkpoint_id"],
            "feedback": "以后都简洁。", "action": "rejected",
        }])


class WorkflowMemoryOutboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = Mock()
        cls.enqueue = Mock()
        with patch.dict(sys.modules, {
            "app.modules.marketing.workflow.graph": fake_module(
                "app.modules.marketing.workflow.graph", graph=cls.graph,
                MAX_SELF_REVIEW_ATTEMPTS=3,
                revisions_exhausted=lambda count: count >= 3,
            ),
            "app.modules.marketing.workflow.nodes": fake_module(
                "app.modules.marketing.workflow.nodes", NO_KNOWLEDGE="没有品牌资料",
                MAX_REVISION_PREFIX="已达到最大修改次数",
                SELF_REVIEW_EXHAUSTED_PREFIX="模型自审多次未通过",
            ),
            "app.modules.memory.services.memory_job": fake_module(
                "app.modules.memory.services.memory_job",
                enqueue_memory_feedback_events=cls.enqueue,
            ),
        }):
            cls.service = load_source(
                "_memory_feedback_workflow_service",
                "app/modules/marketing/service.py",
            )

    def setUp(self):
        self.graph.reset_mock(return_value=True, side_effect=True)
        self.enqueue.reset_mock(return_value=True, side_effect=True)
        # Production imports this boundary lazily only when head events exist.
        jobs = patch.dict(sys.modules, {
            "app.modules.memory.services.memory_job": fake_module(
                "app.modules.memory.services.memory_job",
                enqueue_memory_feedback_events=self.enqueue,
            ),
        })
        jobs.start()
        self.addCleanup(jobs.stop)
        self.brief = SimpleNamespace(
            id=uuid4(), team_id=uuid4(), user_id=uuid4(), brand_id=uuid4(), workflow_version=4,
            region="eu-na", social_media=["linkedin"],
        )
        scope = patch.object(self.service, "load_owned_team_brief_cursor", return_value={})
        scope.start()
        self.addCleanup(scope.stop)
        self.events = [{
            "source_checkpoint_id": "reviewed-checkpoint", "feedback": "以后简洁。",
            "action": "rejected",
        }]
        self.snapshot = SimpleNamespace(
            config={"configurable": {"checkpoint_id": "current-head"}},
            interrupts=(True,), values={"team_id": self.brief.team_id, "actor_user_id": self.brief.user_id, "revision_count": 1,
                                        "accepted_memory_events": self.events},
            next=("human_review",),
        )
        self.graph.get_state.return_value = self.snapshot
        self.response = MarketingWorkflowMiddleOutput(
            brief_id=self.brief.id, checkpoint_id="current-head",
            status="awaiting_review", revision_count=1,
        )
        self.cursor = Mock()
        self.cursor.fetchone.side_effect = [
            {"workflow_version": 4}, {"workflow_version": 5},
        ]
        self.transaction_open = False
        self.transaction_failed = False
        self.guard = SimpleNamespace(transaction=self.transaction)

    @contextmanager
    def transaction(self):
        self.transaction_open = True
        try:
            yield self.cursor
        except BaseException:
            self.transaction_failed = True
            raise
        finally:
            self.transaction_open = False

    def projection_patches(self):
        return (
            patch.object(self.service, "require_workflow_execution", return_value=self.guard),
            patch.object(self.service, "to_response", return_value=self.response),
        )

    def test_sync_and_stream_review_pass_validated_source_checkpoint_to_node(self):
        for streaming in (False, True):
            for action in ("approved", "rejected"):
                with self.subTest(streaming=streaming, action=action):
                    self.graph.reset_mock(return_value=True, side_effect=True)
                    self.graph.get_state.return_value = self.snapshot
                    self.graph.stream.return_value = iter(())
                    payload = MarketingWorkflowReviewInput(
                        brief_id=self.brief.id, expected_checkpoint_id="current-head",
                        action=action, feedback="以后简洁。",
                    )
                    with patch.object(self.service, "workflow_execution", return_value=nullcontext(self.guard)), \
                         patch.object(self.service, "_load_execution_brief", return_value=self.brief), \
                         patch.object(self.service, "review_passed", return_value=True), \
                         patch.object(self.service, "persist_snapshot", return_value=self.response):
                        if streaming:
                            lines = list(self.service.iter_review_stream(self.brief.user_id, payload, team_id=self.brief.team_id))
                            decoded = [json.loads(line.splitlines()[0]) for line in lines]
                            self.assertEqual(decoded[-1]["type"], "done")
                            command = self.graph.stream.call_args.args[0]
                        else:
                            self.assertIs(self.service.review(self.brief.user_id, payload, team_id=self.brief.team_id), self.response)
                            command = self.graph.invoke.call_args.args[0]
                    self.assertEqual(command.resume, {
                        "action": action, "feedback": "以后简洁。",
                        "source_checkpoint_id": "current-head",
                    })

    def test_stale_review_rejected_before_graph_or_outbox_in_both_paths(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                payload = MarketingWorkflowReviewInput(
                    brief_id=self.brief.id, expected_checkpoint_id="stale-head",
                    action="rejected", feedback="以后简洁。",
                )
                with patch.object(self.service, "workflow_execution", return_value=nullcontext(self.guard)), \
                     patch.object(self.service, "_load_execution_brief", return_value=self.brief):
                    if streaming:
                        events = [json.loads(line.splitlines()[0]) for line in
                                  self.service.iter_review_stream(self.brief.user_id, payload, team_id=self.brief.team_id)]
                        self.assertEqual(events[0]["type"], "error")
                        self.assertEqual(events[0]["status"], 409)
                    else:
                        with self.assertRaises(HTTPException) as caught:
                            self.service.review(self.brief.user_id, payload, team_id=self.brief.team_id)
                        self.assertEqual(caught.exception.status_code, 409)
                self.graph.invoke.assert_not_called()
                self.graph.stream.assert_not_called()
                self.enqueue.assert_not_called()

    def test_projection_enqueues_head_events_with_server_brief_on_same_cursor(self):
        def enqueue(cursor, brief, events):
            self.assertTrue(self.transaction_open)
            self.assertIs(cursor, self.cursor)
            self.assertIs(brief, self.brief)
            self.assertIs(events, self.events)
            self.assertTrue(any("UPDATE briefs" in call.args[0]
                                for call in self.cursor.execute.call_args_list))
            self.assertEqual(self.brief.workflow_version, 4)

        self.enqueue.side_effect = enqueue
        first, second = self.projection_patches()
        with first, second:
            result = self.service.persist_snapshot(self.brief, self.snapshot)
        self.assertIs(result, self.response)
        self.enqueue.assert_called_once()
        self.assertFalse(self.transaction_open)
        self.assertFalse(self.transaction_failed)
        self.assertEqual(self.brief.workflow_version, 5)

    def test_old_head_is_rejected_before_transaction_or_enqueue(self):
        old = SimpleNamespace(config={"configurable": {"checkpoint_id": "stale-head"}})
        first, second = self.projection_patches()
        with first, second, self.assertRaises(HTTPException) as caught:
            self.service.persist_snapshot(self.brief, old)
        self.assertEqual(caught.exception.status_code, 409)
        self.cursor.execute.assert_not_called()
        self.enqueue.assert_not_called()

    def test_old_projection_version_is_rejected_before_enqueue(self):
        self.cursor.fetchone.side_effect = [{"workflow_version": 6}]
        first, second = self.projection_patches()
        with first, second, self.assertRaises(HTTPException) as caught:
            self.service.persist_snapshot(self.brief, self.snapshot)
        self.assertEqual(caught.exception.status_code, 409)
        self.enqueue.assert_not_called()
        self.assertTrue(self.transaction_failed)
        self.assertEqual(self.brief.workflow_version, 4)
        self.assertFalse(any("UPDATE briefs" in call.args[0]
                             for call in self.cursor.execute.call_args_list))

    def test_lost_update_cannot_enqueue(self):
        self.cursor.fetchone.side_effect = [{"workflow_version": 4}, None]
        first, second = self.projection_patches()
        with first, second, self.assertRaises(HTTPException) as caught:
            self.service.persist_snapshot(self.brief, self.snapshot)
        self.assertEqual(caught.exception.status_code, 409)
        self.enqueue.assert_not_called()
        self.assertTrue(self.transaction_failed)

    def test_missing_brief_cannot_enqueue(self):
        self.cursor.fetchone.side_effect = [None]
        first, second = self.projection_patches()
        with first, second, self.assertRaises(HTTPException) as caught:
            self.service.persist_snapshot(self.brief, self.snapshot)
        self.assertEqual(caught.exception.status_code, 404)
        self.enqueue.assert_not_called()

    def test_enqueue_failure_aborts_projection_and_does_not_advance_local_version(self):
        self.enqueue.side_effect = RuntimeError("outbox insert failed")
        first, second = self.projection_patches()
        with first, second, self.assertRaisesRegex(RuntimeError, "outbox insert failed"):
            self.service.persist_snapshot(self.brief, self.snapshot)
        self.assertTrue(self.transaction_failed)
        self.assertFalse(self.transaction_open)
        self.assertEqual(self.brief.workflow_version, 4)

    def test_older_checkpoint_without_events_remains_compatible(self):
        self.snapshot.values.pop("accepted_memory_events")
        first, second = self.projection_patches()
        with first, second:
            self.service.persist_snapshot(self.brief, self.snapshot)
        # Both a no-op enqueue and omitting it are compatible with old checkpoints.
        if self.enqueue.called:
            self.assertFalse(self.enqueue.call_args.args[2])
        self.assertEqual(self.brief.workflow_version, 5)


if __name__ == "__main__":
    unittest.main()
