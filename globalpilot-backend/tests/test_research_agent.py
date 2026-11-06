"""Offline research-agent checks with real LangChain/LangGraph execution.

The model and search tool below are deterministic; tests must never import the
application's settings, real model, or network search implementation.
"""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
import sys
import types
from typing import Any
import unittest
from unittest.mock import patch

import httpx
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from pydantic import Field


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """Keep scripted tool calls while exercising create_agent's actual loop."""

    call_count: int = 0
    bound_tool_names: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any):
        self.bound_tool_names.append([
            item.get("function", {}).get("name", item.get("name", ""))
            if isinstance(item, dict) else getattr(item, "name", "")
            for item in tools
        ])
        return self

    def _generate(self, *args: Any, **kwargs: Any):
        self.call_count += 1
        return super()._generate(*args, **kwargs)


class OfflineSearchError(RuntimeError):
    """Match the search dependency's public exception without importing it."""


def tool_message(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": call_id}],
    )


def search_message(query: str, call_id: str = "search-1") -> AIMessage:
    return tool_message("search_web", {"query": query}, call_id)


def report_message(report: dict[str, Any]) -> AIMessage:
    return tool_message("ResearchReport", report, "report-1")


def fake_search_tool(
    results: list[dict[str, Any]] | None = None,
    *,
    error: Exception | None = None,
) -> tuple[BaseTool, list[str]]:
    """Return a real LangChain tool plus its observable search-query log."""
    queries: list[str] = []

    @tool
    def search_web(query: str) -> dict:
        """Search deterministic public-source fixtures without network access."""
        queries.append(query)
        if error is not None:
            raise error
        return {"query": query, "results": deepcopy(results or [])}

    return search_web, queries


BRAND_FACTS = "教学示例产品是65W GaN充电器，双USB-C接口"
TASK = f"品牌资料：{BRAND_FACTS}。\n任务：仅整理给定资料里的产品事实，不联网。"
SOURCE_URL = "https://university.example.test/electrical-safety"
SOURCE_QUOTE = "宿舍内不得串联插线板。"
SOURCE = {
    "title": "宿舍用电规定",
    "url": SOURCE_URL,
    "content": SOURCE_QUOTE,
}


def report(
    status: str = "not_needed",
    summary: str = "产品采用GaN技术，功率为65W，配备双USB-C接口。",
    source_urls: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "source_urls": source_urls or [],
        "uncertainties": [],
    }


def task_reviewer(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_answered": True,
        "checks": [{
            "index": sentence["index"],
            "verdict": "supported",
            "source": "task",
            "source_url": None,
            "quote": BRAND_FACTS,
            "reason": "只复述用户提供的产品事实。",
        } for sentence in payload["sentences"]],
        "issues": [],
    }


def web_reviewer(payload: dict[str, Any], *, task_answered: bool = True) -> dict[str, Any]:
    return {
        "task_answered": task_answered,
        "checks": [{
            "index": sentence["index"],
            "verdict": "supported",
            "source": "web",
            "source_url": SOURCE_URL,
            "quote": SOURCE_QUOTE,
            "reason": "测试网页的原文支持该项规定。",
        } for sentence in payload["sentences"]],
        "issues": [] if task_answered else ["单校来源不能回答全部地区的问题。"],
    }


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


class ResearchAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only dependency boundaries are replaced: create_agent and its graph
        # middleware are the actual installed implementations.
        default_search, _ = fake_search_tool()
        default_model = ToolCallingFakeModel(responses=[report_message(report())])
        cls.modules = patch.dict(sys.modules, {
            "app.infra.ai.llm": fake_module("app.infra.ai.llm", llm=default_model),
            "app.modules.marketing.workflow.research_tools": fake_module(
                "app.modules.marketing.workflow.research_tools", search_web=default_search,
                ResearchSearchError=OfflineSearchError,
            ),
        })
        cls.modules.start()
        try:
            sys.modules.pop("app.modules.marketing.workflow.research_agent", None)
            cls.agent_module = importlib.import_module("app.modules.marketing.workflow.research_agent")
            errors = importlib.import_module("app.modules.marketing.workflow.research_validation")
            cls.MissingError = errors.ResearchReportMissingError
            cls.ValidationError = errors.ResearchReportValidationError
        except BaseException:
            cls.modules.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.modules.stop()

    def agent(self, messages, *, results=None, error=None, reviewer=task_reviewer, **limits):
        model = ToolCallingFakeModel(responses=messages)
        search, queries = fake_search_tool(results, error=error)
        graph = self.agent_module.build_research_agent(
            model=model, search_tool=search, reviewer=reviewer, **limits,
        )
        return graph, model, queries

    @staticmethod
    def invoke(graph, task=TASK):
        return graph.invoke({"messages": [{"role": "user", "content": task}]})

    def test_given_product_facts_need_no_search(self):
        graph, model, queries = self.agent([report_message(report())])
        result = self.invoke(graph)
        self.assertEqual(result["structured_response"].status, "not_needed")
        self.assertEqual(queries, [])
        self.assertEqual(model.call_count, 1)
        self.assertIn("research_validation", result)
        self.assertEqual(result["structured_response"].source_urls, [])

    def test_research_review_has_its_own_scope_when_marketing_goal_requests_strategy(self):
        audits = []

        def research_only_reviewer(payload):
            audits.append(payload)
            self.assertIn("制定五部分营销策略", payload["task"])
            self.assertEqual(payload["scope"]["node"], "research_agent")
            self.assertIn("五部分策略", payload["scope"]["excluded_deliverables"])
            return task_reviewer(payload)

        graph, model, queries = self.agent(
            [report_message(report())], reviewer=research_only_reviewer,
        )
        result = self.invoke(
            graph, f"品牌资料：{BRAND_FACTS}。营销目标：制定五部分营销策略。调研要求：整理现有产品事实，不联网。",
        )
        self.assertEqual(result["structured_response"].status, "not_needed")
        self.assertTrue(result["research_validation"]["passed"])
        self.assertEqual(len(audits), 1)
        self.assertEqual(queries, [])
        self.assertEqual(model.call_count, 1)

    def test_one_search_can_submit_a_supported_report(self):
        graph, model, queries = self.agent([
            search_message("该校 插线板 规定"),
            report_message(report("completed", "该校禁止串联插线板。", [SOURCE_URL])),
        ], results=[SOURCE], reviewer=web_reviewer)
        result = self.invoke(graph, "查明该校是否允许串联插线板，给出来源。")
        self.assertEqual(result["structured_response"].status, "completed")
        self.assertEqual(result["structured_response"].source_urls, [SOURCE_URL])
        self.assertEqual(queries, ["该校 插线板 规定"])
        self.assertEqual(model.call_count, 2)

    def test_three_sequential_searches_leave_a_model_turn_for_report(self):
        graph, model, queries = self.agent([
            search_message("美国 宿舍 用电规定", "search-1"),
            search_message("美国 图书馆 用电规定", "search-2"),
            search_message("美国 校园 插线板 政策", "search-3"),
            report_message(report("insufficient", "现有来源仅能确认该校禁止串联插线板。", [SOURCE_URL])),
        ], results=[SOURCE], reviewer=lambda value: web_reviewer(value, task_answered=False))
        result = self.invoke(graph, "调查美国高校宿舍和图书馆的用电规定。")
        self.assertEqual(result["structured_response"].status, "insufficient")
        self.assertEqual(len(queries), 3)
        self.assertEqual(model.call_count, 4)
        self.assertNotIn("search_web", model.bound_tool_names[-1])

    def test_parallel_search_requests_execute_only_the_first(self):
        parallel = AIMessage(content="", tool_calls=[
            {"name": "search_web", "args": {"query": "first"}, "id": "search-1"},
            {"name": "search_web", "args": {"query": "second"}, "id": "search-2"},
        ])
        graph, _, queries = self.agent([
            parallel,
            report_message(report("completed", "该校禁止串联插线板。", [SOURCE_URL])),
        ], results=[SOURCE], reviewer=web_reviewer)
        result = self.invoke(graph, "查询该校串联插线板规定。")
        self.assertEqual(queries, ["first"])
        self.assertEqual(result["structured_response"].status, "completed")

    def test_search_and_report_in_same_turn_waits_for_search_results(self):
        early_report = report("completed", "旧报告尚未读取搜索结果，不应接受。")
        mixed = AIMessage(content="", tool_calls=[
            {"name": "search_web", "args": {"query": "first"}, "id": "search-1"},
            {"name": "search_web", "args": {"query": "second"}, "id": "search-2"},
            {"name": "ResearchReport", "args": early_report, "id": "early-report"},
        ])
        fresh_report = report("completed", "该校禁止串联插线板。", [SOURCE_URL])
        graph, model, queries = self.agent([
            mixed, report_message(fresh_report),
        ], results=[SOURCE], reviewer=web_reviewer)
        result = self.invoke(graph, "查询该校串联插线板规定，并依据搜索结果交报告。")
        self.assertEqual(queries, ["first"])
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result["structured_response"].model_dump(), fresh_report)
        self.assertFalse(any(
            call["id"] == "early-report"
            for message in result["messages"] if isinstance(message, AIMessage)
            for call in message.tool_calls
        ))
        self.assertFalse(any(
            isinstance(message, ToolMessage) and message.tool_call_id == "early-report"
            for message in result["messages"]
        ))

    def test_missing_report_gets_one_recovery_turn_without_search(self):
        graph, model, queries = self.agent([
            AIMessage(content="普通文本不能替代结构化报告。"),
            report_message(report()),
        ])
        result = self.invoke(graph)
        self.assertEqual(result["structured_response"].status, "not_needed")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(queries, [])
        self.assertNotIn("search_web", model.bound_tool_names[-1])

    def test_repeated_missing_report_fails_explicitly(self):
        graph, model, queries = self.agent([
            AIMessage(content="第一次没有提交报告。"),
            AIMessage(content="第二次仍然没有提交报告。"),
        ])
        with self.assertRaises(self.MissingError):
            self.invoke(graph)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(queries, [])

    def test_valid_but_truncated_report_cannot_pass_invoke_or_stream(self):
        for mode in ("invoke", "stream"):
            for reason in ("length", "max_tokens", "max_output_tokens"):
                with self.subTest(mode=mode, reason=reason):
                    audits = []

                    def reviewer(payload):
                        audits.append(payload)
                        return task_reviewer(payload)

                    truncated = report_message(report()).model_copy(update={
                        "response_metadata": {"finish_reason": reason},
                    })
                    graph, model, queries = self.agent([truncated], reviewer=reviewer)
                    with self.assertRaisesRegex(self.ValidationError, "截断"):
                        if mode == "invoke":
                            self.invoke(graph)
                        else:
                            list(graph.stream({"messages": [{"role": "user", "content": TASK}]}))
                    self.assertEqual(model.call_count, 1)
                    self.assertEqual(audits, [])
                    self.assertEqual(queries, [])

    def test_truncated_search_tool_call_is_not_executed(self):
        truncated = search_message("不应执行的搜索").model_copy(update={
            "response_metadata": {"finish_reason": "length"},
        })
        graph, model, queries = self.agent([truncated, report_message(report())])
        with self.assertRaisesRegex(self.ValidationError, "截断"):
            self.invoke(graph)
        self.assertEqual(queries, [])
        self.assertEqual(model.call_count, 1)

    def test_valid_json_audit_with_length_finish_reason_is_rejected(self):
        audit = {"task_answered": True, "checks": [], "issues": []}
        for reason in ("length", "max_tokens", "max_output_tokens"):
            with self.subTest(reason=reason):
                model = ToolCallingFakeModel(responses=[AIMessage(
                    content="",
                    tool_calls=[{"name": "ResearchAudit", "args": audit, "id": "audit-1"}],
                    response_metadata={"finish_reason": reason},
                )])
                with patch.object(sys.modules["app.infra.ai.llm"], "llm", model):
                    with self.assertRaisesRegex(self.ValidationError, "截断"):
                        self.agent_module._review_with_shared_model({"sentences": []})

    def test_missing_report_at_model_limit_fails_without_extra_model_call(self):
        graph, model, _ = self.agent([AIMessage(content="没有提交报告。")], model_limit=1)
        with self.assertRaises(self.MissingError):
            self.invoke(graph)
        self.assertEqual(model.call_count, 1)

    def test_url_not_returned_by_search_is_rejected(self):
        graph, _, queries = self.agent([
            search_message("该校规定"),
            report_message(report("completed", "该校禁止串联插线板。", ["https://forged.example.test/policy"])),
        ], results=[SOURCE], reviewer=web_reviewer)
        with self.assertRaises(self.ValidationError):
            self.invoke(graph, "查明该校用电规定并引用来源。")
        self.assertEqual(queries, ["该校规定"])

    def test_stream_also_rejects_a_forged_source(self):
        graph, _, _ = self.agent([
            search_message("该校规定"),
            report_message(report("completed", "该校禁止串联插线板。", ["https://forged.example.test/policy"])),
        ], results=[SOURCE], reviewer=web_reviewer)
        with self.assertRaises(self.ValidationError):
            list(graph.stream({"messages": [{"role": "user", "content": "查明该校用电规定。"}]}))

    def test_fabricated_evidence_cannot_keep_a_completed_claim(self):
        def reviewer(payload):
            checks = web_reviewer(payload)
            for item, sentence in zip(checks["checks"], payload["sentences"]):
                if "认证" in sentence["text"]:
                    item["quote"] = "该充电器已获得安全认证。"
            return checks

        graph, _, _ = self.agent([
            search_message("该校规定"),
            report_message(report("completed", "该校禁止串联插线板。该充电器已获得安全认证。", [SOURCE_URL])),
        ], results=[SOURCE], reviewer=reviewer)
        result = self.invoke(graph, "查明该校用电规定并引用来源。")
        validated = result["structured_response"]
        self.assertEqual(validated.status, "insufficient")
        self.assertNotIn("已获得安全认证", validated.summary)
        self.assertIn("禁止串联插线板", validated.summary)
        self.assertTrue(validated.uncertainties)

    def test_unsupported_product_advantage_is_removed(self):
        def reviewer(payload):
            checks = task_reviewer(payload)
            for item, sentence in zip(checks["checks"], payload["sentences"]):
                if "发热" in sentence["text"]:
                    item.update(verdict="unsupported", quote="", reason="资料没有提供产品发热性能。")
            return checks

        graph, _, queries = self.agent([report_message(report(
            summary="产品是65W GaN充电器，配备双USB-C接口。该产品发热优于传统充电器。",
        ))], reviewer=reviewer)
        validated = self.invoke(graph)["structured_response"]
        self.assertEqual(validated.status, "insufficient")
        self.assertNotIn("发热优于", validated.summary)
        self.assertIn("65W", validated.summary)
        self.assertTrue(validated.uncertainties)
        self.assertEqual(queries, [])

    def test_explicit_rejection_overrides_supported_or_inference_verdict(self):
        for claim, verdict, source in (
            ("推断：该产品具有便携自备价值", "inference", "inference"),
            ("推断：该产品可以同时为两台USB-C设备供电", "supported", "task"),
        ):
            with self.subTest(claim=claim):
                def reviewer(payload, verdict=verdict, source=source):
                    audit = task_reviewer(payload)
                    audit["checks"][1].update(
                        verdict=verdict,
                        source=source,
                        reason="品牌资料不支持该产品优势或同时输出能力，明确拒绝此句。",
                    )
                    audit["rejected_indices"] = [1]
                    return audit

                graph, _, _ = self.agent([report_message(report(
                    summary=f"{BRAND_FACTS}。{claim}。",
                ))], reviewer=reviewer)
                result = self.invoke(graph)
                validated = result["structured_response"]
                self.assertEqual(validated.status, "insufficient")
                self.assertIn("65W", validated.summary)
                self.assertNotIn(claim, validated.summary)
                self.assertEqual(result["research_validation"]["rejected_indices"], [1])
                self.assertFalse(result["research_validation"]["passed"])
                self.assertFalse(result["research_validation"]["globally_blocked"])

    def test_unlocated_global_issue_cannot_leave_old_web_claims_in_summary(self):
        def reviewer(payload):
            audit = web_reviewer(payload)
            audit["issues"] = ["本报告存在未能定位到具体句段的适用对象错误。"]
            return audit

        graph, _, _ = self.agent([
            search_message("该校规定"),
            report_message(report("completed", "该校禁止串联插线板。", [SOURCE_URL])),
        ], results=[SOURCE], reviewer=reviewer)
        result = self.invoke(graph, "查明该校用电规定。")
        validated = result["structured_response"]
        self.assertEqual(validated.summary, "现有资料不足以形成可核对的调研结论。")
        self.assertEqual(validated.source_urls, [])
        self.assertEqual(validated.status, "insufficient")
        self.assertFalse(result["research_validation"]["passed"])
        self.assertTrue(result["research_validation"]["globally_blocked"])

    def test_legacy_global_product_error_cannot_leave_an_inference_claim(self):
        def reviewer(payload):
            audit = task_reviewer(payload)
            audit["checks"][1].update(verdict="inference", source="inference")
            # Legacy audits may point out the unsupported claim only globally.
            audit["issues"] = ["第二句便携自备价值没有尺寸或重量资料支持。"]
            return audit

        graph, _, _ = self.agent([report_message(report(
            summary=f"{BRAND_FACTS}。推断：该产品具有便携自备价值。",
        ))], reviewer=reviewer)
        result = self.invoke(graph)
        self.assertNotIn("便携自备价值", result["structured_response"].summary)
        self.assertEqual(result["structured_response"].status, "insufficient")
        self.assertTrue(result["research_validation"]["globally_blocked"])

    def test_declared_limitations_do_not_reject_an_answered_single_school_question(self):
        limitation = "来源仅覆盖一所学校，不能代表全美；页面当前状态仍需学校确认。"

        def reviewer(payload):
            audit = web_reviewer(payload)
            audit["limitations"] = [limitation]
            return audit

        submitted = report("completed", "该校禁止串联插线板。", [SOURCE_URL])
        submitted["uncertainties"] = [limitation]
        graph, _, _ = self.agent([
            search_message("该校规定"), report_message(submitted),
        ], results=[SOURCE], reviewer=reviewer)
        result = self.invoke(graph, "只需查明这一所学校的官方规定，不要求代表全美。")
        validated = result["structured_response"]
        self.assertEqual(validated.status, "completed")
        self.assertEqual(validated.source_urls, [SOURCE_URL])
        self.assertEqual(validated.uncertainties, [limitation])
        self.assertTrue(result["research_validation"]["passed"])
        self.assertEqual(result["research_validation"]["issues"], [])

    def test_receipt_date_is_preserved_and_only_supports_its_metadata_description(self):
        date = "2022-03-05"

        def reviewer(payload):
            self.assertEqual(payload["sources"][0]["date"], date)
            audit = web_reviewer(payload)
            audit["checks"][1].update(source="web_metadata", quote=date)
            return audit

        submitted = report(
            "completed", f"该校禁止串联插线板。检索结果date字段标注日期为{date}。", [SOURCE_URL],
        )
        graph, _, _ = self.agent([
            search_message("该校规定"), report_message(submitted),
        ], results=[{**SOURCE, "date": date}], reviewer=reviewer)
        result = self.invoke(graph, "查明该校官方规定，并如实记录检索date元数据。")
        self.assertEqual(result["structured_response"].status, "completed")
        self.assertIn(date, result["structured_response"].summary)
        self.assertTrue(result["research_validation"]["passed"])

    def test_forged_or_partial_date_cannot_use_web_metadata_evidence(self):
        for quote in ("2025-01-01", "2022"):
            with self.subTest(quote=quote):
                def reviewer(payload, quote=quote):
                    audit = web_reviewer(payload)
                    audit["checks"][1].update(source="web_metadata", quote=quote)
                    return audit

                graph, _, _ = self.agent([
                    search_message("该校规定"),
                    report_message(report(
                        "completed", f"该校禁止串联插线板。检索结果date字段标注日期为{quote}。", [SOURCE_URL],
                    )),
                ], results=[{**SOURCE, "date": "2022-03-05"}], reviewer=reviewer)
                result = self.invoke(graph, "查明该校官方规定。")
                self.assertEqual(result["structured_response"].summary, "该校禁止串联插线板。")
                self.assertEqual(result["structured_response"].status, "insufficient")
                self.assertFalse(result["research_validation"]["passed"])

    def test_metadata_date_cannot_prove_current_policy_or_page_publication(self):
        for claim in (
            "检索结果date字段标注日期为2022-03-05，该校当前仍禁止串联插线板",
            "该网页发布日期为2022-03-05",
        ):
            with self.subTest(claim=claim):
                def reviewer(payload):
                    audit = web_reviewer(payload)
                    audit["checks"][1].update(source="web_metadata", quote="2022-03-05")
                    return audit

                graph, _, _ = self.agent([
                    search_message("该校规定"),
                    report_message(report("completed", f"该校禁止串联插线板。{claim}。", [SOURCE_URL])),
                ], results=[{**SOURCE, "date": "2022-03-05"}], reviewer=reviewer)
                result = self.invoke(graph, "查明该校官方规定。")
                self.assertEqual(result["structured_response"].summary, "该校禁止串联插线板。")
                self.assertFalse(result["research_validation"]["passed"])

    def test_web_body_evidence_does_not_match_the_separate_date_field(self):
        def reviewer(payload):
            audit = web_reviewer(payload)
            audit["checks"][1].update(source="web", quote="2022-03-05")
            return audit

        graph, _, _ = self.agent([
            search_message("该校规定"),
            report_message(report(
                "completed", "该校禁止串联插线板。检索结果date字段标注日期为2022-03-05。", [SOURCE_URL],
            )),
        ], results=[{**SOURCE, "date": "2022-03-05"}], reviewer=reviewer)
        result = self.invoke(graph, "查明该校官方规定。")
        self.assertEqual(result["structured_response"].summary, "该校禁止串联插线板。")
        self.assertFalse(result["research_validation"]["passed"])

    def test_reviewed_insufficient_report_never_claims_passed(self):
        graph, _, _ = self.agent([report_message(report("insufficient"))])
        result = self.invoke(graph)
        validation = result["research_validation"]
        self.assertTrue(validation["review_completed"])
        self.assertTrue(validation["task_answered"])
        self.assertEqual(validation["report_status"], "insufficient")
        self.assertFalse(validation["passed"])

    def test_rejected_indices_must_identify_real_unique_sentences(self):
        for indices in ([4], [-1], [0, 0]):
            with self.subTest(indices=indices):
                def reviewer(payload, indices=indices):
                    audit = task_reviewer(payload)
                    audit["rejected_indices"] = indices
                    return audit

                graph, _, _ = self.agent([report_message(report())], reviewer=reviewer)
                with self.assertRaises(self.ValidationError):
                    self.invoke(graph)

    def test_search_network_failure_cannot_be_reported_as_completed(self):
        def reviewer(payload):
            return {
                "task_answered": False,
                "checks": [{
                    "index": sentence["index"], "verdict": "supported",
                    "source": "process", "source_url": None,
                    "quote": payload["process"]["description"], "reason": "搜索失败，无法核实。",
                } for sentence in payload["sentences"]],
                "issues": ["搜索失败，未取得外部依据。"],
            }

        graph, model, queries = self.agent([
            search_message("该校规定"),
            report_message(report("completed", "搜索失败，尚未取得网页依据。")),
        ], error=httpx.ConnectError("offline network fixture"), reviewer=reviewer)
        result = self.invoke(graph, "查询该校当前的用电规定。")
        self.assertEqual(result["structured_response"].status, "insufficient")
        self.assertTrue(result["structured_response"].uncertainties)
        self.assertEqual(queries, ["该校规定"])
        self.assertEqual(model.call_count, 2)

    def test_missing_or_duplicate_audit_indices_fail_closed(self):
        for failure in ("missing", "duplicate"):
            with self.subTest(failure=failure):
                def reviewer(payload, failure=failure):
                    audit = task_reviewer(payload)
                    if failure == "missing":
                        audit["checks"] = audit["checks"][:-1]
                    else:
                        audit["checks"] = [audit["checks"][0], audit["checks"][0]]
                    return audit

                graph, model, queries = self.agent([report_message(report(
                    summary="产品功率为65W。产品配备双USB-C接口。",
                ))], reviewer=reviewer)
                with self.assertRaises(self.ValidationError):
                    self.invoke(graph)
                self.assertEqual(model.call_count, 1)
                self.assertEqual(queries, [])

    def test_reviewer_exception_fails_invoke_and_stream(self):
        for mode in ("invoke", "stream"):
            with self.subTest(mode=mode):
                audit_calls = []

                def broken_reviewer(payload):
                    audit_calls.append(payload)
                    raise RuntimeError("offline audit failure")

                graph, model, queries = self.agent(
                    [report_message(report())], reviewer=broken_reviewer,
                )
                chunks = []
                with self.assertRaises(self.ValidationError):
                    if mode == "invoke":
                        self.invoke(graph)
                    else:
                        for chunk in graph.stream({"messages": [{"role": "user", "content": TASK}]}):
                            chunks.append(chunk)
                self.assertEqual(len(audit_calls), 1)
                self.assertEqual(model.call_count, 1)
                self.assertEqual(queries, [])
                self.assertFalse(any(
                    isinstance(update, dict) and update.get("research_validation", {}).get("passed")
                    for chunk in chunks for update in chunk.values()
                ))


if __name__ == "__main__":
    unittest.main()
