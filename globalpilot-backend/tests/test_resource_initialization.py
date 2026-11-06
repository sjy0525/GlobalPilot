"""Database resources stay lazy on import and initialize only in sync workers."""

import importlib.util
import sys
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import sqlalchemy.engine  # Load dependencies before temporarily replacing modules.

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


def load_isolated(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, APP_ROOT / relative_path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class VectorInitializationTests(unittest.TestCase):
    def setUp(self):
        self.factory = Mock(name="PGVector")
        self.engine = Mock(name="vector_engine")
        self.engine_factory = Mock(return_value=self.engine)
        with patch.dict(sys.modules, {
            "langchain_postgres": module("langchain_postgres", PGVector=self.factory),
            "app.core.config": module("app.core.config", settings=types.SimpleNamespace(database_url="postgresql://unused")),
            "app.db.database": module("app.db.database", async_engine=object()),
            "app.infra.ai.embeddings": module("app.infra.ai.embeddings", embeddings=object()),
        }), patch("sqlalchemy.create_engine", self.engine_factory):
            self.store_module = load_isolated("isolated_vector_initialization", "modules/rag/retrieval/vector_store.py")

    def test_import_does_not_construct_the_database_store(self):
        self.factory.assert_not_called()
        self.engine_factory.assert_not_called()
        self.store_module.close_vector_store()
        self.engine.dispose.assert_not_called()

    def test_concurrent_first_use_initializes_once_and_keeps_shared_handle(self):
        entered, release = Event(), Event()
        store = Mock(name="actual_store")

        def construct(**kwargs):
            self.assertIs(kwargs["connection"], self.engine)
            entered.set()
            if not release.wait(2):
                raise AssertionError("Initialization gate was not released")
            return store

        self.factory.side_effect = construct
        with ThreadPoolExecutor(max_workers=6) as workers:
            first = workers.submit(self.store_module.initialize_vector_store)
            try:
                self.assertTrue(entered.wait(2))
                others = [workers.submit(self.store_module.initialize_vector_store) for _ in range(5)]
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertIs(first.result(timeout=2), store)
            for future in others:
                self.assertIs(future.result(timeout=2), store)
        self.factory.assert_called_once()
        self.engine_factory.assert_called_once()
        self.store_module.vector_store.add_documents(documents=["document"])
        store.add_documents.assert_called_once_with(documents=["document"])
        self.factory.assert_called_once()

    def test_failed_initialization_disposes_engine_and_can_retry(self):
        failure = RuntimeError("database initialization failed")
        store = Mock(name="retry_store")
        self.factory.side_effect = [failure, store]
        with self.assertRaisesRegex(RuntimeError, "database initialization failed"):
            self.store_module.initialize_vector_store()
        self.engine.dispose.assert_called_once()
        self.assertIsNone(self.store_module.vector_store._store)
        self.assertIs(self.store_module.initialize_vector_store(), store)

    def test_failed_cleanup_does_not_replace_initialization_error(self):
        self.factory.side_effect = RuntimeError("original failure")
        self.engine.dispose.side_effect = RuntimeError("cleanup failure")
        with self.assertLogs(self.store_module.logger, level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "original failure"):
                self.store_module.initialize_vector_store()

    def test_close_disposes_once_and_next_initialization_uses_a_new_engine(self):
        next_engine = Mock(name="next_engine")
        first_store, next_store = Mock(), Mock()
        self.engine_factory.side_effect = [self.engine, next_engine]
        self.factory.side_effect = [first_store, next_store]
        self.assertIs(self.store_module.initialize_vector_store(), first_store)
        self.store_module.close_vector_store()
        self.store_module.close_vector_store()
        self.engine.dispose.assert_called_once()
        self.assertIs(self.store_module.initialize_vector_store(), next_store)
        self.assertIs(self.factory.call_args.kwargs["connection"], next_engine)
        self.store_module.close_vector_store()
        next_engine.dispose.assert_called_once()

    def test_installed_pgvector_accepts_the_explicit_engine_without_connecting(self):
        from langchain_postgres import PGVector
        from sqlalchemy import create_engine

        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with patch.object(self.store_module, "create_engine", return_value=engine), \
             patch.object(self.store_module, "PGVector", PGVector), \
             patch.object(PGVector, "__post_init__") as initialize:
            store = self.store_module.initialize_vector_store()
        initialize.assert_called_once()
        self.assertIs(store._engine, engine)
        self.assertFalse(store.async_mode)
        self.store_module.close_vector_store()


class CheckpointerInitializationTests(unittest.TestCase):
    def test_import_keeps_pool_closed_and_setup_opens_before_creating_tables(self):
        events = []
        pool = Mock()
        pool.open.side_effect = lambda: events.append("open")
        pool.close.side_effect = lambda: events.append("close")
        saver = Mock()
        saver.setup.side_effect = lambda: events.append("setup")
        pool_factory, saver_factory = Mock(return_value=pool), Mock(return_value=saver)

        class Saver:
            def __init__(self, connection):
                self.delegate = saver_factory(connection)

            def setup(self):
                self.delegate.setup()

        with patch.dict(sys.modules, {
            "psycopg_pool": module("psycopg_pool", ConnectionPool=pool_factory),
            "langgraph.checkpoint.postgres": module("langgraph.checkpoint.postgres", PostgresSaver=Saver),
            "app.modules.marketing.workflow.execution": module(
                "app.modules.marketing.workflow.execution",
                current_workflow_execution=Mock(),
                require_workflow_execution=Mock(),
                to_psycopg_conninfo=lambda value: value.replace("postgresql+psycopg://", "postgresql://"),
            ),
            "app.core.config": module("app.core.config", settings=types.SimpleNamespace(database_url="postgresql+psycopg://unused")),
        }):
            checkpointer = load_isolated("isolated_checkpointer_initialization", "modules/marketing/workflow/checkpointer.py")
        self.assertIs(pool_factory.call_args.kwargs["open"], False)
        self.assertEqual(events, [])
        checkpointer.setup_checkpointer()
        checkpointer.close_checkpointer()
        self.assertEqual(events, ["open", "setup", "close"])
        saver_factory.assert_called_once_with(pool)


if __name__ == "__main__":
    unittest.main()
