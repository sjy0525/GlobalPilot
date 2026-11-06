"""Offline semantic-review contracts and execution with injected model responses."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from pydantic import ValidationError

from app.modules.marketing.schemas.revision import (
    BrandEvidence,
    DraftCandidate,
    ModelSemanticReview,
    RevisionReviewContext,
    RevisionTaskInput,
    SemanticValidationResult,
)
from app.modules.marketing.workflow.revision.validation import (
    semantic_input_hash,
    validate_semantics,
)


FIRST_BLOCK_ID = "10000000-0000-4000-8000-000000000001"
SECOND_BLOCK_ID = "10000000-0000-4000-8000-000000000002"
UNKNOWN_BLOCK_ID = "10000000-0000-4000-8000-000000000003"


def review_task(*, two_editable=False):
    return RevisionTaskInput.model_validate({
        "draft": {
            "draft_id": "20000000-0000-4000-8000-000000000001",
            "version": 12,
            "source_brief_id": "30000000-0000-4000-8000-000000000001",
            "source_checkpoint_id": "checkpoint-12",
            "platform": "小红书",
            "blocks": [
                {"block_id": FIRST_BLOCK_ID, "text": "这款充电器支持 65W 输出，日常充电使用更顺手。"},
                {"block_id": SECOND_BLOCK_ID, "text": "支持 30 天退货。"},
            ],
        },
        "requirements": {
            "goal": "表达更简洁，保留功率信息",
            "allowed_block_ids": [FIRST_BLOCK_ID, SECOND_BLOCK_ID] if two_editable else [FIRST_BLOCK_ID],
            "must_keep": ["65W", "30 天退货"],
        },
    })


def review_candidate(task):
    blocks = task.draft.model_dump(mode="json")["blocks"]
    blocks[0]["text"] = "支持 65W 输出，日常充电更顺手。"
    return DraftCandidate.model_validate({
        "candidate_id": "40000000-0000-4000-8000-000000000001",
        "draft_id": task.draft.draft_id,
        "base_version": task.draft.version,
        "blocks": blocks,
    })


def review_context():
    return RevisionReviewContext.model_validate({
        "rules_version": "brand-v3",
        "brand_rules": ["表达自然简洁", "避免夸张承诺"],
        "evidence": [
            {
                "evidence_id": "power-1",
                "document_id": "50000000-0000-4000-8000-000000000001",
                "document_version": 3,
                "source": "测试参数表",
                "quote": "该充电器支持 65W 输出。",
            },
            {
                "evidence_id": "returns-1",
                "document_id": "50000000-0000-4000-8000-000000000002",
                "document_version": 2,
                "source": "测试售后政策",
                "quote": "本产品支持 30 天退货。",
            },
        ],
    })


def passing_report(*, two_editable=False):
    blocks = [{
        "block_id": FIRST_BLOCK_ID,
        "tone": {"verdict": "met", "note": "表达自然清晰"},
        "facts": [{
            "claim": "支持 65W 输出",
            "verdict": "supported",
            "references": [{"evidence_id": "power-1", "quote": "支持 65W 输出"}],
            "note": "参数表直接支持输出功率",
        }],
    }]
    if two_editable:
        blocks.append({
            "block_id": SECOND_BLOCK_ID,
            "tone": {"verdict": "met", "note": "退货说明清晰"},
            "facts": [{
                "claim": "支持 30 天退货",
                "verdict": "supported",
                "references": [{"evidence_id": "returns-1", "quote": "支持 30 天退货"}],
                "note": "售后政策支持该说明",
            }],
        })
    return {
        "goal": {"verdict": "met", "note": "精简了表达并保留功率"},
        "rules": [
            {"rule_index": 0, "verdict": "met", "note": "表达自然简洁"},
            {"rule_index": 1, "verdict": "met", "note": "没有夸张承诺"},
        ],
        "blocks": blocks,
        "uncertainties": [],
    }


def response_for(report, *, finish_reason="stop"):
    return SimpleNamespace(
        content=json.dumps(report, ensure_ascii=False),
        response_metadata={"finish_reason": finish_reason},
    )


class SemanticReviewSchemaTests(unittest.TestCase):
    def test_context_json_round_trip_preserves_evidence_identity_and_exact_quotes(self):
        context = review_context()
        context.evidence[0].quote = "  该充电器支持 65W 输出。\n"
        restored = RevisionReviewContext.model_validate_json(context.model_dump_json())
        self.assertEqual(restored, context)
        self.assertIsInstance(restored.evidence[0].document_id, UUID)
        self.assertEqual(restored.evidence[0].quote, "  该充电器支持 65W 输出。\n")

    def test_evidence_version_requires_strict_positive_integer(self):
        evidence = review_context().evidence[0].model_dump(mode="json")
        for value in (True, False, "3", 3.0, 0, -1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as raised:
                    BrandEvidence.model_validate(dict(evidence, document_version=value))
                self.assertTrue(any(error["loc"] == ("document_version",) for error in raised.exception.errors()))

    def test_required_evidence_text_rejects_whitespace(self):
        evidence = review_context().evidence[0].model_dump(mode="json")
        for field in ("evidence_id", "source", "quote"):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError) as raised:
                    BrandEvidence.model_validate(dict(evidence, **{field: " \n\t"}))
                self.assertTrue(any(error["loc"] == (field,) for error in raised.exception.errors()))

    def test_duplicate_evidence_ids_are_rejected(self):
        context = review_context().model_dump(mode="json")
        context["evidence"][1]["evidence_id"] = context["evidence"][0]["evidence_id"]
        with self.assertRaises(ValidationError) as raised:
            RevisionReviewContext.model_validate(context)
        self.assertTrue(any(error["loc"] == ("evidence",) for error in raised.exception.errors()))

    def test_brand_rules_and_version_reject_blank_text(self):
        for field, value in (("rules_version", " \n"), ("brand_rules", [" \t"])):
            with self.subTest(field=field):
                data = review_context().model_dump(mode="json")
                data[field] = value
                with self.assertRaises(ValidationError) as raised:
                    RevisionReviewContext.model_validate(data)
                self.assertTrue(any(error["loc"][0] == field for error in raised.exception.errors()))

    def test_model_cannot_supply_its_own_passed_flag(self):
        report = passing_report()
        report["passed"] = True
        with self.assertRaises(ValidationError) as raised:
            ModelSemanticReview.model_validate(report)
        self.assertTrue(any(error["loc"] == ("passed",) and error["type"] == "extra_forbidden" for error in raised.exception.errors()))

    def test_required_report_sections_cannot_be_omitted(self):
        for field in ("goal", "rules", "blocks", "uncertainties"):
            with self.subTest(field=field):
                report = passing_report()
                del report[field]
                with self.assertRaises(ValidationError) as raised:
                    ModelSemanticReview.model_validate(report)
                self.assertTrue(any(error["loc"] == (field,) and error["type"] == "missing" for error in raised.exception.errors()))

    def test_backend_report_status_cannot_disagree_with_issues_or_missing_model_review(self):
        task = review_task()
        candidate = review_candidate(task)
        context = review_context()
        valid = validate_semantics(task, candidate, context, reviewer=Mock(return_value=response_for(passing_report())))
        for changes in (
            {"issues": ["仍有未解决问题"]},
            {"model_review": None},
            {"status": "error", "issues": []},
            {"status": "needs_revision", "issues": []},
            {"status": "needs_clarification", "issues": []},
        ):
            with self.subTest(changes=changes):
                payload = valid.model_dump(mode="json")
                payload.update(changes)
                with self.assertRaises(ValidationError):
                    SemanticValidationResult.model_validate(payload)


class SemanticReviewExecutionTests(unittest.TestCase):
    def setUp(self):
        self.task = review_task()
        self.candidate = review_candidate(self.task)
        self.context = review_context()

    def run_report(self, report=None):
        reviewer = Mock(return_value=response_for(passing_report() if report is None else report))
        result = validate_semantics(self.task, self.candidate, self.context, reviewer=reviewer)
        return result, reviewer

    def test_valid_report_passes_and_is_bound_to_current_inputs(self):
        result, reviewer = self.run_report()
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.issues, [])
        self.assertEqual(result.candidate_id, self.candidate.candidate_id)
        self.assertEqual(result.input_hash, semantic_input_hash(self.task, self.candidate, self.context))
        self.assertIsNotNone(result.model_review)
        reviewer.assert_called_once()

    def test_prompt_contains_only_editable_blocks_and_supplied_evidence(self):
        result, reviewer = self.run_report()
        messages = reviewer.call_args.args[0]
        payload = json.loads(messages[1][1])
        self.assertEqual(result.status, "passed")
        self.assertEqual([block["block_id"] for block in payload["blocks"]], [FIRST_BLOCK_ID])
        self.assertEqual(payload["blocks"][0]["original_text"], self.task.draft.blocks[0].text)
        self.assertEqual(payload["blocks"][0]["candidate_text"], self.candidate.blocks[0].text)
        self.assertEqual([item["evidence_id"] for item in payload["evidence"]], ["power-1", "returns-1"])
        self.assertEqual([item["rule_index"] for item in payload["brand_rules"]], [0, 1])
        self.assertIn("JSON", messages[0][1])

    def test_program_failures_never_invoke_model(self):
        candidates = [
            self.candidate.model_copy(update={"base_version": 13}),
            self.candidate.model_copy(update={"draft_id": UUID(UNKNOWN_BLOCK_ID)}),
            self.candidate.model_copy(deep=True),
            self.candidate.model_copy(deep=True),
        ]
        candidates[2].blocks[1].text = "未经允许修改的段落。"
        candidates[3].blocks[0].text = "日常充电更顺手。"
        for candidate in candidates:
            with self.subTest(candidate=candidate.model_dump(mode="json")):
                reviewer = Mock(side_effect=AssertionError("must not call"))
                result = validate_semantics(self.task, candidate, self.context, reviewer=reviewer)
                self.assertEqual(result.status, "needs_revision")
                self.assertTrue(result.issues)
                self.assertIsNone(result.model_review)
                reviewer.assert_not_called()

    def test_length_failure_also_stops_before_model_call(self):
        self.task.requirements.length_limits = [{"block_id": FIRST_BLOCK_ID, "unit": "unicode_chars", "maximum": 3}]
        reviewer = Mock()
        result = validate_semantics(self.task, self.candidate, self.context, reviewer=reviewer)
        self.assertEqual(result.status, "needs_revision")
        reviewer.assert_not_called()

    def test_goal_tone_and_brand_rule_failures_need_revision(self):
        for section in ("goal", "tone", "rule"):
            with self.subTest(section=section):
                report = passing_report()
                assessment = report["goal"] if section == "goal" else report["blocks"][0]["tone"] if section == "tone" else report["rules"][1]
                assessment.update(verdict="unmet", note="请删去重复或夸张表达")
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "needs_revision")
                self.assertTrue(any("请删去重复或夸张表达" in issue for issue in result.issues))

    def test_unsupported_and_contradicted_facts_need_revision(self):
        for verdict in ("unsupported", "contradicted"):
            with self.subTest(verdict=verdict):
                report = passing_report()
                fact = report["blocks"][0]["facts"][0]
                fact.update(verdict=verdict, note="请删除或改正功率声明")
                if verdict == "unsupported":
                    fact["references"] = []
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "needs_revision")
                self.assertTrue(any("功率声明" in issue for issue in result.issues))

    def test_uncertain_assessments_and_facts_require_clarification(self):
        for section in ("goal", "tone", "rule", "fact", "uncertainties"):
            with self.subTest(section=section):
                report = passing_report()
                if section == "uncertainties":
                    report["uncertainties"] = ["请确认适用的产品型号"]
                else:
                    assessment = report["goal"] if section == "goal" else report["blocks"][0]["tone"] if section == "tone" else report["rules"][0] if section == "rule" else report["blocks"][0]["facts"][0]
                    assessment.update(verdict="uncertain", note="请确认适用的产品型号")
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "needs_clarification")
                self.assertTrue(any("产品型号" in issue for issue in result.issues))

    def test_clarification_has_priority_and_retains_known_revision_issues(self):
        report = passing_report()
        report["goal"].update(verdict="unmet", note="请缩短表达")
        report["uncertainties"] = ["请补充品牌语气要求"]
        result, _ = self.run_report(report)
        self.assertEqual(result.status, "needs_clarification")
        self.assertTrue(any("缩短表达" in issue for issue in result.issues))
        self.assertTrue(any("品牌语气" in issue for issue in result.issues))

    def test_unknown_or_read_only_blocks_and_duplicate_reviews_are_errors(self):
        for block_id in (SECOND_BLOCK_ID, UNKNOWN_BLOCK_ID, FIRST_BLOCK_ID):
            with self.subTest(block_id=block_id):
                report = passing_report()
                extra = deepcopy(report["blocks"][0])
                extra["block_id"] = block_id
                report["blocks"].append(extra)
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "error")
                self.assertTrue(result.issues)

    def test_multiple_editable_blocks_must_each_have_one_review(self):
        task = review_task(two_editable=True)
        candidate = review_candidate(task)
        full = passing_report(two_editable=True)
        valid = validate_semantics(task, candidate, self.context, reviewer=Mock(return_value=response_for(full)))
        self.assertEqual(valid.status, "passed")
        missing = passing_report()
        invalid = validate_semantics(task, candidate, self.context, reviewer=Mock(return_value=response_for(missing)))
        self.assertEqual(invalid.status, "error")

    def test_rule_coverage_rejects_missing_duplicate_and_unknown_indices(self):
        for indices in ([0], [0, 0], [0, 2], [0, 1, 2]):
            with self.subTest(indices=indices):
                report = passing_report()
                report["rules"] = [{"rule_index": index, "verdict": "met", "note": "测试规则"} for index in indices]
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "error")

    def test_fact_excerpt_must_belong_to_its_candidate_block(self):
        for claim in ("支持 100W 输出", "支持 30 天退货"):
            with self.subTest(claim=claim):
                report = passing_report()
                report["blocks"][0]["facts"][0]["claim"] = claim
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "error")
                self.assertTrue(any(claim in issue for issue in result.issues))

    def test_unknown_evidence_and_nonliteral_quotes_are_errors(self):
        references = [
            {"evidence_id": "fabricated", "quote": "支持 65W 输出"},
            {"evidence_id": "power-1", "quote": "支持双设备同时快充"},
            {"evidence_id": "power-1", "quote": "该充电器输出。"},
        ]
        for reference in references:
            with self.subTest(reference=reference):
                report = passing_report()
                report["blocks"][0]["facts"][0]["references"] = [reference]
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "error")

    def test_supported_and_contradicted_verdicts_require_evidence(self):
        for verdict in ("supported", "contradicted"):
            with self.subTest(verdict=verdict):
                report = passing_report()
                report["blocks"][0]["facts"][0].update(verdict=verdict, references=[])
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "error")

    def test_even_uncertain_or_unsupported_reports_cannot_invent_references(self):
        for verdict in ("uncertain", "unsupported"):
            with self.subTest(verdict=verdict):
                report = passing_report()
                fact = report["blocks"][0]["facts"][0]
                fact["verdict"] = verdict
                fact["references"][0]["evidence_id"] = "invented"
                result, _ = self.run_report(report)
                self.assertEqual(result.status, "error")

    def test_invalid_or_blank_model_content_is_error(self):
        for content in (None, [], {}, "", " \n\t", "not-json", "{}", "```json\n{}\n```", "{\"goal\":"):
            with self.subTest(content=content):
                response = SimpleNamespace(content=content, response_metadata={"finish_reason": "stop"})
                with patch("app.modules.marketing.workflow.revision.validation.logger"):
                    result = validate_semantics(self.task, self.candidate, self.context, reviewer=Mock(return_value=response))
                self.assertEqual(result.status, "error")
                self.assertTrue(result.issues)
                self.assertIsNone(result.model_review)

    def test_nonstop_or_missing_finish_reason_is_error_even_for_valid_json(self):
        for reason in ("length", "max_tokens", "max_output_tokens", "content_filter", "tool_calls", None):
            with self.subTest(reason=reason):
                result = validate_semantics(self.task, self.candidate, self.context, reviewer=Mock(return_value=response_for(passing_report(), finish_reason=reason)))
                self.assertEqual(result.status, "error")
                self.assertIsNone(result.model_review)

    def test_missing_or_malformed_response_metadata_is_error(self):
        responses = [SimpleNamespace(content=json.dumps(passing_report()))]
        for metadata in ({}, None, [], "stop", 1, {"other": "stop"}):
            responses.append(SimpleNamespace(content=json.dumps(passing_report()), response_metadata=metadata))
        for response in responses:
            with self.subTest(response=response):
                result = validate_semantics(self.task, self.candidate, self.context, reviewer=Mock(return_value=response))
                self.assertEqual(result.status, "error")
                self.assertTrue(result.issues)
                self.assertIsNone(result.model_review)

    def test_provider_failure_is_error_and_does_not_expose_exception_text(self):
        reviewer = Mock(side_effect=RuntimeError("private upstream detail"))
        with self.assertLogs("app.modules.marketing.workflow.revision.validation", level="ERROR"):
            result = validate_semantics(self.task, self.candidate, self.context, reviewer=reviewer)
        self.assertEqual(result.status, "error")
        self.assertNotIn("private upstream detail", " ".join(result.issues))
        reviewer.assert_called_once()

    def test_no_rules_or_evidence_is_valid_for_subjective_copy(self):
        task = self.task.model_copy(deep=True)
        task.requirements.must_keep = ["30 天退货"]
        candidate = self.candidate.model_copy(deep=True)
        candidate.blocks[0].text = "让日常充电更顺手。"
        context = RevisionReviewContext(rules_version="no-rules-v1", brand_rules=[], evidence=[])
        report = passing_report()
        report["rules"] = []
        report["blocks"][0]["facts"] = []
        result = validate_semantics(task, candidate, context, reviewer=Mock(return_value=response_for(report)))
        self.assertEqual(result.status, "passed")

    def test_validation_does_not_mutate_caller_inputs(self):
        before = [item.model_dump(mode="json") for item in (self.task, self.candidate, self.context)]
        report = passing_report()
        before_report = deepcopy(report)
        result, _ = self.run_report(report)
        self.assertEqual(result.status, "passed")
        self.assertEqual([item.model_dump(mode="json") for item in (self.task, self.candidate, self.context)], before)
        self.assertEqual(report, before_report)

    def test_review_uses_snapshots_when_callback_mutates_originals(self):
        expected_hash = semantic_input_hash(self.task, self.candidate, self.context)

        def mutate_originals(messages):
            self.task.requirements.goal = "改为完全不同的目标"
            self.candidate.blocks[0].text = "调用方在检查期间改动了候选"
            self.context.evidence[0].quote = "调用方在检查期间改动了证据"
            return response_for(passing_report())

        result = validate_semantics(self.task, self.candidate, self.context, reviewer=mutate_originals)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.input_hash, expected_hash)
        self.assertNotEqual(result.input_hash, semantic_input_hash(self.task, self.candidate, self.context))

    def test_tampered_context_is_revalidated_before_model_call(self):
        self.context.evidence.append(self.context.evidence[0])
        reviewer = Mock()
        with self.assertRaises(ValidationError):
            validate_semantics(self.task, self.candidate, self.context, reviewer=reviewer)
        reviewer.assert_not_called()

    def test_hash_is_stable_across_json_round_trip(self):
        restored = [type(value).model_validate_json(value.model_dump_json()) for value in (self.task, self.candidate, self.context)]
        self.assertEqual(semantic_input_hash(*restored), semantic_input_hash(self.task, self.candidate, self.context))

    def test_hash_changes_for_candidate_requirements_rules_and_evidence_versions(self):
        original = semantic_input_hash(self.task, self.candidate, self.context)
        changed = []
        task = self.task.model_copy(deep=True)
        task.requirements.goal = "突出功率信息"
        changed.append((task, self.candidate, self.context))
        candidate = self.candidate.model_copy(deep=True)
        candidate.blocks[0].text += "了解详情。"
        changed.append((self.task, candidate, self.context))
        candidate = self.candidate.model_copy(update={"candidate_id": UUID(UNKNOWN_BLOCK_ID)})
        changed.append((self.task, candidate, self.context))
        context = self.context.model_copy(deep=True)
        context.rules_version = "brand-v4"
        changed.append((self.task, self.candidate, context))
        context = self.context.model_copy(deep=True)
        context.brand_rules[0] = "改用正式语气"
        changed.append((self.task, self.candidate, context))
        context = self.context.model_copy(deep=True)
        context.evidence[0].document_version += 1
        changed.append((self.task, self.candidate, context))
        context = self.context.model_copy(deep=True)
        context.evidence[0].quote += "功率条件已更新。"
        changed.append((self.task, self.candidate, context))
        for values in changed:
            with self.subTest(values=[value.model_dump(mode="json") for value in values]):
                self.assertNotEqual(semantic_input_hash(*values), original)


if __name__ == "__main__":
    unittest.main()
