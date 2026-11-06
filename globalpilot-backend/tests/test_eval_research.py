"""Exercise eval's research handoff and provenance without providers or a DB."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables.config import var_child_runnable_config

from app.modules.evaluation.engine.usage import UsageHandler


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def fake_module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, APP_ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvalResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def forbidden(*args, **kwargs):
            raise AssertionError("No production database or provider may run")

        cls.nodes = fake_module(
            "app.modules.marketing.workflow.nodes",
            NO_KNOWLEDGE="没有品牌资料",
            retrieve_context=forbidden,
            research_node=forbidden,
            generate_strategy=forbidden,
            generate_content=forbidden,
            PROMPT_VERSION="test-marketing-prompt-v3",
            strategy_prompt=ChatPromptTemplate.from_messages([("system", "策略提示")]),
            content_prompt=ChatPromptTemplate.from_messages([("system", "文案提示")]),
        )
        cls.research_module = fake_module(
            "app.modules.marketing.workflow.research_agent",
            RESEARCH_PROMPT="调研提示",
            REVIEW_PROMPT="证据核对提示",
        )
        cls.modules = patch.dict(sys.modules, {
            "app.db.database": fake_module("app.db.database", sessionLocal=forbidden),
            "app.core.config": fake_module("app.core.config", settings=types.SimpleNamespace()),
            "app.modules.marketing.workflow.nodes": cls.nodes,
            "app.modules.marketing.workflow.research_agent": cls.research_module,
        })
        cls.modules.start()
        try:
            cls.runner = load_module("eval_research_test_runner", "modules/evaluation/engine/runner.py")
            cls.versions = load_module("eval_research_test_versions", "modules/evaluation/engine/versions.py")
        except BaseException:
            cls.modules.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.modules.stop()

    def test_first_draft_research_precedes_strategy_and_counts_its_model_calls(self):
        calls = []
        handler = UsageHandler()
        model = FakeMessagesListChatModel(responses=[
            AIMessage(content="调研结论", usage_metadata={
                "input_tokens": 11, "output_tokens": 3, "total_tokens": 14,
            }),
            AIMessage(content="独立核对", usage_metadata={
                "input_tokens": 17, "output_tokens": 5, "total_tokens": 22,
            }),
        ])
        research = {"status": "completed", "summary": "有来源的结论", "source_urls": []}
        validation = {"passed": True}
        content = {"platforms": {"linkedin": {"body": "品牌文案"}}}

        def retrieve(state):
            calls.append("retrieve_context")
            return {"context": "品牌资料", "retrieved_context": "品牌资料"}

        def investigate(state, config):
            calls.append("research_agent")
            self.assertEqual(state["retrieved_context"], "品牌资料")
            self.assertEqual(config["callbacks"], [handler])
            model.invoke("调研", config=config)
            # Verification invoked inside the run also inherits the same usage
            # context, matching research's independent-review call.
            model.invoke("核对")
            return {"research": deepcopy(research), "research_validation": dict(validation)}

        def strategy(state):
            calls.append("generate_strategy")
            self.assertEqual(state["research"], research)
            self.assertEqual(state["research_validation"], validation)
            self.assertEqual(state["retrieved_context"], "品牌资料")
            return {"strategy": "审核后的策略"}

        def generate_content(state):
            calls.append("generate_content")
            self.assertEqual(state["strategy"], "审核后的策略")
            self.assertEqual(state["research"], research)
            return {"content": deepcopy(content)}

        with patch.multiple(self.runner, retrieve_context=retrieve, research_node=investigate,
                            generate_strategy=strategy, generate_content=generate_content):
            result = self.runner.generate_first_draft({"goal": "营销目标"}, handler)
        self.assertEqual(calls, ["retrieve_context", "research_agent", "generate_strategy", "generate_content"])
        self.assertEqual(result["strategy"], "审核后的策略")
        self.assertEqual(result["content"], content)
        self.assertFalse(result["empty"])
        self.assertEqual(handler.snapshot()["calls"], 2)
        self.assertEqual(handler.snapshot()["input_tokens"], 28)
        self.assertEqual(handler.snapshot()["output_tokens"], 8)

    def test_missing_brand_context_does_not_run_research_or_generation(self):
        research = Mock(side_effect=AssertionError("Research must not run without brand facts"))
        strategy = Mock(side_effect=AssertionError("Strategy must not run"))
        content = Mock(side_effect=AssertionError("Content must not run"))
        with patch.multiple(
            self.runner,
            retrieve_context=lambda state: {"context": "", "retrieved_context": ""},
            research_node=research,
            generate_strategy=strategy,
            generate_content=content,
        ):
            result = self.runner.generate_first_draft({}, UsageHandler())
        self.assertEqual(result["blocked"], "没有品牌资料")
        self.assertTrue(result["empty"])
        research.assert_not_called()
        strategy.assert_not_called()
        content.assert_not_called()

    def test_research_failure_stops_generation_and_restores_callback_context(self):
        outer = {"callbacks": [UsageHandler()]}
        token = var_child_runnable_config.set(outer)
        strategy = Mock(side_effect=AssertionError("Rejected research must not reach strategy"))
        try:
            with patch.multiple(
                self.runner,
                retrieve_context=lambda state: {"context": "品牌资料"},
                research_node=Mock(side_effect=RuntimeError("审核失败")),
                generate_strategy=strategy,
            ):
                with self.assertRaisesRegex(RuntimeError, "审核失败"):
                    self.runner.generate_first_draft({}, UsageHandler())
            self.assertIs(var_child_runnable_config.get(), outer)
            strategy.assert_not_called()
        finally:
            var_child_runnable_config.reset(token)

    def test_research_and_review_prompt_changes_both_change_fingerprint(self):
        baseline = self.versions.prompt_identity()
        for prompt_name in ("RESEARCH_PROMPT", "REVIEW_PROMPT"):
            with self.subTest(prompt=prompt_name):
                original = getattr(self.research_module, prompt_name)
                with patch.object(self.research_module, prompt_name, original + "；新规则"):
                    updated = self.versions.prompt_identity()
                self.assertEqual(updated[0], baseline[0])
                self.assertNotEqual(updated[1], baseline[1])
                self.assertEqual(self.versions.prompt_identity(), baseline)


if __name__ == "__main__":
    unittest.main()
