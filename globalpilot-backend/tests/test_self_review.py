"""Offline tests: real LangGraph + fake LLM and in-memory checkpoints, no DB."""
import importlib
import json
import sys
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.modules.marketing.schemas.content_review import ModelReview
from app.modules.marketing.schemas.copy import COPY_VERSION
from app.modules.marketing.schemas.research_report import ResearchReport
from app.modules.marketing.workflow.copy import parse_copy
from app.modules.marketing.workflow.self_review import (
    content_hash, review_passed, self_review, validate_review,
)

CONTENT = {
    "version": COPY_VERSION,
    "platforms": {
        "linkedin": {
            "hook": "Hook",
            "body": "Our product is blue.",
            "cta": "Try it",
            "tags": "#brand",
        }
    },
}
CONTEXT = "来源：brand.txt\n内容：Our product is blue."
RESEARCH = {
    "status": "not_needed",
    "summary": "The supplied brand material states that our product is blue.",
    "source_urls": [],
    "uncertainties": [],
}
RESEARCH_VALIDATION = {"passed": True, "issues": []}
GENERATED = {
    "platforms": [{
        "platform": "linkedin",
        "hook": "Hook",
        "body": "Our product is blue.",
        "cta": "Try it",
        "tags": "#brand",
    }]
}


def response():
    return {
        "facts": [{"claim": "Our product is blue.", "verdict": "supported",
                   "evidence": "Our product is blue.", "suggestion": ""}],
    }


def fake_module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


class CopyTests(unittest.TestCase):
    def test_accepts_list_or_object_platforms(self):
        listed, listed_errors = parse_copy(GENERATED, ["linkedin"])
        keyed, keyed_errors = parse_copy(CONTENT, ["linkedin"])
        self.assertEqual(listed, CONTENT)
        self.assertEqual(keyed, CONTENT)
        self.assertEqual(listed_errors, [])
        self.assertEqual(keyed_errors, [])

    def test_platform_coverage_and_duplicates_block(self):
        _, missing = parse_copy(CONTENT, ["linkedin", "youtube"])
        self.assertTrue(any("平台" in item for item in missing))
        _, duplicated = parse_copy({"platforms": GENERATED["platforms"] * 2}, ["linkedin"])
        self.assertTrue(any("平台" in item for item in duplicated))

    def test_missing_fields_and_x_budget_block(self):
        raw = {"platforms": [{"platform": "linkedin", "hook": "", "body": "Our product is blue.",
                              "cta": "Try it", "tags": "#brand"}]}
        _, errors = parse_copy(raw, ["linkedin"])
        self.assertTrue(any("缺少必要字段" in item for item in errors))
        raw = {"platforms": [{"platform": "x", "hook": "H", "body": "a" * 281, "cta": "C", "tags": "#t"}]}
        _, errors = parse_copy(raw, ["x"])
        self.assertTrue(any("280" in item for item in errors))

    def test_unknown_platform_and_invalid_json_block(self):
        _, unknown = parse_copy(CONTENT, ["unknown"])
        self.assertTrue(any("未配置" in item for item in unknown))
        _, invalid = parse_copy("not JSON", ["linkedin"])
        self.assertTrue(any("JSON" in item for item in invalid))


