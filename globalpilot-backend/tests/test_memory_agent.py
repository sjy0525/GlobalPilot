"""Offline Agent wiring and tool-loop checks; these do not evaluate LLM judgment."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
import warnings
from unittest.mock import Mock, patch
from uuid import uuid4

from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import Field, ValidationError

from app.modules.memory.schemas.brand_memory import (
    CreateMemoryArgs,
    InvalidateMemoryArgs,
    MemoryScope,
    MemorySource,
    SearchMemoryArgs,
    UpdateMemoryArgs,
)


class ScriptedToolModel(FakeMessagesListChatModel):
    """LangChain's local fake model with tool binding and call observations."""

    calls: int = 0
    inputs: list = Field(default_factory=list)
    bound_names: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bound_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.inputs.append([message.model_copy(deep=True) for message in messages])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def tool_message(name, args, call_id):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


class MemoryAgentTests(unittest.TestCase):
    def setUp(self):
        tracing = patch.dict(os.environ, {
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
        })
        tracing.start()
        self.addCleanup(tracing.stop)
        self.scope = MemoryScope(team_id=uuid4(), user_id=uuid4(), brand_profile_id=uuid4())
        self.source = MemorySource(
            brief_id=uuid4(), checkpoint_id="offline-memory-agent-checkpoint",
            quote="以后品牌文案都采用克制、简洁的语气。",
        )
        self.memory_id = str(uuid4())
        self.executions = []
        self.outputs = {
            "search_memory": {"memories": []},
            "create_memory": {"memory_id": self.memory_id, "version": 1},
            "update_memory": {"ok": True, "memory_id": self.memory_id, "version": 2},
            "invalidate_memory": {
                "ok": True, "memory_id": self.memory_id,
                "version": 2, "status": "inactive",
            },
        }
        tools_module = types.ModuleType("app.modules.memory.tools")
        self.factories = {}
        for tool_name, schema in (
            ("create_memory", CreateMemoryArgs),
            ("search_memory", SearchMemoryArgs),
            ("update_memory", UpdateMemoryArgs),
            ("invalidate_memory", InvalidateMemoryArgs),
        ):
            factory = Mock(side_effect=self.make_factory(tool_name, schema))
            self.factories[tool_name] = factory
            setattr(tools_module, f"build_{tool_name}_tool", factory)

        path = Path(__file__).resolve().parents[1] / "app/modules/memory/agent.py"
        name = "offline_memory_agent_under_test"
        spec = importlib.util.spec_from_file_location(name, path)
        self.agent = importlib.util.module_from_spec(spec)
        # An accidental eager LLM import fails here, before credentials or settings load.
        with patch.dict(sys.modules, {
            name: self.agent,
            "app.modules.memory.tools": tools_module,
            "app.infra.ai.llm": None,
        }):
            spec.loader.exec_module(self.agent)

    def make_factory(self, name, schema):
        def factory(scope, source=None):
            bound_scope = scope.model_copy(deep=True)
            bound_source = source.model_copy(deep=True) if source is not None else None

            def execute(**kwargs):
                self.executions.append({
                    "name": name, "scope": bound_scope,
                    "source": bound_source, "args": kwargs,
                })
                return self.outputs[name]

            return StructuredTool.from_function(
                execute, name=name, description=f"Offline {name} stub",
                args_schema=schema,
            )
        return factory

    def run_agent(self, model, scope=None, source=None):
        return self.agent.run_memory_agent(
            scope or self.scope, source or self.source,
            regions="eu-na", platforms=["Instagram"], model=model,
        )

    def test_build_binds_four_tools_with_correct_trusted_arguments_and_limits(self):
        model = object()
        graph = Mock()
        with patch.object(self.agent, "create_agent", return_value=graph) as create:
            result = self.agent.build_memory_agent(self.scope, self.source, model=model)
        self.assertIs(result, graph)
        self.factories["search_memory"].assert_called_once_with(self.scope)
        for name in ("create_memory", "update_memory", "invalidate_memory"):
            self.factories[name].assert_called_once_with(self.scope, self.source)
        config = create.call_args.kwargs
        self.assertIs(config["model"], model)
        self.assertEqual(config["system_prompt"], self.agent.MEMORY_PROMPT)
        self.assertEqual({tool.name for tool in config["tools"]}, set(self.factories))
        self.assertIs(config["checkpointer"], False)
        limits = {type(item): item for item in config["middleware"]}
        self.assertEqual(set(limits), {ModelCallLimitMiddleware, ToolCallLimitMiddleware})
        self.assertEqual(limits[ModelCallLimitMiddleware].run_limit, 6)
        self.assertEqual(limits[ToolCallLimitMiddleware].run_limit, 8)
        self.assertTrue(all(item.exit_behavior == "error" for item in limits.values()))

    def test_default_model_is_loaded_lazily_only_when_not_injected(self):
        llm_module = types.ModuleType("app.infra.ai.llm")
        llm_module.llm = object()
        with patch.dict(sys.modules, {"app.infra.ai.llm": llm_module}):
            with patch.object(self.agent, "create_agent") as create:
                self.agent.build_memory_agent(self.scope, self.source)
        self.assertIs(create.call_args.kwargs["model"], llm_module.llm)

    def test_run_uses_source_quote_json_context_validated_copies_and_recursion_limit(self):
        self.source.quote = '品牌说："保持克制"。\n不要堆叠形容词。'
        graph = Mock()
        graph.invoke.return_value = {"messages": ["completed"]}
        model = object()
        with patch.object(self.agent, "build_memory_agent", return_value=graph) as build:
            result = self.run_agent(model)
        self.assertIs(result, graph.invoke.return_value)
        bound = build.call_args.kwargs
        self.assertEqual(bound["scope"], self.scope)
        self.assertEqual(bound["source"], self.source)
        self.assertIsNot(bound["scope"], self.scope)
        self.assertIsNot(bound["source"], self.source)
        self.assertIs(bound["model"], model)
        payload = graph.invoke.call_args.args[0]["messages"][0]
        self.assertEqual(payload["role"], "user")
        self.assertIn("品牌", payload["content"])
        self.assertEqual(json.loads(payload["content"]), {
            "user_feedback": self.source.quote,
            "task_context": {"regions": "eu-na", "platforms": ["Instagram"]},
        })
        self.assertEqual(graph.invoke.call_args.kwargs["config"], {"recursion_limit": 40})

    def test_empty_or_whitespace_quote_and_checkpoint_fail_before_build(self):
        with patch.object(self.agent, "build_memory_agent") as build:
            for field in ("quote", "checkpoint_id"):
                for value in ("", " \n\t "):
                    with self.subTest(field=field, value=value):
                        source = self.source.model_copy(update={field: value})
                        with self.assertRaisesRegex(ValueError, "不能为空"):
                            self.run_agent(object(), source=source)
            build.assert_not_called()

    def test_mutated_scope_and_source_are_revalidated_before_build(self):
        cases = [
            (self.scope.model_copy(update={"user_id": "invalid"}), self.source),
            (self.scope.model_copy(update={"brand_profile_id": None}), self.source),
            (self.scope, self.source.model_copy(update={"brief_id": "invalid"})),
            (self.scope, self.source.model_copy(update={"checkpoint_id": None})),
        ]
        with patch.object(self.agent, "build_memory_agent") as build:
            for scope, source in cases:
                with self.subTest(scope=scope, source=source):
                    with self.assertRaises(ValidationError):
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", UserWarning)
                            self.run_agent(object(), scope=scope, source=source)
            build.assert_not_called()

    def test_each_invocation_receives_its_own_user_brand_scope_and_feedback(self):
        other_scope = MemoryScope(team_id=uuid4(), user_id=uuid4(), brand_profile_id=uuid4())
        other_source = MemorySource(
            brief_id=uuid4(), checkpoint_id="other-checkpoint", quote="以后采用热情语气",
        )
        for scope, source in ((self.scope, self.source), (other_scope, other_source)):
            model = ScriptedToolModel(responses=[
                tool_message("search_memory", {"query": "品牌语气"}, str(uuid4())),
                AIMessage(content="没有改动。"),
            ])
            self.run_agent(model, scope=scope, source=source)
        self.assertEqual([call["scope"] for call in self.executions], [self.scope, other_scope])
        for name in ("create_memory", "update_memory", "invalidate_memory"):
            self.assertEqual(self.factories[name].call_args_list[0].args, (self.scope, self.source))
            self.assertEqual(self.factories[name].call_args_list[1].args, (other_scope, other_source))

    def test_final_model_response_can_finish_without_any_tool(self):
        model = ScriptedToolModel(responses=[AIMessage(content="本次为临时要求，无需写入。")])
        result = self.run_agent(model)
        self.assertEqual(result["messages"][-1].content, "本次为临时要求，无需写入。")
        self.assertEqual(self.executions, [])
        self.assertEqual(model.calls, 1)
        self.assertEqual(set(model.bound_names), set(self.factories))

    def test_search_result_can_finish_without_a_write(self):
        self.outputs["search_memory"] = {"memories": [{
            "memory_id": self.memory_id, "version": 1, "content": "长期采用克制语气",
        }]}
        model = ScriptedToolModel(responses=[
            tool_message("search_memory", {"query": "品牌语气"}, "search"),
            AIMessage(content="已有相同偏好，无需重复保存。"),
        ])
        self.run_agent(model)
        self.assertEqual([call["name"] for call in self.executions], ["search_memory"])
        received = [message for message in model.inputs[-1] if isinstance(message, ToolMessage)]
        self.assertEqual(json.loads(received[0].content), self.outputs["search_memory"])

    def test_real_agent_loop_dispatches_search_then_each_write_tool(self):
        cases = [
            ("create_memory", {
                "content": "长期采用克制语气", "regions": "eu-na", "platforms": ["Instagram"],
            }),
            ("update_memory", {
                "memory_id": self.memory_id, "expected_version": 1,
                "content": "长期采用温暖且克制的语气",
            }),
            ("invalidate_memory", {
                "memory_id": self.memory_id, "expected_version": 1,
                "reason": "用户明确撤回长期语气要求",
            }),
        ]
        for name, args in cases:
            with self.subTest(tool=name):
                self.executions.clear()
                model = ScriptedToolModel(responses=[
                    tool_message("search_memory", {"query": "品牌语气"}, "search"),
                    tool_message(name, args, "write"),
                    AIMessage(content="处理完成。"),
                ])
                result = self.run_agent(model)
                self.assertEqual([call["name"] for call in self.executions], ["search_memory", name])
                self.assertEqual(self.executions[-1]["scope"], self.scope)
                self.assertEqual(self.executions[-1]["source"], self.source)
                tool_results = [message for message in result["messages"] if isinstance(message, ToolMessage)]
                self.assertEqual(json.loads(tool_results[-1].content), self.outputs[name])
                self.assertEqual(model.calls, 3)

    def test_business_error_result_reaches_the_model_without_becoming_success(self):
        self.outputs["update_memory"] = {
            "ok": False, "error": "version_conflict", "message": "请重新搜索",
        }
        model = ScriptedToolModel(responses=[
            tool_message("update_memory", {
                "memory_id": self.memory_id, "expected_version": 1,
                "content": "长期采用温暖语气",
            }, "write"),
            AIMessage(content="更新冲突，本轮没有写入。"),
        ])
        self.run_agent(model)
        results = [message for message in model.inputs[-1] if isinstance(message, ToolMessage)]
        self.assertEqual(json.loads(results[0].content), self.outputs["update_memory"])

    def test_model_call_budget_stops_a_repeating_tool_loop(self):
        model = ScriptedToolModel(responses=[
            tool_message("search_memory", {"query": "品牌语气"}, f"repeat-{number}")
            for number in range(7)
        ])
        with self.assertRaises(ModelCallLimitExceededError):
            self.run_agent(model)
        self.assertEqual(model.calls, 6)
        self.assertEqual(len(self.executions), 6)

    def test_tool_call_budget_rejects_a_ninth_tool_in_one_model_response(self):
        model = ScriptedToolModel(responses=[AIMessage(content="", tool_calls=[
            {"name": "search_memory", "args": {"query": f"语气 {number}"}, "id": str(number)}
            for number in range(9)
        ])])
        with self.assertRaises(ToolCallLimitExceededError):
            self.run_agent(model)
        self.assertEqual(model.calls, 1)
        # With exit_behavior="error", an oversized batch is rejected as a whole.
        self.assertEqual(self.executions, [])


if __name__ == "__main__":
    unittest.main()
