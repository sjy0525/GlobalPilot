import json
import unittest
from datetime import datetime, timezone

from app.modules.evaluation.engine.cases import CASES, validate_catalog
from app.modules.evaluation.engine.judge import parse_judge_output
from app.modules.evaluation.engine.metrics import TaskMetric, summarize
from app.modules.evaluation.engine.pricing import chat_cost_usd, is_peak
from app.modules.evaluation.engine.report import render_report
from app.modules.evaluation.engine.scoring import check_format, is_qualified, score_draft
from app.modules.marketing.schemas.copy import COPY_VERSION


def _copy(platform: str, **fields: str) -> dict:
    item = {"hook": "开场已经足够", "body": "正文把卖点写清楚，并且给出可以核对的事实。", "cta": "去官网", "tags": "#brand"}
    item.update(fields)
    return {"version": COPY_VERSION, "platforms": {platform: item}}


def _metric(**kwargs) -> TaskMetric:
    base = dict(
        case_code="M01",
        title="样例",
        qualified=False,
        revision_count=0,
        facts_verdict="pass",
        execution_status="completed",
        empty_output=False,
        timed_out=False,
        duration_ms=1000,
        api_cost_usd=0.1,
        failure_reasons=[],
    )
    base.update(kwargs)
    return TaskMetric(**base)


class CatalogTests(unittest.TestCase):
    def test_thirty_cases_are_complete(self):
        self.assertEqual(validate_catalog(CASES), [])
        self.assertEqual(len(CASES), 30)


class ScoringTests(unittest.TestCase):
    def test_format_passes_within_budget(self):
        verdict, reason = check_format(_copy("instagram"), ["instagram"])
        self.assertEqual(verdict, "pass", reason)

    def test_x_over_budget_fails(self):
        content = _copy("x", hook="h" * 100, body="b" * 100, cta="c" * 50, tags="t" * 40)
        verdict, reason = check_format(content, ["x"])
        self.assertEqual(verdict, "fail")
        self.assertIn("280", reason)

    def test_short_body_fails_word_count(self):
        verdict, reason = check_format(_copy("linkedin", body="太短"), ["linkedin"])
        self.assertEqual(verdict, "fail")
        self.assertIn("body", reason)

    def test_all_four_must_pass(self):
        judge = {
            "facts": {"verdict": "pass", "reason": "有依据"},
            "completeness": {"verdict": "pass", "reason": "卖点都在"},
            "tone": {"verdict": "fail", "reason": "语气过满"},
        }
        scores = score_draft(_copy("instagram"), ["instagram"], judge)
        self.assertFalse(is_qualified(scores))
        self.assertEqual(scores["tone"]["verdict"], "fail")

    def test_pending_is_not_qualified(self):
        judge = {
            "facts": {"verdict": "pending", "reason": "资料不够"},
            "completeness": {"verdict": "pass", "reason": "覆盖了"},
            "tone": {"verdict": "pass", "reason": "语气相符"},
        }
        scores = score_draft(_copy("instagram"), ["instagram"], judge)
        self.assertFalse(is_qualified(scores))

    def test_empty_output_fails_every_dimension(self):
        scores = score_draft({"version": COPY_VERSION, "platforms": {}}, ["instagram"], None, "空输出")
        self.assertTrue(all(item["verdict"] == "fail" for item in scores.values()))
        self.assertFalse(is_qualified(scores))


