"""Exercise the update tool boundary without config, database, or model access."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from langchain_core.tools import BaseTool
from pydantic import ValidationError

from app.modules.memory.schemas.brand_memory import (
    BrandMemory, MemoryScope, MemorySource, UpdateMemoryArgs,
)


def load_source(name, relative_path):
    source = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


class MemoryUpdateToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = types.ModuleType("app.modules.memory.store")
        for name in ("open_memory_store", "save_memory", "get_memory", "search_memories"):
            setattr(store, name, Mock(side_effect=AssertionError("Storage is forbidden")))
        store.memory_namespace = lambda scope: (
            "brand_preferences_v2", str(scope.team_id), str(scope.user_id), str(scope.brand_profile_id),
        )
        config = types.ModuleType("app.core.config")
        config.settings = types.SimpleNamespace()
        embeddings = types.ModuleType("app.infra.ai.embeddings")
        embeddings.embeddings = Mock(side_effect=AssertionError("Model access is forbidden"))
        execution = types.ModuleType("app.modules.marketing.workflow.execution")
        execution.to_psycopg_conninfo = lambda value: value
        with patch.dict(sys.modules, {
            "app.modules.memory.store": store,
            "app.core.config": config,
            "app.infra.ai.embeddings": embeddings,
            "app.modules.marketing.workflow.execution": execution,
        }):
            # Reuse the real exception class while blocking the service's infrastructure.
            cls.service = load_source(
                "brand_memory_service_under_test", "app/modules/memory/services/brand_memory.py",
            )
            with patch.dict(sys.modules, {
                "app.modules.memory.services.brand_memory": cls.service,
            }):
                cls.tools = load_source(
                    "memory_update_tools_under_test", "app/modules/memory/tools.py",
                )

    def setUp(self):
        self.scope = MemoryScope(team_id=uuid4(), user_id=uuid4(), brand_profile_id=uuid4())
        self.source = MemorySource(
            brief_id=uuid4(), checkpoint_id="offline-checkpoint", quote="以后文案更简洁",
        )
        self.memory_id = uuid4()
        self.payload = {
            "memory_id": str(self.memory_id), "expected_version": 1, "content": "文案更简洁",
        }
        self.updated = BrandMemory(
            id=self.memory_id, content="文案更简洁", regions="eu-na",
            platforms=["Instagram"], sources=[self.source], version=2,
        )
        service_patch = patch.object(
            self.tools, "update_brand_memory", return_value=self.updated,
        )
        self.update = service_patch.start()
        self.addCleanup(service_patch.stop)
        self.tool = self.tools.build_update_memory_tool(self.scope, self.source)

    def test_invalid_or_private_arguments_are_rejected_before_the_service(self):
        invalid = [
            {key: value for key, value in self.payload.items() if key != required}
            for required in ("memory_id", "expected_version", "content")
        ]
        invalid.extend([
            {**self.payload, "memory_id": "not-a-uuid"},
            {**self.payload, "expected_version": 0},
            {**self.payload, "content": " \n\t "},
            {**self.payload, "content": "x" * 2001},
            {**self.payload, "regions": " \t "},
            {**self.payload, "regions": "x" * 101},
        ])
        for name, value in (
            ("user_id", str(uuid4())), ("brand_profile_id", str(uuid4())),
            ("sources", []), ("source", {}), ("status", "inactive"), ("version", 99),
        ):
            invalid.append({**self.payload, name: value})
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.tool.invoke(payload)
        self.update.assert_not_called()

    def test_optional_scope_fields_preserve_none_and_explicit_empty_platforms(self):
        for optional, regions, platforms in (
            ({}, None, None),
            ({"regions": None, "platforms": None}, None, None),
            ({"platforms": []}, None, []),
            ({"regions": " us ", "platforms": ["YouTube"]}, "us", ["YouTube"]),
        ):
            with self.subTest(optional=optional):
                self.update.reset_mock()
                self.tool.invoke({**self.payload, "content": " 更简洁 ", **optional})
                self.update.assert_called_once()
                args = self.update.call_args.args[2]
                self.assertIsInstance(args, UpdateMemoryArgs)
                self.assertEqual(args.content, "更简洁")
                self.assertEqual(args.regions, regions)
                self.assertEqual(args.platforms, platforms)

    def test_scope_and_feedback_remain_bound_after_external_mutation(self):
        original_scope = self.scope.model_copy(deep=True)
        original_source = self.source.model_copy(deep=True)
        self.scope.user_id = uuid4()
        self.scope.brand_profile_id = uuid4()
        self.source.brief_id = uuid4()
        self.source.checkpoint_id = "changed-checkpoint"
        self.source.quote = "被外部改写的原话"
        self.tool.invoke(self.payload)
        bound_scope, bound_source, _ = self.update.call_args.args
        self.assertEqual(bound_scope, original_scope)
        self.assertEqual(bound_source, original_source)
        self.assertIsNot(bound_scope, self.scope)
        self.assertIsNot(bound_source, self.source)

    def test_success_returns_the_service_result_version(self):
        self.assertIsInstance(self.tool, BaseTool)
        result = self.tool.invoke(self.payload)
        self.assertEqual(result, {
            "ok": True, "memory_id": str(self.updated.id), "version": self.updated.version,
        })

    def test_business_errors_are_structured_and_do_not_report_success(self):
        for code in (
            "not_found", "not_editable", "version_conflict", "history_conflict", "invalid_source",
        ):
            with self.subTest(code=code):
                self.update.side_effect = self.service.MemoryUpdateError(code, "需要重新检查记忆")
                self.assertEqual(self.tool.invoke(self.payload), {
                    "ok": False, "error": code, "message": "需要重新检查记忆",
                })

    def test_unexpected_service_failure_propagates_instead_of_reporting_success(self):
        self.update.side_effect = RuntimeError("synthetic backend failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic backend failure"):
            self.tool.invoke(self.payload)


if __name__ == "__main__":
    unittest.main()
