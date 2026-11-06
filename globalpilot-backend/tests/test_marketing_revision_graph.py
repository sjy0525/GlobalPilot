"""Offline execution of the revision check graph with injected reviewers."""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from app.modules.marketing.schemas.revision import SemanticValidationResult
from app.modules.marketing.workflow.revision.graph import build_revision_graph
from app.modules.marketing.workflow.revision.nodes import validate_candidate_node
from tests.test_marketing_revision_semantic import (
    passing_report,
    response_for,
    review_candidate,
    review_context,
    review_task,
)


def revision_state():
    task = review_task()
    return {
        "task": task.model_dump(mode="json"),
        "candidate": review_candidate(task).model_dump(mode="json"),
        "review_context": review_context().model_dump(mode="json"),
    }


class RevisionGraphTests(unittest.TestCase):
    def setUp(self):
        self.state = revision_state()

    def run_report(self, report):
        reviewer = Mock(return_value=response_for(report))
        output = build_revision_graph(reviewer=reviewer).invoke(self.state)
        reviewer.assert_called_once()
        return output

    def test_passing_graph_returns_complete_json_report_and_preserves_inputs(self):
        before = deepcopy(self.state)
        output = self.run_report(passing_report())

        self.assertEqual(self.state, before)
        for key in ("task", "candidate", "review_context"):
            self.assertEqual(output[key], before[key])
        report = SemanticValidationResult.model_validate(output["semantic_validation"])
        self.assertEqual(report.status, "passed")
        self.assertEqual(report.issues, [])
        self.assertEqual(str(report.candidate_id), self.state["candidate"]["candidate_id"])
        self.assertIsNotNone(report.model_review)
        self.assertEqual(json.loads(json.dumps(output, ensure_ascii=False)), output)

    def test_unmet_goal_is_returned_as_needs_revision_without_a_loop(self):
        report = passing_report()
        report["goal"].update(verdict="unmet", note="请继续精简重复表达")

        output = self.run_report(report)

        self.assertEqual(output["semantic_validation"]["status"], "needs_revision")
        self.assertTrue(any("精简重复表达" in issue for issue in output["semantic_validation"]["issues"]))

    def test_missing_information_is_returned_as_needs_clarification(self):
        report = passing_report()
        report["uncertainties"] = ["请确认品牌语气适用的受众"]

        output = self.run_report(report)

        self.assertEqual(output["semantic_validation"]["status"], "needs_clarification")
        self.assertTrue(any("受众" in issue for issue in output["semantic_validation"]["issues"]))

    def test_invalid_citation_is_returned_as_error(self):
        report = passing_report()
        report["blocks"][0]["facts"][0]["references"][0]["evidence_id"] = "missing-evidence"

        output = self.run_report(report)

        self.assertEqual(output["semantic_validation"]["status"], "error")
        self.assertTrue(any("missing-evidence" in issue for issue in output["semantic_validation"]["issues"]))

    def test_program_failure_skips_reviewer_and_keeps_candidate(self):
        self.state["candidate"]["base_version"] += 1
        before = deepcopy(self.state)
        reviewer = Mock(side_effect=AssertionError("must not call"))

        output = build_revision_graph(reviewer=reviewer).invoke(self.state)

        reviewer.assert_not_called()
        self.assertEqual(self.state, before)
        self.assertEqual(output["candidate"], before["candidate"])
        report = output["semantic_validation"]
        self.assertEqual(report["status"], "needs_revision")
        self.assertIsNone(report["model_review"])
        self.assertTrue(any("版本" in issue for issue in report["issues"]))

    def test_truncated_response_stops_with_error_after_one_model_call(self):
        reviewer = Mock(return_value=response_for(passing_report(), finish_reason="length"))

        output = build_revision_graph(reviewer=reviewer).invoke(self.state)

        reviewer.assert_called_once()
        self.assertEqual(output["semantic_validation"]["status"], "error")
        self.assertIsNone(output["semantic_validation"]["model_review"])

    def test_provider_failure_overwrites_old_passed_report(self):
        old_report = {"status": "passed", "old_only": "must disappear"}
        self.state["semantic_validation"] = old_report
        before = deepcopy(self.state)
        reviewer = Mock(side_effect=RuntimeError("private provider detail"))

        with self.assertLogs("app.modules.marketing.workflow.revision.validation", level="ERROR"):
            output = build_revision_graph(reviewer=reviewer).invoke(self.state)

        reviewer.assert_called_once()
        self.assertEqual(self.state, before)
        report = output["semantic_validation"]
        self.assertEqual(report["status"], "error")
        self.assertNotIn("old_only", report)
        self.assertNotIn("private provider detail", " ".join(report["issues"]))

    def test_node_returns_only_report_update_and_does_not_mutate_state(self):
        self.state["semantic_validation"] = {"status": "old-report"}
        before = deepcopy(self.state)
        reviewer = Mock(return_value=response_for(passing_report()))

        update = validate_candidate_node(self.state, reviewer=reviewer)

        reviewer.assert_called_once()
        self.assertEqual(set(update), {"semantic_validation"})
        self.assertEqual(update["semantic_validation"]["status"], "passed")
        self.assertEqual(self.state, before)

    def test_repeated_invokes_do_not_leak_previous_reports(self):
        failing_report = passing_report()
        failing_report["goal"].update(verdict="unmet", note="第一轮需要精简")
        reviewer = Mock(side_effect=[response_for(failing_report), response_for(passing_report())])
        graph = build_revision_graph(reviewer=reviewer)

        first = graph.invoke(self.state)
        second_state = revision_state()
        second_state["candidate"]["candidate_id"] = "40000000-0000-4000-8000-000000000002"
        second = graph.invoke(second_state)

        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(first["semantic_validation"]["status"], "needs_revision")
        self.assertEqual(second["semantic_validation"]["status"], "passed")
        self.assertEqual(second["semantic_validation"]["issues"], [])
        self.assertNotEqual(first["semantic_validation"]["candidate_id"], second["semantic_validation"]["candidate_id"])
        self.assertNotEqual(first["semantic_validation"]["input_hash"], second["semantic_validation"]["input_hash"])

    def test_invalid_payloads_are_rejected_before_reviewer(self):
        states = []
        invalid_task = revision_state()
        invalid_task["task"]["draft"]["version"] = "12"
        states.append(invalid_task)
        invalid_candidate = revision_state()
        invalid_candidate["candidate"]["blocks"] = []
        states.append(invalid_candidate)
        invalid_context = revision_state()
        invalid_context["review_context"]["evidence"][1]["evidence_id"] = "power-1"
        states.append(invalid_context)

        for state in states:
            with self.subTest(state=state):
                reviewer = Mock(side_effect=AssertionError("must not call"))
                graph = build_revision_graph(reviewer=reviewer)
                with self.assertRaises(ValidationError):
                    graph.invoke(state)
                reviewer.assert_not_called()

    def test_missing_required_state_is_rejected_before_reviewer(self):
        for key in ("task", "candidate", "review_context"):
            with self.subTest(missing=key):
                state = revision_state()
                del state[key]
                reviewer = Mock(side_effect=AssertionError("must not call"))
                graph = build_revision_graph(reviewer=reviewer)
                with self.assertRaises(KeyError):
                    graph.invoke(state)
                reviewer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