class ReviewTests(unittest.TestCase):
    def check(self, raw=None, content=None, context=CONTEXT, platforms=None):
        return validate_review(ModelReview.model_validate(raw or response()), content or CONTENT,
                               context, platforms or ["linkedin"])

    def test_supported_facts_pass(self):
        report = self.check()
        self.assertEqual(report.status, "passed")
        self.assertEqual(report.blockers, [])
        self.assertEqual(report.summary, "自审通过")
        self.assertTrue(review_passed({"content": CONTENT, "self_review": report.model_dump()}))

    def test_unverified_and_contradicted_claims_block(self):
        for verdict in ("unsupported", "contradicted"):
            raw = response()
            raw["facts"][0]["verdict"] = verdict
            self.assertEqual(self.check(raw).status, "needs_revision")

    def test_invented_evidence_and_claims_block(self):
        for key in ("claim", "evidence"):
            raw = response()
            raw["facts"][0][key] = "Made up quote"
            self.assertEqual(self.check(raw).status, "needs_revision")

    def test_incomplete_copy_blocks_before_facts(self):
        report = self.check(platforms=["linkedin", "youtube"])
        self.assertEqual(report.status, "needs_revision")
        self.assertIn("format", report.blockers)
        self.assertEqual(report.stuck_at, "self_review")
        self.assertIn("格式", report.summary)

    def test_empty_context_blocks(self):
        report = self.check(context="")
        self.assertEqual(report.status, "needs_revision")
        self.assertIn("empty", report.blockers)

    def test_stale_report_cannot_approve_changed_content(self):
        report = self.check().model_dump()
        changed = json.loads(json.dumps(CONTENT))
        changed["platforms"]["linkedin"]["hook"] = "Award winner!"
        self.assertFalse(review_passed({"content": changed, "self_review": report}))
        self.assertFalse(review_passed({"content": CONTENT}))

    def test_invalid_model_response_and_network_failure_fail_closed(self):
        def broken(value, **kwargs):
            raise TimeoutError("test timeout")
        for runnable in (RunnableLambda(lambda value, **kwargs: AIMessage(content="not JSON")), RunnableLambda(broken)):
            with patch.dict(sys.modules, {"app.infra.ai.llm": fake_module("app.infra.ai.llm", llm=runnable)}):
                with self.assertLogs("app.modules.marketing.workflow.self_review", level="ERROR"):
                    result = self_review({"content": CONTENT, "context": CONTEXT, "social_media": ["linkedin"]})
                self.assertEqual(result["self_review"]["status"], "error")
                self.assertEqual(result["self_review_attempts"], 1)
                self.assertIn("model_error", result["self_review"]["blockers"])
                self.assertFalse(review_passed({"content": CONTENT, **result}))


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.research_calls = []
        cls.failures_left = 0

        def model(value, **kwargs):
            text = value.to_string()
            cls.calls.append(text)
            if "独立的文案审查员" in text:
                raw = response()
                if cls.failures_left > 0:
                    cls.failures_left -= 1
                    raw["facts"][0]["verdict"] = "unsupported"
                return AIMessage(content=json.dumps(raw))
            if "跨境社媒文案" in text:
                return AIMessage(content=json.dumps(GENERATED))
            return AIMessage(content="Brand strategy")

        def research(payload, *, config=None):
            cls.research_calls.append({"payload": payload, "config": config})
            return {
                "structured_response": ResearchReport.model_validate(RESEARCH),
                "research_validation": dict(RESEARCH_VALIDATION),
            }

        doc = types.SimpleNamespace(metadata={"source": "brand.txt"}, page_content="Our product is blue.")
        cls.modules = patch.dict(sys.modules, {
            "app.infra.ai.llm": fake_module("app.infra.ai.llm", llm=RunnableLambda(model)),
            "app.modules.rag.service": fake_module("app.modules.rag.service", search_documents=lambda *a, **k: [(doc, 0)]),
            "app.modules.marketing.workflow.checkpointer": fake_module("app.modules.marketing.workflow.checkpointer", checkpointer=InMemorySaver()),
            "app.modules.marketing.workflow.research_agent": fake_module(
                "app.modules.marketing.workflow.research_agent",
                research_agent=types.SimpleNamespace(invoke=research),
            ),
        })
        cls.modules.start()
        for name in (
            "app.modules.marketing.workflow.copy",
            "app.modules.marketing.workflow.self_review",
            "app.modules.marketing.workflow.nodes",
            "app.modules.marketing.workflow.graph",
            "app.modules.marketing.service",
        ):
            sys.modules.pop(name, None)
        cls.graph_module = importlib.import_module("app.modules.marketing.workflow.graph")
        cls.graph = cls.graph_module.graph

    @classmethod
    def tearDownClass(cls):
        cls.modules.stop()

    def setUp(self):
        type(self).failures_left = 0
        self.calls.clear()
        self.research_calls.clear()
        self.config = {"configurable": {"thread_id": str(uuid4())}}
        self.input = dict(brief_id=uuid4(), team_id=uuid4(), actor_user_id=uuid4(), brand_id=uuid4(), goal="awareness", selling_points="blue",
                          region="eu-na", user_persona="buyers", social_media=["linkedin"],
                          purchase_channels=None, competitor_url=None)

    def test_generation_self_review_interrupt_approval(self):
        chunks = list(self.graph.stream(self.input, self.config, stream_mode="updates"))
        names = [name for chunk in chunks for name in chunk]
        self.assertLess(names.index("retrieve_context"), names.index("research_agent"))
        self.assertLess(names.index("research_agent"), names.index("generate_strategy"))
        self.assertLess(names.index("generate_content"), names.index("self_review"))
        snapshot = self.graph.get_state(self.config)
        self.assertEqual(len(self.research_calls), 1)
        self.assertEqual(snapshot.values["research"], RESEARCH)
        self.assertEqual(snapshot.values["research_validation"], RESEARCH_VALIDATION)
        research_task = json.loads(self.research_calls[0]["payload"]["messages"][0]["content"])
        self.assertEqual(research_task["已有品牌和产品资料"], CONTEXT)
        self.assertEqual(research_task["营销任务"]["goal"], "awareness")
        self.assertTrue(snapshot.interrupts)
        self.assertEqual(snapshot.interrupts[0].value["self_review"]["status"], "passed")
        self.assertEqual(snapshot.values["content"], CONTENT)
        self.graph.invoke(Command(resume={"action": "approved"}), self.config)
        self.assertIn("Our product is blue.", self.graph.get_state(self.config).values["final_output"])

    def test_strategy_receives_research_without_merging_brand_facts(self):
        cases = (
            ("completed", {
                "status": "completed", "summary": "该校宿舍禁止串联插线板。",
                "source_urls": ["https://school.example.test/policy"],
                "uncertainties": ["其他学校的规定尚待补充。"],
            }, {"passed": True, "issues": []}),
            ("insufficient", {
                "status": "insufficient", "summary": "当前仅取得一所学校的资料。",
                "source_urls": ["https://school.example.test/policy"],
                "uncertainties": ["现有资料无法代表美国所有高校。"],
            }, {"passed": True, "issues": []}),
            ("not_needed", {
                "status": "not_needed", "summary": "本任务仅需整理品牌提供的蓝色产品信息。",
                "source_urls": [], "uncertainties": ["资料未提供产品认证。"],
            }, {"passed": True, "issues": []}),
            ("failed_validation", {
                "status": "completed", "summary": "这段外部结论尚未通过证据核对。",
                "source_urls": ["https://school.example.test/unverified"],
                "uncertainties": ["拒绝内容也不能经不确定性泄露：https://school.example.test/rejected。"],
            }, {"passed": False, "issues": ["未通过的错误说法：产品取得UL认证，https://school.example.test/rejected。"]}),
            ("contradictory_validation", {
                "status": "completed", "summary": "审核自身存在矛盾，不能作为外部依据。",
                "source_urls": ["https://school.example.test/conflicting"], "uncertainties": [],
            }, {"passed": True, "issues": ["仍存在实质问题。"]}),
            ("non_boolean_passed", {
                "status": "completed", "summary": "字符串true不能通过代码门槛。",
                "source_urls": ["https://school.example.test/untyped"], "uncertainties": [],
            }, {"passed": "true", "issues": []}),
            ("missing_source", {
                "status": "completed", "summary": "没有任何来源的外部结论。",
                "source_urls": [], "uncertainties": [],
            }, {"passed": True, "issues": []}),
            ("missing_report", None, None),
        )
        decoder = json.JSONDecoder()
        for name, research, validation in cases:
            with self.subTest(case=name):
                self.calls.clear()
                config = {"configurable": {"thread_id": str(uuid4())}}
                values = {
                    **self.input, "purchase_channels": "official-store",
                    "context": "兼容上下文，不能覆盖检索到的品牌资料。",
                    "retrieved_context": CONTEXT,
                }
                if research is not None:
                    values["research"] = research
                if validation is not None:
                    values["research_validation"] = validation
                # Resume the actual graph after research so the missing-report
                # case exercises node input selection and safe missing inputs.
                self.graph.update_state(config, values, as_node="research_agent")
                self.graph.invoke(None, config)
                strategy_calls = [call for call in self.calls if "跨境营销策略顾问" in call]
                self.assertEqual(len(strategy_calls), 1)
                system_text, human_text = strategy_calls[0].split("\nHuman:", 1)
                brand_block = system_text.rsplit("品牌资料：\n", 1)[1].strip()
                self.assertEqual(brand_block, CONTEXT)
                for field in (
                    "目标：awareness", "必须出现的卖点：blue", "目标市场：eu-na",
                    "目标用户：buyers", "投放平台：linkedin", "购买渠道：official-store",
                ):
                    self.assertIn(field, human_text)
                received_research, _ = decoder.raw_decode(
                    human_text.split("调研报告（JSON）：", 1)[1].lstrip(),
                )
                received_validation, _ = decoder.raw_decode(
                    human_text.split("调研核对结果（JSON）：", 1)[1].lstrip(),
                )
                if name == "completed":
                    self.assertEqual(received_research, research)
                    self.assertEqual(received_validation, validation)
                elif name == "not_needed":
                    self.assertEqual(received_research["status"], "not_needed")
                    self.assertEqual(received_research["source_urls"], [])
                    self.assertTrue(received_validation["passed"])
                else:
                    self.assertEqual(received_research["status"], "insufficient")
                    self.assertEqual(received_research["source_urls"], [])
                    self.assertIs(received_validation["passed"], False)
                if name != "completed" and research:
                    self.assertNotIn(research["summary"], human_text)
                    for url in research["source_urls"]:
                        self.assertNotIn(url, human_text)
                    for uncertainty in research["uncertainties"]:
                        self.assertNotIn(uncertainty, human_text)
                if name == "failed_validation":
                    self.assertNotIn("UL认证", human_text)
                    self.assertNotIn("school.example.test/rejected", human_text)
                for rule in (
                    "品牌事实", "外部", "completed", "insufficient", "not_needed", "passed",
                    "source_urls", "uncertainties", "issues", "保守", "推断", "待核实",
                ):
                    self.assertIn(rule, system_text)
                snapshot = self.graph.get_state(config)
                self.assertEqual(snapshot.values["retrieved_context"], CONTEXT)
                self.assertEqual(snapshot.values["context"], values["context"])
                self.assertEqual(snapshot.values.get("research"), research)
                self.assertEqual(snapshot.values.get("research_validation"), validation)
                if name == "completed":
                    for url in research["source_urls"]:
                        self.assertIn(url, snapshot.values["strategy"])
                elif research:
                    for url in research["source_urls"]:
                        self.assertNotIn(url, snapshot.values["strategy"])

    def test_strategy_source_attachment_does_not_duplicate_existing_links(self):
        nodes = sys.modules["app.modules.marketing.workflow.nodes"]
        urls = ["https://school.example.test/one", "https://school.example.test/two"]
        chain = RunnableLambda(lambda value, **kwargs: AIMessage(content="有来源的策略：" + urls[0]))
        with patch.object(nodes, "strategy_chain", chain):
            result = nodes.generate_strategy({
                **self.input, "retrieved_context": CONTEXT,
                "research": {"status": "completed", "summary": "有出处的学校服务。",
                             "source_urls": urls, "uncertainties": []},
                "research_validation": {"passed": True, "issues": []},
            })
        for url in urls:
            self.assertEqual(result["strategy"].count(url), 1)

    def test_incomplete_strategy_does_not_advance_or_save_partial_text(self):
        nodes = sys.modules["app.modules.marketing.workflow.nodes"]
        for name, message in (
            ("truncated", AIMessage(content="只有半份策略", response_metadata={"finish_reason": "length"})),
            ("empty", AIMessage(content="   ", response_metadata={"finish_reason": "stop"})),
        ):
            with self.subTest(case=name):
                config = {"configurable": {"thread_id": str(uuid4())}}
                self.graph.update_state(config, {
                    **self.input, "retrieved_context": CONTEXT,
                    "research": RESEARCH, "research_validation": RESEARCH_VALIDATION,
                }, as_node="research_agent")
                chain = RunnableLambda(lambda value, message=message: message)
                with patch.object(nodes, "strategy_chain", chain):
                    with self.assertRaises(nodes.StrategyGenerationError):
                        self.graph.invoke(None, config)
                snapshot = self.graph.get_state(config)
                self.assertFalse(snapshot.values.get("strategy"))
                self.assertFalse(snapshot.values.get("content"))

    def test_content_keeps_original_marketing_fields(self):
        self.graph.invoke(self.input, self.config)
        content_calls = [call for call in self.calls if "跨境社媒文案" in call]
        self.assertEqual(len(content_calls), 1)
        self.assertIn("awareness", content_calls[0])
        self.assertIn("必须逐字出现在每个平台正文中的卖点：\nblue", content_calls[0])

    def test_research_receives_the_user_competitor_url(self):
        values = {**self.input, "competitor_url": "https://competitor.example.test/product"}
        self.graph.invoke(values, self.config)
        task = json.loads(self.research_calls[0]["payload"]["messages"][0]["content"])
        self.assertEqual(task["用户提供的竞品链接"], values["competitor_url"])

    def test_truncated_content_cannot_be_saved_as_a_complete_draft(self):
        nodes = sys.modules["app.modules.marketing.workflow.nodes"]
        self.graph.update_state(self.config, {
            **self.input, "retrieved_context": CONTEXT, "strategy": "Brand strategy",
            "research": RESEARCH, "research_validation": RESEARCH_VALIDATION,
        }, as_node="generate_strategy")
        response = AIMessage(content=json.dumps(GENERATED), response_metadata={"finish_reason": "length"})
        with patch.object(nodes, "llm", RunnableLambda(lambda value, **kwargs: response)):
            with self.assertRaises(nodes.ContentGenerationError):
                self.graph.invoke(None, self.config)
        snapshot = self.graph.get_state(self.config)
        self.assertFalse(snapshot.values.get("content"))
        self.assertFalse(snapshot.values.get("self_review"))

    def test_failed_self_review_auto_revises_before_human(self):
        type(self).failures_left = 1
        chunks = list(self.graph.stream(self.input, self.config, stream_mode="updates"))
        names = [name for chunk in chunks for name in chunk]
        self.assertEqual(names.count("generate_content"), 2)
        self.assertEqual(names.count("self_review"), 2)
        self.assertNotIn("human_review", names)
        snapshot = self.graph.get_state(self.config)
        self.assertTrue(snapshot.interrupts)
        self.assertEqual(snapshot.interrupts[0].value["self_review"]["status"], "passed")
        content_calls = [c for c in self.calls if "跨境社媒文案" in c]
        self.assertIn('"unsupported"', content_calls[-1])

    def test_self_review_exhausted_never_reaches_human(self):
        type(self).failures_left = 99
        self.graph.invoke(self.input, self.config)
        snapshot = self.graph.get_state(self.config)
        self.assertFalse(snapshot.interrupts)
        self.assertEqual(snapshot.values.get("self_review_attempts"), 3)
        output = snapshot.values["final_output"]
        self.assertTrue(output.startswith("模型自审多次未通过"))
        self.assertIn("卡点：self_review", output)
        self.assertIn("facts", snapshot.values["self_review"]["blockers"])
        self.assertIn("unsupported", output)
        self.assertEqual(sum("独立的文案审查员" in c for c in self.calls), 3)

    def test_human_revision_reruns_self_review(self):
        self.graph.invoke(self.input, self.config)
        self.graph.invoke(Command(resume={"action": "rejected", "feedback": "Tighten the hook"}), self.config)
        snapshot = self.graph.get_state(self.config)
        self.assertEqual(snapshot.values["revision_count"], 1)
        self.assertEqual(snapshot.values["self_review"]["status"], "passed")
        self.assertEqual(snapshot.values.get("self_review_attempts"), 0)
        self.assertEqual(sum("独立的文案审查员" in c for c in self.calls), 2)
        self.assertEqual(len(self.research_calls), 1)
        self.assertEqual(snapshot.values["research"], RESEARCH)

    def test_revision_cap_preserved(self):
        self.graph.invoke(self.input, self.config)
        for _ in range(3):
            self.graph.invoke(Command(resume={"action": "rejected", "feedback": "Revise"}), self.config)
        snapshot = self.graph.get_state(self.config)
        self.assertEqual(snapshot.values["revision_count"], 3)
        self.assertFalse(snapshot.interrupts)
        self.assertTrue(snapshot.values["final_output"].startswith("已达到最大修改次数"))

    def test_service_report_roundtrip_and_approval_guard(self):
        from fastapi import HTTPException
        self.graph.invoke(self.input, self.config)
        snapshot = self.graph.get_state(self.config)
        with patch.dict(sys.modules, {"app.db.database": fake_module("app.db.database", sessionLocal=None)}):
            sys.modules.pop("app.modules.marketing.service", None)
            service = importlib.import_module("app.modules.marketing.service")
        brief = types.SimpleNamespace(id=self.input["brief_id"], team_id=self.input["team_id"], user_id=self.input["actor_user_id"], workflow_status="awaiting_review",
                                      content=CONTENT, strategy="Brand strategy", final_output=None, revision_count=0)
        result = service.to_response(brief, snapshot)
        self.assertEqual(result.self_review.status, "passed")
        self.assertEqual(result.content.platforms["linkedin"].body, "Our product is blue.")
        service.validate_approval(snapshot, "approved")
        with self.assertRaises(HTTPException) as caught:
            service.validate_approval(types.SimpleNamespace(values={"content": CONTENT}), "approved")
        self.assertEqual(caught.exception.status_code, 409)
        with patch.object(service, "get_owned_brief", return_value=brief), patch.object(service.graph, "get_state", return_value=snapshot):
            self.assertEqual(service.get_workflow(None, None, brief.id, team_id=self.input["team_id"]).self_review.status, "passed")
            brief.content = {"version": COPY_VERSION, "platforms": {"linkedin": {
                "hook": "changed", "body": "Our product is blue.", "cta": "Try it", "tags": "#brand",
            }}}
            self.assertEqual(service.get_workflow(None, None, brief.id, team_id=self.input["team_id"]).self_review.status, "passed")
            self.assertEqual(service.get_workflow(None, None, brief.id, team_id=self.input["team_id"]).content.platforms["linkedin"].hook, "Hook")
        self.assertEqual(service.next_node_after("generate_content", {}), "self_review")
        self.assertEqual(service.next_node_after("self_review", {
            "self_review": {"status": "passed"}, "self_review_attempts": 0,
        }), "human_review")
        self.assertEqual(service.next_node_after("self_review", {
            "self_review": {"status": "needs_revision"}, "self_review_attempts": 1,
        }), "generate_content")
        self.assertEqual(service.next_node_after("self_review", {
            "self_review": {"status": "needs_revision"}, "self_review_attempts": 3,
        }), "self_review_exhausted")
        event = service.node_event("self_review", "done", {"self_review": result.self_review.model_dump()})
        self.assertEqual(json.loads(service.encode_stream_event(event).splitlines()[0])["self_review"]["status"], "passed")

    def test_no_context_skips_review(self):
        with patch("app.modules.marketing.workflow.nodes.search_documents", return_value=[]):
            self.graph.invoke(self.input, self.config)
        self.assertFalse(self.calls)
        self.assertFalse(self.research_calls)
        self.assertFalse(self.graph.get_state(self.config).values.get("self_review"))


if __name__ == "__main__":
    unittest.main()
