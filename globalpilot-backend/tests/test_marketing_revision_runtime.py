"""Offline full revision lifecycle, interrupts and bounded repair execution."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.modules.marketing.schemas.revision import (
    DraftCandidate,
    RevisionRepairOutput,
)
from app.modules.marketing.workflow.revision.agent import invoke_revision_reviser
from app.modules.marketing.workflow.revision.graph import build_revision_graph
from app.modules.marketing.workflow.revision.nodes import revise_candidate_node
from app.modules.marketing.workflow.revision.validation import validate_semantics
from tests.test_marketing_revision_semantic import (
    FIRST_BLOCK_ID,
    SECOND_BLOCK_ID,
    passing_report,
    response_for,
    review_context,
    review_task,
)


def revision_output(text="支持 65W 输出，充电更顺手。", *, second=None):
    blocks = [{"block_id": FIRST_BLOCK_ID, "text": text}]
    if second is not None:
        blocks.append({"block_id": SECOND_BLOCK_ID, "text": second})
    return RevisionRepairOutput.model_validate({"blocks": blocks})


def failed_report(*, two_editable=False):
    report = passing_report(two_editable=two_editable)
    report["goal"].update(verdict="unmet", note="请继续精简重复表达")
    return report


class RevisionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.task = review_task()
        self.context = review_context()
        self.reader = Mock(return_value=self.task)
        self.searcher = Mock(return_value=self.context)
        self.saver = InMemorySaver()
        self.config = {"configurable": {"thread_id": str(uuid4())}}

    def graph(self, *, reviewer=None, reviser=None, maximum=3):
        self.reviewer = reviewer if reviewer is not None else Mock(return_value=response_for(passing_report()))
        self.reviser = reviser if reviser is not None else Mock(return_value=revision_output())
        return build_revision_graph(
            reviewer=self.reviewer,
            reviser=self.reviser,
            max_revisions=maximum,
            interactive=True,
            checkpointer=self.saver,
            document_reader=self.reader,
            evidence_searcher=self.searcher,
        )

    def test_initial_generation_pauses_for_review_then_reconstructed_graph_accepts(self):
        original = self.task.model_dump(mode="json")
        graph = self.graph()

        output = graph.invoke({}, self.config)

        self.assertEqual(output["status"], "awaiting_review")
        self.assertEqual(output["revision_attempts"], 0)
        self.assertEqual(output["generation_attempts"], 1)
        self.assertEqual(output["__interrupt__"][0].value["kind"], "review")
        self.assertEqual(output["candidate"]["blocks"][1], original["draft"]["blocks"][1])
        self.assertEqual(self.task.model_dump(mode="json"), original)
        self.reader.assert_called_once()
        self.searcher.assert_called_once()
        query = self.searcher.call_args.args[0]
        self.assertIn(self.task.requirements.goal, query)
        self.assertIn("65W", query)
        self.assertNotIn("30 天退货", query)
        self.assertEqual([item["kind"] for item in output["attempt_history"]], [
            "read_document", "search_brand_evidence", "initial", "validate",
        ])

        # Rebuild the process-local graph object, keeping only its checkpointer.
        restored = self.graph(reviewer=self.reviewer, reviser=self.reviser)
        final = restored.invoke(Command(resume={"decision": "approved", "feedback": "采纳"}), self.config)

        self.assertEqual(final["status"], "accepted")
        self.assertEqual(final["review_feedback"], "采纳")
        self.assertEqual(final["generation_attempts"], 1)
        self.assertEqual(final["original_requirements"], original["requirements"])
        self.reviser.assert_called_once()
        self.reviewer.assert_called_once()

    def test_initial_no_change_is_still_checked_and_can_be_reviewed(self):
        original_text = self.task.draft.blocks[0].text
        graph = self.graph(reviser=Mock(return_value=revision_output(original_text)))
        result = graph.invoke({}, self.config)
        self.assertEqual(result["status"], "awaiting_review")
        self.reviewer.assert_called_once()

    def test_human_rejection_is_terminal_and_preserves_candidate(self):
        graph = self.graph()
        initial = graph.invoke({}, self.config)
        result = graph.invoke(Command(resume={"action": "rejected", "feedback": "语气不合适"}), self.config)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["candidate"], initial["candidate"])
        self.assertEqual(result["review_feedback"], "语气不合适")
        self.reviser.assert_called_once()

    def test_empty_evidence_pauses_before_model_then_clarification_generates(self):
        empty = self.context.model_copy(update={"evidence": []})
        self.searcher.return_value = empty
        graph = self.graph()
        result = graph.invoke({}, self.config)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["__interrupt__"][0].value["kind"], "clarification")
        self.reviser.assert_not_called()
        self.reviewer.assert_not_called()
        updated_task = self.task.model_copy(deep=True)
        updated_task.requirements.goal += "；请使用自然语气"
        result = graph.invoke(Command(resume={
            "task": updated_task.model_dump(mode="json"),
            "review_context": self.context.model_dump(mode="json"),
            "feedback": "已补充资料",
        }), self.config)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["revision_attempts"], 0)
        self.assertEqual(result["original_requirements"]["goal"], self.task.requirements.goal)
        self.assertIn("自然语气", result["task"]["requirements"]["goal"])
        self.assertEqual(len(result["clarification_history"]), 1)
        self.reader.assert_called_once()

    def test_first_generation_can_request_clarification_without_a_candidate(self):
        reviser = Mock(side_effect=[
            RevisionRepairOutput(clarification_questions=["必须保留内容超过长度上限，请确认要求"]),
            revision_output(),
        ])
        graph = self.graph(reviser=reviser)
        result = graph.invoke({}, self.config)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertFalse(result.get("candidate"))
        self.assertEqual(result["generation_attempts"], 1)
        self.assertIn("超过长度上限", result["__interrupt__"][0].value["issues"][0])
        self.reviewer.assert_not_called()
        result = graph.invoke(Command(resume={
            "task": self.task.model_dump(mode="json"),
            "review_context": self.context.model_dump(mode="json"),
            "feedback": "确认保持限制",
        }), self.config)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["generation_attempts"], 2)
        self.assertEqual(result["revision_attempts"], 0)

    def test_three_repairs_exhaust_budget_and_keep_current_report(self):
        def changing(messages):
            data = json.loads(messages[1][1])
            return revision_output(data["blocks"][0]["candidate_text"] + "更简洁。")

        graph = self.graph(
            reviewer=Mock(return_value=response_for(failed_report())),
            reviser=Mock(side_effect=changing),
        )
        result = graph.invoke({}, self.config)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["revision_attempts"], 3)
        self.assertEqual(result["generation_attempts"], 4)
        self.assertEqual(result["semantic_validation"]["status"], "needs_revision")
        self.assertEqual(self.reviser.call_count, 4)
        self.assertEqual(self.reviewer.call_count, 4)
        self.assertEqual(len([event for event in result["attempt_history"] if event["kind"] == "repair"]), 3)

    def test_repair_clarification_preserves_budget_candidate_and_both_issue_lists(self):
        graph = self.graph(
            reviewer=Mock(side_effect=[response_for(failed_report()), response_for(failed_report()), response_for(passing_report())]),
            reviser=Mock(side_effect=[
                revision_output(),
                RevisionRepairOutput(clarification_questions=["请确认品牌语气"]),
                revision_output("支持 65W 输出，充电顺手。"),
            ]),
        )
        result = graph.invoke({}, self.config)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["revision_attempts"], 1)
        self.assertIsNotNone(result["candidate"])
        questions = result["__interrupt__"][0].value["issues"]
        self.assertTrue(any("品牌语气" in item for item in questions))
        self.assertTrue(any("精简" in item for item in questions))
        result = graph.invoke(Command(resume={
            "task": self.task.model_dump(mode="json"),
            "review_context": self.context.model_dump(mode="json"),
            "feedback": "语气自然",
        }), self.config)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["revision_attempts"], 2)

    def test_program_failures_repair_without_calling_reviewer_on_invalid_copy(self):
        graph = self.graph(reviser=Mock(side_effect=[revision_output("充电更顺手。"), revision_output()]))
        result = graph.invoke({}, self.config)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["revision_attempts"], 1)
        self.reviewer.assert_called_once()

    def test_repair_no_change_and_provider_failure_preserve_current_candidate(self):
        for failure in ("no_change", "provider"):
            with self.subTest(failure=failure):
                self.config = {"configurable": {"thread_id": str(uuid4())}}
                reviser = Mock(side_effect=[revision_output(), revision_output() if failure == "no_change" else RuntimeError("private error")])
                graph = self.graph(reviewer=Mock(return_value=response_for(failed_report())), reviser=reviser)
                with patch("app.modules.marketing.workflow.revision.nodes.logger"):
                    result = graph.invoke({}, self.config)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["stop_reason"], "no_change" if failure == "no_change" else "repair_error")
                self.assertEqual(result["candidate"]["blocks"][0]["text"], revision_output().blocks[0].text)
                self.assertEqual(result["revision_attempts"], 1)
                self.assertNotIn("private error", str(result["execution_issues"]))

    def test_generation_and_review_errors_do_not_trigger_repairs(self):
        for failure in ("generation", "review"):
            with self.subTest(failure=failure):
                self.config = {"configurable": {"thread_id": str(uuid4())}}
                reviewer = Mock(return_value=SimpleNamespace(content="{}", response_metadata={"finish_reason": "length"}))
                reviser = Mock(side_effect=RuntimeError("upstream")) if failure == "generation" else Mock(return_value=revision_output())
                graph = self.graph(reviewer=reviewer, reviser=reviser)
                with patch("app.modules.marketing.workflow.revision.nodes.logger"):
                    result = graph.invoke({}, self.config)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["revision_attempts"], 0)
                reviser.assert_called_once()
                if failure == "generation":
                    reviewer.assert_not_called()

    def test_all_allowed_blocks_are_required_and_previous_edits_survive_repairs(self):
        self.task = review_task(two_editable=True)
        self.reader.return_value = self.task
        initial = revision_output(second="本产品支持 30 天退货。")
        fixed = revision_output("支持 65W 输出，充电顺手。", second="本产品支持 30 天退货。")
        graph = self.graph(
            reviewer=Mock(side_effect=[response_for(failed_report(two_editable=True)), response_for(passing_report(two_editable=True))]),
            reviser=Mock(side_effect=[initial, fixed]),
        )
        result = graph.invoke({}, self.config)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["candidate"]["blocks"][1]["text"], "本产品支持 30 天退货。")
        bad = self.graph(reviser=Mock(return_value=revision_output()))
        with patch("app.modules.marketing.workflow.revision.nodes.logger"):
            result = bad.invoke({}, {"configurable": {"thread_id": str(uuid4())}})
        self.assertEqual(result["stop_reason"], "generation_error")

    def test_clarification_cannot_change_original_snapshot_or_scope(self):
        self.searcher.return_value = self.context.model_copy(update={"evidence": []})
        graph = self.graph()
        graph.invoke({}, self.config)
        changed = self.task.model_copy(deep=True)
        changed.draft.blocks[0].text = "支持 65W 输出，替换原稿。"
        with self.assertRaisesRegex(ValueError, "不能更换原稿"):
            graph.invoke(Command(resume={
                "task": changed.model_dump(mode="json"),
                "review_context": self.context.model_dump(mode="json"),
            }), self.config)
        self.reviser.assert_not_called()

    def test_approval_of_stale_or_out_of_scope_final_candidate_is_blocked(self):
        graph = self.graph()
        initial = graph.invoke({}, self.config)
        edited = deepcopy(initial["candidate"])
        edited["blocks"][1]["text"] = "范围外修改"
        final = graph.invoke(Command(resume={"decision": "approved", "candidate": edited}), self.config)
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["stop_reason"], "approval_validation_error")
        self.assertEqual(final["candidate"], initial["candidate"])

    def test_checked_human_final_candidate_is_preserved_on_acceptance(self):
        graph = self.graph()
        initial = graph.invoke({}, self.config)
        candidate = DraftCandidate.model_validate(initial["candidate"])
        candidate.candidate_id = uuid4()
        candidate.blocks[0].text = "支持 65W 输出，充电顺手。"
        report = validate_semantics(self.task, candidate, self.context, reviewer=Mock(return_value=response_for(passing_report())))
        final = graph.invoke(Command(resume={
            "decision": "approved",
            "candidate": candidate.model_dump(mode="json"),
            "semantic_validation": report.model_dump(mode="json"),
        }), self.config)
        self.assertEqual(final["status"], "accepted")
        self.assertEqual(final["candidate"], candidate.model_dump(mode="json"))

    def test_stale_report_cannot_spend_a_repair_attempt(self):
        graph = self.graph()
        initial = graph.invoke({}, self.config)
        state = deepcopy(initial)
        state["semantic_validation"]["status"] = "needs_revision"
        state["semantic_validation"]["issues"] = ["需要修改"]
        state["candidate"]["blocks"][0]["text"] += "内容已经变更"
        reviser = Mock()
        update = revise_candidate_node(state, reviser=reviser)
        self.assertEqual(update["stop_reason"], "stale_report")
        self.assertNotIn("revision_attempts", update)
        reviser.assert_not_called()


class RevisionReviserAdapterTests(unittest.TestCase):
    def test_adapter_uses_json_timeout_and_rejects_incomplete_responses(self):
        import sys

        provider = Mock()
        response = SimpleNamespace(
            content=revision_output().model_dump_json(),
            response_metadata={"finish_reason": "stop"},
        )
        provider.bind.return_value.invoke.return_value = response
        with patch.dict(sys.modules, {"app.infra.ai.llm": SimpleNamespace(llm=provider)}):
            self.assertEqual(invoke_revision_reviser([("human", "json")]), revision_output())
            provider.bind.assert_called_with(timeout=60.0, max_tokens=4096, response_format={"type": "json_object"})
            response.response_metadata["finish_reason"] = "length"
            with self.assertRaises(ValueError):
                invoke_revision_reviser([("human", "json")])


if __name__ == "__main__":
    unittest.main()
