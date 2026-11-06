"""Review real copy against separate brand and approved-research evidence."""

from copy import deepcopy
import json
import sys
import types
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.modules.marketing.schemas.content_review import ModelReview
from app.modules.marketing.schemas.copy import COPY_VERSION
from app.modules.marketing.workflow.self_review import review_passed, self_review, validate_review


BRAND = "本产品为65W GaN充电器，配备双USB-C接口。"
EXTERNAL = "该校图书馆提供充电器借用服务。"
URL = "https://university.example.test/library/equipment"
RESEARCH = {
    "status": "completed", "summary": EXTERNAL,
    "source_urls": [URL], "uncertainties": ["仅代表这所学校，不代表全美。"],
}
VALIDATION = {"passed": True, "issues": []}


def copy_content(body=BRAND + "推断：宿舍学习一角可以作为校园内容灵感。"):
    return {
        "version": COPY_VERSION,
        "platforms": {"linkedin": {
            "hook": "给美国大学生的校园灵感",
            "body": body,
            "cta": "了解产品，欢迎品牌主页联系",
            "tags": "#品牌 #校园",
        }},
    }


def brand_fact():
    # Existing reviewers need no new fields and still default to brand evidence.
    return {"claim": BRAND, "verdict": "supported", "evidence": BRAND, "suggestion": ""}


def external_fact(claim=EXTERNAL):
    return {
        "claim": claim, "claim_type": "external_fact", "evidence_scope": "research",
        "source_url": URL, "verdict": "supported", "evidence": claim, "suggestion": "",
    }


def non_factual(claim):
    return {
        "claim": claim, "claim_type": "non_factual", "evidence_scope": "not_required",
        "verdict": "supported", "evidence": "", "suggestion": "",
    }


