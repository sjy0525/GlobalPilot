"""Audit-output and public-error checks without model, search, DB, or ports.

Exercise the shared reviewer entry point with real AIMessage/Pydantic objects.
Only the model boundary is scripted; quote/provenance validation stays real.
"""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import types
from typing import Any
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from app.core.http_errors import install_exception_handlers, stream_error_event
from app.modules.marketing.schemas.research_report import ResearchAudit, ResearchReport
from app.modules.marketing.workflow.research_validation import (
    INSUFFICIENT_SUMMARY,
    ResearchReportMissingError,
    ResearchReportValidationError,
    validate_research_report,
)


class RecordingAuditModel(FakeMessagesListChatModel):
    """A deterministic model which records the requested output protocol."""

    tool_bindings: list[dict[str, Any]] = Field(default_factory=list)
    settings_bindings: list[dict[str, Any]] = Field(default_factory=list)
    call_count: int = 0

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self.tool_bindings.append({
            "tools": [convert_to_openai_tool(item) for item in tools],
            "tool_choice": tool_choice,
            **kwargs,
        })
        return self

    def bind(self, **kwargs):
        self.settings_bindings.append(kwargs)
        return self

    def _generate(self, *args, **kwargs):
        self.call_count += 1
        return super()._generate(*args, **kwargs)


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


@tool
def offline_search(query: str) -> dict:
    """Never contact a search service during output validation tests."""
    raise AssertionError("No search may execute in these tests")


TASK = "品牌事实：产品为65W充电器。只整理给定品牌事实。"
AUDIT = {
    "task_answered": True,
    "checks": [{
        "index": 0,
        "verdict": "supported",
        "source": "task",
        "source_url": None,
        "quote": "产品为65W充电器",
        "reason": "任务明确提供该事实。",
    }],
    "rejected_indices": [],
    "issues": [],
    "limitations": [],
}


def audit_message(arguments=None, *, name="ResearchAudit", **kwargs) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": name,
            "args": deepcopy(AUDIT if arguments is None else arguments),
            "id": "audit-1",
        }],
        **kwargs,
    )


class ResearchAuditOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load under an isolated name so existing graph tests keep their modules.
        default_model = RecordingAuditModel(responses=[audit_message()])
        cls.llm_module = fake_module("app.infra.ai.llm", llm=default_model)
        search_module = fake_module(
            "app.modules.marketing.workflow.research_tools",
            search_web=offline_search,
            ResearchSearchError=RuntimeError,
        )
        path = Path(__file__).resolve().parents[1] / "app/modules/marketing/workflow/research_agent.py"
        with patch.dict(sys.modules, {
            "app.infra.ai.llm": cls.llm_module,
            "app.modules.marketing.workflow.research_tools": search_module,
        }):
            spec = importlib.util.spec_from_file_location("_research_audit_output_agent", path)
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)

    def review(self, response):
        model = RecordingAuditModel(responses=[response])
        with patch.dict(sys.modules, {"app.infra.ai.llm": self.llm_module}):
            with patch.object(self.llm_module, "llm", model):
                result = self.module._review_with_shared_model({"sentences": [{"index": 0}]})
        return result, model

    def assert_rejected(self, response, pattern=None):
        model = RecordingAuditModel(responses=[response])
        with patch.dict(sys.modules, {"app.infra.ai.llm": self.llm_module}):
            with patch.object(self.llm_module, "llm", model):
                context = (
                    self.assertRaisesRegex(ResearchReportValidationError, pattern)
                    if pattern else self.assertRaises(ResearchReportValidationError)
                )
                with context:
                    self.module._review_with_shared_model({"sentences": [{"index": 0}]})
        self.assertEqual(model.call_count, 1, "Bad output must not trigger silent repair or extra model calls")

    def test_exactly_one_valid_audit_tool_call_returns_typed_audit(self):
        audit, model = self.review(audit_message())
        self.assertIsInstance(audit, ResearchAudit)
        self.assertEqual(audit.model_dump(), AUDIT)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(model.tool_bindings), 1)
        binding = model.tool_bindings[0]
        self.assertEqual(binding["tool_choice"], "ResearchAudit")
        self.assertIs(binding["parallel_tool_calls"], False)
        self.assertEqual(len(binding["tools"]), 1)
        function = binding["tools"][0]["function"]
        self.assertEqual(function["name"], "ResearchAudit")
        self.assertEqual(function["parameters"]["properties"]["checks"]["type"], "array")
        self.assertEqual(model.settings_bindings, [{"max_tokens": 3072, "timeout": 30.0}])

    def test_plain_json_and_empty_content_do_not_count_as_audit_confirmation(self):
        for content in (json.dumps(AUDIT), "", "```json\n" + json.dumps(AUDIT) + "\n```"):
            with self.subTest(content=content[:20]):
                self.assert_rejected(AIMessage(content=content))

    def test_original_invalid_index_mapping_json_is_rejected(self):
        # Reproduce a common generation failure: index keys inside an array,
        # rather than each item carrying its own index property.
        malformed = '{"task_answered":true,"checks":["0":{"verdict":"supported"}],"issues":[]}'
        response = AIMessage(content="", invalid_tool_calls=[{
            "name": "ResearchAudit", "args": malformed, "id": "audit-invalid",
            "error": "Could not parse tool arguments as JSON",
        }])
        self.assert_rejected(response)

    def test_index_mapping_cannot_replace_the_checks_list(self):
        payload = deepcopy(AUDIT)
        payload["checks"] = {"0": payload["checks"][0]}
        self.assert_rejected(audit_message(payload))

    def test_schema_errors_are_not_treated_as_success(self):
        cases = [
            {"checks": []},
            {"task_answered": True},
            {"task_answered": True, "checks": None},
            {"task_answered": True, "checks": [{"index": 0, "verdict": "approved", "source": "task"}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_rejected(audit_message(payload))

    def test_wrong_or_multiple_tool_calls_are_rejected(self):
        self.assert_rejected(audit_message(name="ResearchReport"))
        valid_call = audit_message().tool_calls[0]
        for other_name in ("ResearchAudit", "search_web"):
            with self.subTest(other_name=other_name):
                self.assert_rejected(AIMessage(content="", tool_calls=[
                    valid_call,
                    {"name": other_name, "args": deepcopy(AUDIT), "id": "other-call"},
                ]))

    def test_invalid_tool_call_cannot_be_ignored_beside_a_valid_audit(self):
        response = audit_message(invalid_tool_calls=[{
            "name": "ResearchAudit", "args": "{", "id": "invalid-call", "error": "invalid JSON",
        }])
        self.assert_rejected(response)

    def test_truncation_rejects_even_an_otherwise_valid_tool_call(self):
        for field in ("finish_reason", "stop_reason"):
            for reason in ("length", "max_tokens", "max_output_tokens"):
                with self.subTest(field=field, reason=reason):
                    self.assert_rejected(
                        audit_message(response_metadata={field: reason}), pattern="截断",
                    )

    def test_typed_audit_still_requires_actual_task_quote(self):
        payload = deepcopy(AUDIT)
        payload["checks"][0]["quote"] = "产品已获得所有安全认证"
        model = RecordingAuditModel(responses=[audit_message(payload)])
        report = ResearchReport(status="not_needed", summary="产品为65W充电器。")
        with patch.dict(sys.modules, {"app.infra.ai.llm": self.llm_module}):
            with patch.object(self.llm_module, "llm", model):
                result, validation = validate_research_report(
                    {"messages": [HumanMessage(content=TASK)]},
                    report,
                    self.module._review_with_shared_model,
                )
        self.assertEqual(result.status, "insufficient")
        self.assertEqual(result.summary, INSUFFICIENT_SUMMARY)
        self.assertFalse(validation["passed"])
        self.assertEqual(validation["rejected_indices"], [0])

    def test_typed_audit_still_requires_each_sentence_exactly_once(self):
        for checks in ([], [AUDIT["checks"][0], AUDIT["checks"][0]]):
            with self.subTest(checks=len(checks)):
                payload = {**deepcopy(AUDIT), "checks": deepcopy(checks)}
                model = RecordingAuditModel(responses=[audit_message(payload)])
                with patch.dict(sys.modules, {"app.infra.ai.llm": self.llm_module}):
                    with patch.object(self.llm_module, "llm", model):
                        with self.assertRaisesRegex(ResearchReportValidationError, "唯一且完整"):
                            validate_research_report(
                                {"messages": [HumanMessage(content=TASK)]},
                                ResearchReport(status="not_needed", summary="产品为65W充电器。"),
                                self.module._review_with_shared_model,
                            )


class ResearchAuditPublicErrorTests(unittest.TestCase):
    def test_research_failure_is_readable_safe_502_in_http_and_stream(self):
        for error_type in (ResearchReportValidationError, ResearchReportMissingError):
            with self.subTest(error_type=error_type.__name__):
                private_error = error_type("private model arguments postgres://secret")
                app = FastAPI()
                install_exception_handlers(app)

                @app.get("/research-error")
                def research_error():
                    raise private_error

                with self.assertLogs("app.core.http_errors", level="ERROR"):
                    with TestClient(app) as client:
                        response = client.get("/research-error")
                    event = stream_error_event(private_error)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["code"], "RESEARCH_VALIDATION_FAILED")
                self.assertIn("调研", response.json()["detail"])
                self.assertIn("重试", response.json()["detail"])
                self.assertNotIn("private model", response.text)
                self.assertNotIn("postgres", response.text)
                self.assertEqual(event, {"type": "error", "status": 502, **response.json()})

    def test_twice_wrapped_upstream_errors_retain_safe_provider_status(self):
        request = httpx.Request("POST", "https://private-provider.example.test")
        cases = [
            (httpx.ReadTimeout("private timeout", request=request), 504, "UPSTREAM_TIMEOUT"),
            (httpx.ConnectError("private connection", request=request), 503, "SERVICE_UNAVAILABLE"),
            (httpx.HTTPStatusError(
                "private provider output", request=request,
                response=httpx.Response(400, request=request),
            ), 502, "UPSTREAM_ERROR"),
        ]
        for cause, status, code in cases:
            with self.subTest(cause=type(cause).__name__):
                try:
                    try:
                        try:
                            raise cause
                        except httpx.HTTPError as provider_error:
                            raise ResearchReportValidationError("private audit output") from provider_error
                    except ResearchReportValidationError as reviewer_error:
                        raise ResearchReportValidationError("private outer review") from reviewer_error
                except ResearchReportValidationError as error:
                    with self.assertLogs("app.core.http_errors", level="ERROR"):
                        event = stream_error_event(error)
                self.assertEqual(event["status"], status)
                self.assertEqual(event["code"], code)
                self.assertNotIn("private", event["detail"])

    def test_unrelated_runtime_error_does_not_inherit_a_cause_status(self):
        try:
            try:
                raise httpx.ReadTimeout("private upstream timeout")
            except httpx.ReadTimeout as cause:
                raise RuntimeError("private application bug") from cause
        except RuntimeError as error:
            with self.assertLogs("app.core.http_errors", level="ERROR"):
                event = stream_error_event(error)
        self.assertEqual(event["status"], 500)
        self.assertEqual(event["code"], "INTERNAL_ERROR")
        self.assertNotIn("private", event["detail"])


if __name__ == "__main__":
    unittest.main()