class MetricTests(unittest.TestCase):
    def test_timeout_and_empty_stay_in_the_denominator(self):
        tasks = [
            _metric(case_code="M01", qualified=True, facts_verdict="pass"),
            _metric(case_code="M02", qualified=False, empty_output=True, facts_verdict="fail", failure_reasons=["空输出"]),
            _metric(case_code="M03", qualified=False, timed_out=True, execution_status="timeout", facts_verdict="fail"),
            _metric(case_code="M04", qualified=True, revision_count=1, facts_verdict="pass"),
        ]
        summary = summarize(tasks)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["first_draft_passed"], 1)
        self.assertEqual(summary["first_draft_pass_rate"], 0.25)
        self.assertEqual(summary["empty_count"], 1)
        self.assertEqual(summary["timeout_count"], 1)
        self.assertEqual(summary["fact_error_count"], 0)
        self.assertEqual(summary["cost_per_qualified_usd"], 0.4)

    def test_fact_errors_count_only_finished_drafts(self):
        tasks = [
            _metric(facts_verdict="fail", api_cost_usd=0.2),
            _metric(case_code="M02", facts_verdict="pending", api_cost_usd=0.1),
        ]
        summary = summarize(tasks)
        self.assertEqual(summary["fact_error_count"], 1)
        self.assertEqual(summary["fact_pending_count"], 1)
        self.assertIsNone(summary["cost_per_qualified_usd"])

    def test_report_keeps_timeout_visible(self):
        summary = summarize([
            _metric(qualified=True),
            _metric(case_code="M02", title="超时样例", timed_out=True, execution_status="timeout", qualified=False),
        ])
        text = render_report(
            {
                "label": "基线",
                "provenance": {
                    "test_set_version": "marketing-cases-v1",
                    "workflow_version": "marketing-workflow-v1",
                    "prompt_version": "marketing-prompt-v1",
                    "prompt_fingerprint": "abc",
                    "model_settings": {
                        "generator": {"model": "deepseek-v4-flash", "temperature": 0},
                        "cost_scope": "deepseek_chat_tokens_for_strategy_and_copy",
                        "price_card": "deepseek-flash-2026-09-10",
                    },
                    "knowledge_version": "kb-0-000",
                    "scoring_rules_version": "eval-score-v1",
                    "scoring_fingerprint": "def",
                },
                "summary": summary,
            },
            [
                {"code": "M01", "title": "通过样例", "qualified": True, "failure_reasons": []},
                {
                    "code": "M02",
                    "title": "超时样例",
                    "qualified": False,
                    "timed_out": True,
                    "execution_status": "timeout",
                    "failure_reasons": ["事实不通过：生成超时"],
                },
            ],
        )
        self.assertIn("marketing-cases-v1", text)
        self.assertIn("marketing-workflow-v1", text)
        self.assertIn("marketing-prompt-v1", text)
        self.assertIn("kb-0-000", text)
        self.assertIn("eval-score-v1", text)
        self.assertIn("超时样例", text)
        self.assertIn("全部测试任务数", text)
        self.assertIn("50.0%", text)


class PricingTests(unittest.TestCase):
    def test_weekday_peak_windows(self):
        monday = datetime(2026, 9, 28, tzinfo=timezone.utc)
        self.assertTrue(is_peak(monday.replace(hour=2)))
        self.assertFalse(is_peak(monday.replace(hour=5)))
        self.assertTrue(is_peak(monday.replace(hour=9)))
        self.assertFalse(is_peak(monday.replace(hour=10)))
        self.assertFalse(is_peak(monday.replace(day=26, hour=2)))

    def test_cache_hit_is_cheaper_than_miss(self):
        at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        miss = chat_cost_usd(1_000_000, 0, 0, at)
        hit = chat_cost_usd(1_000_000, 0, 1_000_000, at)
        self.assertAlmostEqual(miss, 0.15)
        self.assertAlmostEqual(hit, 0.003)


class JudgeParseTests(unittest.TestCase):
    def test_parse_fenced_json(self):
        raw = "```json\n" + json.dumps({
            "facts": {"verdict": "pass", "reason": "续航有原文"},
            "completeness": {"verdict": "fail", "reason": "漏了退货"},
            "tone": {"verdict": "pending", "reason": "语气边界不清"},
        }) + "\n```"
        parsed = parse_judge_output(raw)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["completeness"]["verdict"], "fail")

    def test_reject_unknown_verdict(self):
        self.assertIsNone(parse_judge_output(json.dumps({
            "facts": {"verdict": "maybe", "reason": ""},
            "completeness": {"verdict": "pass", "reason": ""},
            "tone": {"verdict": "pass", "reason": ""},
        })))


if __name__ == "__main__":
    unittest.main()
