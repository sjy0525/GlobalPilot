"""Exercise invalidation schemas and tools without database or external model access."""

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from langchain_core.tools import BaseTool
from pydantic import ValidationError

from app.modules.memory.schemas.brand_memory import (
    BrandMemory, InvalidateMemoryArgs, MemoryScope, MemorySource,
)


def load_source(name, relative_path):
    source = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


class MemoryInvalidationToolTests(unittest.TestCase):
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
            # Load the real exception type, but prevent infrastructure access.
            cls.service = load_source(
                "brand_memory_invalidation_service_under_test",
                "app/modules/memory/services/brand_memory.py",
            )
            with patch.dict(sys.modules, {
                "app.modules.memory.services.brand_memory": cls.service,
            }):
                cls.tools = load_source(
                    "memory_invalidation_tools_under_test",
                    "app/modules/memory/tools.py",
                )

    def setUp(self):
        self.scope = MemoryScope(team_id=uuid4(), user_id=uuid4(), brand_profile_id=uuid4())
        self.source = MemorySource(
            brief_id=uuid4(), checkpoint_id="offline-checkpoint", quote="撤回以前的极简风格要求",
        )
        self.memory_id = uuid4()
        self.payload = {
            "memory_id": str(self.memory_id),
            "expected_version": 3,
            "reason": "用户撤回原偏好",
        }
        self.invalidated = BrandMemory(
            id=self.memory_id, content="品牌文案采用极简风格", regions="eu-na",
            platforms=["Instagram"], sources=[self.source], version=4,
            status="inactive", invalidation_reason=self.payload["reason"],
        )
        service_patch = patch.object(
            self.tools, "invalidate_brand_memory", return_value=self.invalidated,
        )
        self.invalidate = service_patch.start()
        self.addCleanup(service_patch.stop)
        self.tool = self.tools.build_invalidate_memory_tool(self.scope, self.source)

    def test_invalid_or_private_arguments_are_rejected_before_the_service(self):
        invalid = [
            {key: value for key, value in self.payload.items() if key != required}
            for required in ("memory_id", "expected_version", "reason")
        ]
        invalid.extend([
            {**self.payload, "memory_id": "not-a-uuid"},
            {**self.payload, "reason": ""},
            {**self.payload, "reason": " \n\t "},
            {**self.payload, "reason": "x" * 1001},
            {**self.payload, "reason": 123},
            {**self.payload, "reason": None},
        ])
        invalid.extend(
            {**self.payload, "expected_version": value}
            for value in (None, 0, -1, True, "3", 3.0, 3.5)
        )
        for name, value in (
            ("user_id", str(uuid4())), ("brand_profile_id", str(uuid4())),
            ("sources", []), ("source", {}), ("content", "覆盖正文"),
            ("status", "active"), ("version", 99), ("namespace", ["another-user"]),
        ):
            invalid.append({**self.payload, name: value})
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.tool.invoke(payload)
        self.invalidate.assert_not_called()

    def test_normalized_reason_and_current_version_reach_the_service(self):
        self.tool.invoke({**self.payload, "reason": " \n用户已明确撤回\t "})
        self.invalidate.assert_called_once()
        args = self.invalidate.call_args.args[2]
        self.assertIsInstance(args, InvalidateMemoryArgs)
        self.assertEqual(args.memory_id, self.memory_id)
        self.assertEqual(args.expected_version, 3)
        self.assertEqual(args.reason, "用户已明确撤回")
        # The documented maximum remains valid, rather than being an off-by-one limit.
        self.tool.invoke({**self.payload, "reason": "x" * 1000})
        self.assertEqual(len(self.invalidate.call_args.args[2].reason), 1000)

    def test_scope_and_feedback_remain_bound_after_external_mutation(self):
        original_scope = self.scope.model_copy(deep=True)
        original_source = self.source.model_copy(deep=True)
        self.scope.user_id = uuid4()
        self.scope.brand_profile_id = uuid4()
        self.source.brief_id = uuid4()
        self.source.checkpoint_id = "changed-checkpoint"
        self.source.quote = "被外部改写的原话"
        self.tool.invoke(self.payload)
        bound_scope, bound_source, _ = self.invalidate.call_args.args
        self.assertEqual(bound_scope, original_scope)
        self.assertEqual(bound_source, original_source)
        self.assertIsNot(bound_scope, self.scope)
        self.assertIsNot(bound_source, self.source)

    def test_success_returns_the_service_id_version_and_status(self):
        self.assertIsInstance(self.tool, BaseTool)
        result = self.tool.invoke(self.payload)
        self.assertEqual(result, {
            "ok": True,
            "memory_id": str(self.invalidated.id),
            "version": self.invalidated.version,
            "status": "inactive",
        })
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_business_errors_are_structured_and_do_not_report_success(self):
        for code in (
            "not_found", "version_conflict", "not_editable", "invalid_source", "history_conflict",
        ):
            with self.subTest(code=code):
                self.invalidate.side_effect = self.service.MemoryUpdateError(code, "请重新检查记忆")
                self.assertEqual(self.tool.invoke(self.payload), {
                    "ok": False, "error": code, "message": "请重新检查记忆",
                })

    def test_unexpected_service_failure_propagates_instead_of_reporting_success(self):
        self.invalidate.side_effect = RuntimeError("synthetic backend failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic backend failure"):
            self.tool.invoke(self.payload)

    def test_legacy_json_without_invalidation_reason_still_loads(self):
        legacy = self.invalidated.model_dump(mode="json", exclude={"invalidation_reason"})
        for status in ("active", "inactive"):
            with self.subTest(status=status):
                restored = BrandMemory.model_validate({**legacy, "status": status})
                self.assertIsNone(restored.invalidation_reason)
                self.assertEqual(restored.id, self.memory_id)
                self.assertEqual(restored.content, self.invalidated.content)

    def test_stored_reason_rejects_whitespace_and_excessive_length(self):
        payload = self.invalidated.model_dump(mode="json")
        for reason in ("", " \n\t ", "x" * 1001):
            with self.subTest(reason=reason):
                with self.assertRaises(ValidationError):
                    BrandMemory.model_validate({**payload, "invalidation_reason": reason})
        restored = BrandMemory.model_validate({**payload, "invalidation_reason": " 用户撤回 "})
        self.assertEqual(restored.invalidation_reason, "用户撤回")


if __name__ == "__main__":
    unittest.main()
