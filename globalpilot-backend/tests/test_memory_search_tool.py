"""Exercise memory search offline without reading production settings or opening PostgreSQL."""

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.tools import BaseTool
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore  # Initialize before sys.modules isolation.
from pydantic import ValidationError

from app.modules.memory.schemas.brand_memory import BrandMemory, MemoryScope, MemorySource


def load_source(name, relative_path):
    source = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemorySearchToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        embeddings = DeterministicFakeEmbedding(size=8)
        config = types.ModuleType("app.core.config")
        config.settings = types.SimpleNamespace(
            database_url="postgresql://offline.invalid/unused",
            dashscope_embedding_dimensions=8,
        )
        embedding_module = types.ModuleType("app.infra.ai.embeddings")
        embedding_module.embeddings = embeddings
        execution = types.ModuleType("app.modules.marketing.workflow.execution")
        execution.to_psycopg_conninfo = lambda value: value
        # Run the real store functions, replacing only import-time infrastructure.
        with patch.dict(sys.modules, {
            "app.core.config": config,
            "app.infra.ai.embeddings": embedding_module,
            "app.modules.marketing.workflow.execution": execution,
        }):
            cls.memory_store = load_source(
                "memory_store_under_test", "app/modules/memory/store.py",
            )
        with patch.dict(sys.modules, {
            "app.modules.memory.store": cls.memory_store,
        }):
            cls.tools = load_source(
                "memory_tools_under_test", "app/modules/memory/tools.py",
            )
        cls.embeddings = embeddings

    def setUp(self):
        self.store = InMemoryStore(index={
            "dims": 8, "embed": self.embeddings, "fields": ["content"],
        })
        self.scope = MemoryScope(team_id=uuid4(), user_id=uuid4(), brand_profile_id=uuid4())
        self.closed_contexts = 0

        @contextmanager
        def open_store():
            try:
                yield self.store
            finally:
                self.closed_contexts += 1

        store_patch = patch.object(self.tools, "open_memory_store", side_effect=open_store)
        self.open_store = store_patch.start()
        self.addCleanup(store_patch.stop)

    def seed(self, scope=None, **overrides):
        memory = BrandMemory(**{
            "content": "品牌文案简洁直接",
            "regions": "eu-na",
            "platforms": ["Instagram"],
            "sources": [MemorySource(
                brief_id=uuid4(), checkpoint_id="offline-checkpoint", quote="以后简洁直接",
            )],
            **overrides,
        })
        self.store.put(
            self.memory_store.memory_namespace(scope or self.scope),
            str(memory.id), memory.model_dump(mode="json"),
        )
        return memory

    def test_invalid_queries_and_scope_arguments_are_rejected_before_storage(self):
        tool = self.tools.build_search_memory_tool(self.scope)
        payloads = [
            {"query": " \n\t "},
            {"query": "x" * 501},
            {"query": "文案", "user_id": str(uuid4())},
            {"query": "文案", "brand_profile_id": str(uuid4())},
            {"query": "文案", "namespace": ["another-user"]},
            {"query": "文案", "filter": {"status": "inactive"}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    tool.invoke(payload)
        self.open_store.assert_not_called()

    def test_query_normalization_and_traceable_result_are_json_serializable(self):
        memory = self.seed(version=3)
        tool = self.tools.build_search_memory_tool(self.scope)
        self.assertIsInstance(tool, BaseTool)
        with patch.object(
            self.tools, "search_memories", wraps=self.memory_store.search_memories,
        ) as search:
            result = tool.invoke({"query": " \n品牌文案简洁直接\t "})
        self.assertEqual(search.call_args.args[2], "品牌文案简洁直接")
        self.assertEqual(result, {"memories": [{
            "memory_id": str(memory.id),
            "version": 3,
            "content": memory.content,
            "regions": memory.regions,
            "platforms": memory.platforms,
            "sources": [source.model_dump(mode="json") for source in memory.sources],
        }]})
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(self.closed_contexts, 1)

    def test_scope_remains_bound_after_callers_mutate_the_original_object(self):
        memory = self.seed()
        tool = self.tools.build_search_memory_tool(self.scope)
        self.scope.user_id = uuid4()
        self.scope.brand_profile_id = uuid4()
        self.seed(content="品牌文案简洁直接")
        result = tool.invoke({"query": "品牌文案简洁直接"})
        self.assertEqual([item["memory_id"] for item in result["memories"]], [str(memory.id)])

    def test_real_store_search_filters_status_duration_and_user_brand_scope(self):
        expected = self.seed()
        # Identical text gives every row an equally strong vector match.
        self.seed(status="inactive")
        self.seed(duration="task_only")
        self.seed(scope=MemoryScope(team_id=self.scope.team_id,
            user_id=uuid4(), brand_profile_id=self.scope.brand_profile_id,
        ))
        self.seed(scope=MemoryScope(team_id=self.scope.team_id,
            user_id=self.scope.user_id, brand_profile_id=uuid4(),
        ))
        self.seed(scope=MemoryScope(
            team_id=uuid4(), user_id=self.scope.user_id,
            brand_profile_id=self.scope.brand_profile_id,
        ))
        tool = self.tools.build_search_memory_tool(self.scope)
        result = tool.invoke({"query": expected.content})
        self.assertEqual([item["memory_id"] for item in result["memories"]], [str(expected.id)])

    def test_empty_scope_returns_a_successful_empty_list(self):
        tool = self.tools.build_search_memory_tool(self.scope)
        self.assertEqual(tool.invoke({"query": "品牌风格"}), {"memories": []})
        self.assertEqual(self.closed_contexts, 1)


if __name__ == "__main__":
    unittest.main()