class ResearchAwareReviewTests(unittest.TestCase):
    def validate(self, facts, content=None, research=RESEARCH, validation=VALIDATION, platforms=None):
        return validate_review(
            ModelReview.model_validate({"facts": facts}), content or copy_content(),
            BRAND, platforms or ["linkedin"], research=research, research_validation=validation,
        )

    def test_approved_external_fact_and_legacy_brand_fact_both_pass(self):
        raw = ModelReview.model_validate({"facts": [brand_fact()]})
        self.assertEqual(raw.facts[0].evidence_scope, "brand")
        self.assertEqual(raw.facts[0].claim_type, "brand_product")
        content = copy_content(BRAND + EXTERNAL + "\n" + URL)
        report = self.validate([brand_fact(), external_fact()], content)
        self.assertEqual(report.status, "passed")
        self.assertTrue(review_passed({"content": content, "self_review": report.model_dump()}))
        changed = deepcopy(content)
        changed["platforms"]["linkedin"]["body"] += "已经获得UL认证。"
        self.assertFalse(review_passed({"content": changed, "self_review": report.model_dump()}))

    def test_external_fact_requires_the_source_url_in_published_copy(self):
        report = self.validate([external_fact()], copy_content(EXTERNAL))
        self.assertEqual(report.status, "needs_revision")
        self.assertIn("facts", report.blockers)
        self.assertTrue(any("完整来源链接" in error for error in report.errors))
        self.assertIn("在对应事实后附完整来源链接", report.facts[0].suggestion)
        self.assertIn(URL, report.facts[0].suggestion)
        linked = self.validate([external_fact()], copy_content(EXTERNAL + "\n" + URL))
        self.assertEqual(linked.status, "passed")

    def test_external_fact_needs_a_source_on_every_platform_containing_it(self):
        content = copy_content(EXTERNAL)
        content["platforms"]["x"] = deepcopy(content["platforms"]["linkedin"])
        content["platforms"]["x"]["body"] += "\n" + URL
        report = self.validate([external_fact()], content, platforms=["linkedin", "x"])
        self.assertEqual(report.status, "needs_revision")
        self.assertTrue(any("完整来源链接" in error for error in report.errors))
        content["platforms"]["linkedin"]["body"] += "\n" + URL
        linked = self.validate([external_fact()], content, platforms=["linkedin", "x"])
        self.assertEqual(linked.status, "passed")

    def test_unapproved_external_research_and_wrong_source_cannot_support_a_fact(self):
        for research, validation, source in (
            (RESEARCH, {"passed": False, "issues": []}, URL),
            ({**RESEARCH, "status": "insufficient"}, VALIDATION, URL),
            (RESEARCH, {"passed": "true", "issues": []}, URL),
            (RESEARCH, VALIDATION, "https://invented.example.test"),
        ):
            with self.subTest(research=research["status"], validation=validation, source=source):
                fact = external_fact()
                fact["source_url"] = source
                report = self.validate([fact], copy_content(EXTERNAL), research, validation)
                self.assertEqual(report.status, "needs_revision")
                self.assertIn("facts", report.blockers)

    def test_rewritten_evidence_returns_an_actionable_revision(self):
        fact = external_fact()
        fact["evidence"] = "图书馆提供充电服务（改写的证据，并非摘要原文）"
        report = self.validate([fact], copy_content(EXTERNAL + "\n" + URL))
        self.assertEqual(report.status, "needs_revision")
        self.assertIn("单一来源", report.facts[0].suggestion)
        self.assertIn("标题可改为", report.facts[0].suggestion)

    def test_product_certification_cannot_use_even_approved_research_evidence(self):
        claim = "本产品已经取得UL认证。"
        fact = external_fact(claim)
        fact["claim_type"] = "brand_product"
        report = self.validate([fact], copy_content(claim), {**RESEARCH, "summary": claim})
        self.assertEqual(report.status, "needs_revision")
        self.assertTrue(any("本产品事实只能依据品牌资料" in error for error in report.errors))

    def test_creative_target_audience_and_generic_cta_do_not_need_brand_quotes(self):
        content = copy_content()
        fields = content["platforms"]["linkedin"]
        facts = [brand_fact(), *[{
            "claim": claim, "claim_type": "non_factual", "evidence_scope": "not_required",
            "verdict": "supported", "evidence": "", "suggestion": "",
        } for claim in (fields["hook"], fields["cta"], "推断：宿舍学习一角可以作为校园内容灵感。")]]
        self.assertEqual(self.validate(facts, content).status, "passed")

    def test_parameters_and_performance_cannot_be_labeled_non_factual(self):
        for claim in ("65W GaN充电器", "已获UL认证", "推断：支持双设备同时快充", "推断：便携自备价值"):
            with self.subTest(claim=claim):
                fact = {
                    "claim": claim, "claim_type": "non_factual", "evidence_scope": "not_required",
                    "verdict": "supported", "evidence": "", "suggestion": "",
                }
                report = self.validate([fact], copy_content(claim))
                self.assertEqual(report.status, "needs_revision")
                self.assertIn("facts", report.blockers)

    def test_future_brand_actions_cannot_be_labeled_non_factual(self):
        for claim in (
            "我们会公布完整参数。",
            "我们将发布更多产品信息。",
            "品牌会推出新产品。",
            "本品牌将会上线产品。",
            "不承诺更轻，但品牌将上线新品。",
        ):
            with self.subTest(claim=claim):
                report = self.validate([non_factual(claim)], copy_content(claim))
                self.assertEqual(report.status, "needs_revision")
                self.assertIn("facts", report.blockers)

    def test_conditional_follow_up_language_does_not_promise_brand_actions(self):
        claim = "如有新资料，欢迎关注后续更新。"
        self.assertEqual(self.validate([non_factual(claim)], copy_content(claim)).status, "passed")

    def test_explicit_negative_performance_clauses_are_not_positive_assertions(self):
        for claim in (
            "不承诺同时为两台设备供电。",
            "我们不保证更小和更轻。",
            "也不声称更凉。",
            "这些只是场景创意，不代表产品实际表现，也不承诺同时供电或轻便优势。",
        ):
            with self.subTest(claim=claim):
                report = self.validate([non_factual(claim)], copy_content(claim))
                self.assertEqual(report.status, "passed")

    def test_negative_prefixes_and_hedges_do_not_hide_affirmative_product_claims(self):
        for claim in (
            "不但已获UL认证，也支持双设备同时快充。",
            "已获UL认证但待验证。",
            "不承诺更轻，但本产品已获UL认证。",
            "不保证更小，65W快充适配全部设备。",
            "不承诺更轻且已获UL认证。",
            "不声称更凉，但支持双设备同时快充。",
        ):
            with self.subTest(claim=claim):
                report = self.validate([non_factual(claim)], copy_content(claim))
                self.assertEqual(report.status, "needs_revision")
                self.assertIn("facts", report.blockers)

    def test_known_interface_is_a_brand_fact_separate_from_the_creative_scene(self):
        body = (
            "在宿舍，你可以尝试把双USB-C接口纳入桌面整理思路；"
            "在图书馆，也可以把它放进随身携带清单。"
            "这些只是场景创意，不代表产品实际表现，也不承诺同时供电或轻便优势。"
        )
        facts = [{**brand_fact(), "claim": "双USB-C接口"}, *[
            non_factual(claim) for claim in (
                "纳入桌面整理思路", "放进随身携带清单", "也不承诺同时供电或轻便优势",
            )
        ]]
        self.assertEqual(self.validate(facts, copy_content(body)).status, "passed")
        # Known parameters are not waived merely because the same long claim
        # also contains creative language and a negative performance clause.
        self.assertEqual(
            self.validate([non_factual(body)], copy_content(body)).status,
            "needs_revision",
        )

    def test_review_prompt_distinguishes_atomic_facts_from_unconfirmed_scope(self):
        scope_statements = (
            "美国大学生在宿舍和图书馆充电，场景判断仍待核实。",
            "美国校园供电与宿舍规定仍待核实，本文不引用其他地区规定作为美国事实。",
        )
        content = copy_content("".join(scope_statements))
        captured = []

        def model(value, **kwargs):
            captured.append(value.to_messages()[0].content)
            return AIMessage(content=json.dumps({
                "facts": [non_factual(claim) for claim in scope_statements],
            }))

        module = types.ModuleType("app.infra.ai.llm")
        module.llm = RunnableLambda(model)
        with patch.dict(sys.modules, {"app.infra.ai.llm": module}):
            result = self_review({
                "content": content, "retrieved_context": BRAND, "social_media": ["linkedin"],
            })
        self.assertEqual(result["self_review"]["status"], "passed")
        self.assertEqual(len(captured), 1)
        for rule in (
            "原子事实", "仍待核实", "不引用", "否定承诺", "已获UL认证但待验证",
            "完整来源链接", "品牌行动", "明确授权",
        ):
            self.assertIn(rule, captured[0])

    def test_real_review_prompt_only_audits_copy_and_does_not_expose_rejected_research(self):
        captured = []
        rejected = {**RESEARCH, "summary": "REJECTED_UL_CLAIM_NOT_TO_EXPOSE"}

        def model(value, **kwargs):
            payload = json.loads(value.to_messages()[-1].content)
            captured.append(payload)
            self.assertEqual(payload["copy"], copy_content())
            self.assertEqual(payload["task_context"]["user_persona"], "美国大学生")
            self.assertEqual(payload["approved_research"]["summary"], "")
            self.assertEqual(payload["approved_research"]["source_urls"], [])
            self.assertNotIn("STRATEGY_NOT_AUDITED", value.to_string())
            self.assertNotIn("REJECTED_UL_CLAIM_NOT_TO_EXPOSE", value.to_string())
            return AIMessage(content=json.dumps({"facts": [brand_fact()]}))

        module = types.ModuleType("app.infra.ai.llm")
        module.llm = RunnableLambda(model)
        with patch.dict(sys.modules, {"app.infra.ai.llm": module}):
            result = self_review({
                "content": copy_content(), "retrieved_context": BRAND, "social_media": ["linkedin"],
                "goal": "制定五部分营销策略", "user_persona": "美国大学生",
                "strategy": "STRATEGY_NOT_AUDITED", "research": rejected,
                "research_validation": {"passed": False, "issues": ["未通过核对"]},
            })
        self.assertEqual(len(captured), 1)
        self.assertEqual(result["self_review"]["status"], "passed")
        self.assertEqual(result["self_review_attempts"], 0)

    def test_valid_json_from_a_truncated_review_cannot_approve_copy(self):
        for reason in ("length", "max_tokens", "max_output_tokens"):
            with self.subTest(reason=reason):
                module = types.ModuleType("app.infra.ai.llm")
                module.llm = RunnableLambda(lambda value, **kwargs: AIMessage(
                    content='{"facts": []}', response_metadata={"finish_reason": reason},
                ))
                with patch.dict(sys.modules, {"app.infra.ai.llm": module}):
                    result = self_review({
                        "content": copy_content("本产品已经获得UL认证。"),
                        "retrieved_context": BRAND, "social_media": ["linkedin"],
                    })
                self.assertEqual(result["self_review"]["status"], "error")
                self.assertFalse(review_passed(result))


if __name__ == "__main__":
    unittest.main()
