"""Native async lifecycle, sync resource isolation and cancellation cleanup."""
import asyncio
import importlib.util
import sys
import threading
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from anyio import CancelScope, sleep
from fastapi import APIRouter
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from app.core.http_errors import APIErrorMiddleware, http_exception_handler


def module(name, **attributes):
    value = types.ModuleType(name)
    value.__dict__.update(attributes)
    return value


from app.modules.brands.models import Brand as FakeBrand


class LifespanConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        modules = {
            "app.api.router": module("app.api.router", api_router=APIRouter()),
            "app.core.config": module("app.core.config", settings=types.SimpleNamespace(auth_allowed_origins=["http://localhost:5173"], database_url="postgresql://unused")),
            "app.db.concurrency_migrations": module("app.db.concurrency_migrations", apply_concurrency_migrations=Mock()),
            "app.db.query_indexes": module("app.db.query_indexes", ensure_query_indexes=Mock()),
            "app.db.base": module("app.db.base", Base=types.SimpleNamespace(metadata=types.SimpleNamespace(create_all=Mock()))),
            "app.db.database": module("app.db.database", async_engine=Mock(), async_sessionLocal=Mock()),
            "app.modules.brands.models": module("app.modules.brands.models", Brand=FakeBrand, DEFAULT_BRAND_NAMES=("Audio",)),
            "app.modules.briefs.models": module("app.modules.briefs.models", Brief=object),
            "app.modules.rag.models": module("app.modules.rag.models", KnowledgeDocument=object),
            "app.modules.evaluation.models": module("app.modules.evaluation.models", EvalCase=object, EvalExecutorLease=object, EvalRun=object, EvalTask=object, EvalTestSet=object),
            "app.modules.users.models": module("app.modules.users.models", User=object),
            "app.modules.teams.models": module("app.modules.teams.models", Team=object, TeamMember=object),
            "app.modules.memory.models": module("app.modules.memory.models", MemoryJob=object),
            "app.modules.rag.retrieval.vector_store": module("app.modules.rag.retrieval.vector_store", ainitialize_vector_store=AsyncMock(), aclose_vector_store=AsyncMock(), close_vector_store=Mock()),
            "app.modules.rag.retrieval.rerank": module("app.modules.rag.retrieval.rerank", aclose_rerank_client=AsyncMock(), initialize_async_rerank_client=Mock()),
            "app.modules.rag.processing.document_processing": module("app.modules.rag.processing.document_processing", initialize_document_processing=Mock(), close_document_processing=Mock()),
            "app.modules.evaluation.service": module("app.modules.evaluation.service", ensure_test_set=Mock(), recover_interrupted_runs=Mock()),
            "app.modules.marketing.workflow.checkpointer": module("app.modules.marketing.workflow.checkpointer", setup_checkpointer=Mock(), close_checkpointer=Mock()),
        }
        path = Path(__file__).resolve().parents[1] / "app/main.py"
        spec = importlib.util.spec_from_file_location("isolated_native_lifespan", path)
        cls.main = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.main)

    def setUp(self):
        self.loop_thread = threading.get_ident()
        self.events, self.sessions, self.logs = [], [], []
        test = self

        def record(name):
            self.events.append((name, threading.get_ident()))

        self.record = record

        class SyncConnection:
            def execute(self, statement, *parameters):
                record("schema:execute")
                return types.SimpleNamespace(scalar=lambda: "jsonb", all=lambda: [])

        class ConnectionContext:
            async def __aenter__(self):
                record("schema:begin")
                return self

            async def __aexit__(self, *errors):
                record("schema:end")

            async def run_sync(self, function, *args, **kwargs):
                record("schema:bridge")
                await asyncio.sleep(0)
                return function(SyncConnection(), *args, **kwargs)

        class Session:
            def __init__(self):
                self.closed = False
                test.sessions.append(self)
                record("session:create")

            async def run_sync(self, function):
                record("session:bridge")
                await asyncio.sleep(0)
                return function(self)

            def query(self, model):
                record("session:query")
                return types.SimpleNamespace(all=lambda: [])

            def execute(self, statement):
                record("session:query")

            def add(self, value):
                record("session:add")

            def commit(self):
                record("session:commit")

            async def close(self):
                record("session:close")
                self.closed = True

        def log(*args, **kwargs):
            self.logs.append((threading.get_ident(), args, kwargs))

        @asynccontextmanager
        async def schema_lock():
            record("schema:lock")
            try:
                yield
            finally:
                record("schema:unlock")

        self.startup_schema_lock = self.main._startup_schema_lock
        engine = types.SimpleNamespace(begin=lambda: ConnectionContext(), dispose=AsyncMock(side_effect=lambda: record("engine:dispose")))
        values = {
            "_startup_schema_lock": schema_lock,
            "async_engine": engine,
            "async_sessionLocal": Mock(side_effect=Session),
            "ainitialize_vector_store": AsyncMock(side_effect=lambda: record("vector:init")),
            "aclose_vector_store": AsyncMock(side_effect=lambda: record("vector:close")),
            "aclose_rerank_client": AsyncMock(side_effect=lambda: record("http:close")),
            "initialize_async_rerank_client": Mock(side_effect=lambda: record("http:create")),
            "initialize_document_processing": Mock(side_effect=lambda: record("pdf:create")),
            "close_document_processing": Mock(side_effect=lambda: record("pdf:close")),
            "setup_checkpointer": Mock(side_effect=lambda: record("checkpoint:setup")),
            "initialize_memory_resources": Mock(side_effect=lambda: record("memory:setup")),
            "close_memory_resources": Mock(side_effect=lambda: record("memory:close")),
            "ensure_query_indexes": Mock(side_effect=lambda *args, **kwargs: record("indexes:setup")),
            "apply_concurrency_migrations": Mock(side_effect=lambda conn: record("schema:migrate")),
            "_prepare_team_scope_schema": Mock(side_effect=lambda conn: record("schema:team_scope")),
            "close_checkpointer": Mock(side_effect=lambda: record("checkpoint:close")),
            "close_vector_store": Mock(side_effect=lambda: record("sync-vector:close")),
            "recover_interrupted_runs": Mock(side_effect=lambda db: record("eval:recover")),
            "ensure_test_set": Mock(side_effect=lambda db: record("eval:ensure")),
            "seed_all_team_brands": Mock(side_effect=lambda db: record("session:query")),
            "logger": Mock(error=Mock(side_effect=log), exception=Mock(side_effect=log)),
        }
        for name, value in values.items():
            item = patch.object(self.main, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(self.main.Base.metadata, "create_all", side_effect=lambda **kwargs: record("schema:create_all"))
        item.start()
        self.addCleanup(item.stop)

    async def test_native_database_uses_loop_and_sync_resources_use_workers(self):
        async with self.main.lifespan(self.main.app):
            self.assertTrue(self.sessions[0].closed)
        sync_names = {"http:create", "pdf:create", "indexes:setup", "checkpoint:setup", "checkpoint:close", "sync-vector:close", "pdf:close", "memory:setup", "memory:close"}
        for name, thread in self.events:
            with self.subTest(name=name):
                self.assertEqual(thread != self.loop_thread, name in sync_names)
        names = [name for name, thread in self.events]
        self.assertLess(names.index("schema:lock"), names.index("vector:init"))
        self.assertLess(names.index("vector:init"), names.index("schema:create_all"))
        self.assertLess(names.index("schema:create_all"), names.index("schema:migrate"))
        self.assertLess(names.index("schema:migrate"), names.index("schema:end"))
        self.main.apply_concurrency_migrations.assert_called_once()
        self.assertLess(names.index("schema:end"), names.index("checkpoint:setup"))
        self.assertLess(names.index("schema:end"), names.index("indexes:setup"))
        self.assertLess(names.index("indexes:setup"), names.index("checkpoint:setup"))
        self.assertLess(names.index("checkpoint:setup"), names.index("schema:unlock"))
        self.assertLess(names.index("schema:unlock"), names.index("session:create"))
        self.assertLess(names.index("session:commit"), names.index("eval:recover"))
        self.assertLess(names.index("eval:recover"), names.index("eval:ensure"))
        self.assertTrue(all(session.closed for session in self.sessions))

    async def test_schema_lock_uses_dedicated_autocommit_session_and_closes_it(self):
        cursor = types.SimpleNamespace(fetchone=AsyncMock(return_value=(True,)))
        connection = types.SimpleNamespace(execute=AsyncMock(return_value=cursor), close=AsyncMock())
        with patch.object(self.main, "settings", types.SimpleNamespace(database_url="postgresql+psycopg://startup.invalid/test")), \
             patch.object(self.main.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection)) as connect:
            async with self.startup_schema_lock():
                connection.execute.assert_awaited_once_with("SELECT pg_try_advisory_lock(7140214001)")
                connection.close.assert_not_awaited()
        connect.assert_awaited_once_with("postgresql://startup.invalid/test", autocommit=True, connect_timeout=10)
        connection.close.assert_awaited_once()

    async def test_failed_schema_lock_acquisition_closes_session(self):
        original = RuntimeError("lock acquisition failed")
        connection = types.SimpleNamespace(execute=AsyncMock(side_effect=original), close=AsyncMock())
        with patch.object(self.main, "settings", types.SimpleNamespace(database_url="postgresql://startup.invalid/test")), \
             patch.object(self.main.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection)):
            with self.assertRaises(RuntimeError) as caught:
                async with self.startup_schema_lock():
                    self.fail("Failed lock acquisition entered initialization")
        self.assertIs(caught.exception, original)
        connection.close.assert_awaited_once()

    async def test_schema_lock_retries_without_a_blocking_lock_query(self):
        cursor = types.SimpleNamespace(fetchone=AsyncMock(side_effect=[(False,), (True,)]))
        connection = types.SimpleNamespace(execute=AsyncMock(return_value=cursor), close=AsyncMock())
        with patch.object(self.main, "settings", types.SimpleNamespace(database_url="postgresql://startup.invalid/test")), \
             patch.object(self.main.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection)), \
             patch.object(self.main.asyncio, "sleep", AsyncMock()) as backoff:
            async with self.startup_schema_lock():
                pass
        self.assertEqual(connection.execute.await_count, 2)
        backoff.assert_awaited_once_with(0.05)
        connection.close.assert_awaited_once()

    async def test_cancelled_schema_lock_wait_closes_session(self):
        waiting = asyncio.Event()

        async def not_acquired():
            waiting.set()
            return (False,)

        async def run():
            async with self.startup_schema_lock():
                self.fail("Busy lock entered initialization")

        cursor = types.SimpleNamespace(fetchone=AsyncMock(side_effect=not_acquired))
        connection = types.SimpleNamespace(execute=AsyncMock(return_value=cursor), close=AsyncMock())
        with patch.object(self.main, "settings", types.SimpleNamespace(database_url="postgresql://startup.invalid/test")), \
             patch.object(self.main.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection)):
            actor = asyncio.create_task(run())
            await asyncio.wait_for(waiting.wait(), 2)
            actor.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await actor
        connection.close.assert_awaited_once()

    async def test_schema_lock_cleanup_failure_preserves_initialization_error(self):
        original = RuntimeError("schema initialization failed")
        cursor = types.SimpleNamespace(fetchone=AsyncMock(return_value=(True,)))
        connection = types.SimpleNamespace(execute=AsyncMock(return_value=cursor), close=AsyncMock(side_effect=RuntimeError("close failed")))
        with patch.object(self.main, "settings", types.SimpleNamespace(database_url="postgresql://startup.invalid/test")), \
             patch.object(self.main.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection)):
            with self.assertRaises(RuntimeError) as caught:
                async with self.startup_schema_lock():
                    raise original
        self.assertIs(caught.exception, original)
        self.assertEqual(len(self.logs), 1)

    async def test_repeated_cancel_drains_schema_lock_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        closed = []

        async def close():
            entered.set()
            await release.wait()
            closed.append(True)

        async def run():
            async with self.startup_schema_lock():
                pass

        cursor = types.SimpleNamespace(fetchone=AsyncMock(return_value=(True,)))
        connection = types.SimpleNamespace(execute=AsyncMock(return_value=cursor), close=AsyncMock(side_effect=close))
        with patch.object(self.main, "settings", types.SimpleNamespace(database_url="postgresql://startup.invalid/test")), \
             patch.object(self.main.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection)):
            actor = asyncio.create_task(run())
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(2):
                actor.cancel()
                await asyncio.sleep(0)
                self.assertFalse(actor.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await actor
        self.assertEqual(closed, [True])

    async def test_native_io_wait_allows_other_coroutines_to_progress(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def initialize():
            self.record("vector:waiting")
            entered.set()
            await release.wait()

        self.main.ainitialize_vector_store.side_effect = initialize
        actor = asyncio.create_task(self.main.initialize_app())
        await asyncio.wait_for(entered.wait(), 2)
        self.assertFalse(actor.done())
        await asyncio.sleep(0)
        release.set()
        await actor
        await self.main.shutdown_app()

    async def test_synchronous_resource_wait_does_not_block_loop(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def initialize():
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise AssertionError("Event loop could not release startup worker")

        self.main.initialize_document_processing.side_effect = initialize
        actor = asyncio.create_task(self.main.initialize_app())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertFalse(actor.done())
            await asyncio.sleep(0)
        finally:
            release.set()
        await actor
        await self.main.shutdown_app()

    async def test_startup_failure_releases_all_resources_and_preserves_error(self):
        original = RuntimeError("native startup failed")
        self.main.ainitialize_vector_store.side_effect = original
        with self.assertRaises(RuntimeError) as caught:
            async with self.main.lifespan(self.main.app):
                self.fail("Failed startup became ready")
        self.assertIs(caught.exception, original)
        self.main.aclose_vector_store.assert_awaited_once()
        self.main.aclose_rerank_client.assert_awaited_once()
        self.main.async_engine.dispose.assert_awaited_once()
        self.main.close_checkpointer.assert_called_once()
        self.main.close_document_processing.assert_called_once()

    async def test_startup_and_cleanup_errors_keep_original(self):
        original = RuntimeError("migration failed")
        with patch.object(self.main, "_prepare_schema", side_effect=original):
            self.main.aclose_vector_store.side_effect = RuntimeError("vector close failed")
            self.main.close_document_processing.side_effect = RuntimeError("pdf close failed")
            with self.assertRaises(RuntimeError) as caught:
                await self.main.initialize_app()
        self.assertIs(caught.exception, original)
        self.main.async_engine.dispose.assert_awaited_once()
        self.main.close_checkpointer.assert_called_once()
        self.assertTrue(self.logs)
        self.assertTrue(all(thread != self.loop_thread for thread, args, kwargs in self.logs))

    async def test_recovery_and_session_close_errors_keep_recovery_error(self):
        original = RuntimeError("recovery failed")

        def recover(db):
            db.close = AsyncMock(side_effect=RuntimeError("session close failed"))
            raise original

        self.main.recover_interrupted_runs.side_effect = recover
        with self.assertRaises(RuntimeError) as caught:
            await self.main.initialize_app()
        self.assertIs(caught.exception, original)
        self.sessions[0].close.assert_awaited_once()
        self.main.close_document_processing.assert_called_once()
        self.assertTrue(all(thread != self.loop_thread for thread, args, kwargs in self.logs))

    async def test_body_error_survives_shutdown_error(self):
        original = RuntimeError("body failed")
        with self.assertRaises(RuntimeError) as caught:
            async with self.main.lifespan(self.main.app):
                self.main.aclose_vector_store.side_effect = RuntimeError("shutdown failed")
                raise original
        self.assertIs(caught.exception, original)
        self.main.close_checkpointer.assert_called_once()
        self.main.close_document_processing.assert_called_once()
        self.assertTrue(all(thread != self.loop_thread for thread, args, kwargs in self.logs))

    async def test_shutdown_error_is_visible_after_success(self):
        with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
            async with self.main.lifespan(self.main.app):
                self.main.aclose_vector_store.side_effect = RuntimeError("shutdown failed")
        self.main.close_document_processing.assert_called_once()

    async def test_anyio_cancellation_still_closes_all_resources(self):
        with CancelScope() as scope:
            async with self.main.lifespan(self.main.app):
                scope.cancel()
                await sleep(0)
        self.main.aclose_vector_store.assert_awaited_once()
        self.main.close_document_processing.assert_called_once()

    async def cancel_during_shutdown(self, repeat=False, fail=False, body_error=False):
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []
        original = RuntimeError("body failed")

        async def close_http():
            entered.set()
            await release.wait()
            completed.append(True)
            if fail:
                raise RuntimeError("client close failed while cancelled")

        async def run():
            async with self.main.lifespan(self.main.app):
                if body_error:
                    raise original

        self.main.aclose_rerank_client.side_effect = close_http
        actor = asyncio.create_task(run())
        await asyncio.wait_for(entered.wait(), 2)
        actor.cancel()
        await asyncio.sleep(0)
        self.assertFalse(actor.done())
        if repeat:
            actor.cancel()
            await asyncio.sleep(0)
            self.assertFalse(actor.done())
        release.set()
        expected = RuntimeError if body_error else asyncio.CancelledError
        with self.assertRaises(expected) as caught:
            await actor
        if body_error:
            self.assertIs(caught.exception, original)
        self.assertEqual(completed, [True])
        self.main.aclose_rerank_client.assert_awaited_once()
        self.main.aclose_vector_store.assert_awaited_once()
        self.main.async_engine.dispose.assert_awaited_once()
        self.main.close_document_processing.assert_called_once()
        if fail:
            self.assertEqual(len(self.logs), 1)
            self.assertNotEqual(self.logs[0][0], self.loop_thread)

    async def test_task_cancel_waits_for_native_shutdown_to_finish(self):
        await self.cancel_during_shutdown()

    async def test_repeated_cancel_during_failed_shutdown_preserves_cancellation(self):
        await self.cancel_during_shutdown(repeat=True, fail=True)

    async def test_body_failure_survives_cancellation_during_shutdown(self):
        await self.cancel_during_shutdown(body_error=True)

    async def cancel_during_startup(self, fail=False, repeat=False):
        entered, release = asyncio.Event(), asyncio.Event()
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda current_loop, context: errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)
        original = self.main.initialize_app

        async def initialize():
            entered.set()
            await release.wait()
            if fail:
                self.main.setup_checkpointer.side_effect = RuntimeError("startup failed while cancelled")
            await original()

        async def run():
            async with self.main.lifespan(self.main.app):
                self.fail("Cancelled startup became ready")

        with patch.object(self.main, "initialize_app", initialize):
            actor = asyncio.create_task(run())
            await entered.wait()
            actor.cancel()
            await asyncio.sleep(0)
            self.assertFalse(actor.done())
            if repeat:
                actor.cancel()
                await asyncio.sleep(0)
                self.assertFalse(actor.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await actor
        await asyncio.sleep(0)
        self.assertEqual(errors, [])
        self.main.aclose_vector_store.assert_awaited_once()
        self.main.close_checkpointer.assert_called_once()
        self.main.close_document_processing.assert_called_once()

    async def test_task_cancel_waits_for_startup_then_closes_resources(self):
        await self.cancel_during_startup()

    async def test_repeated_cancel_cannot_skip_startup_cleanup(self):
        await self.cancel_during_startup(repeat=True)

    async def test_cancelled_failed_startup_cleans_up_once_without_future_error_logs(self):
        await self.cancel_during_startup(fail=True)
        self.assertEqual(len(self.logs), 1)
        self.assertNotEqual(self.logs[0][0], self.loop_thread)

    async def test_error_handler_and_outer_cors_are_preserved(self):
        self.assertIs(self.main.app.exception_handlers[HTTPException], http_exception_handler)
        self.assertIs(self.main.app.user_middleware[0].cls, CORSMiddleware)
        self.assertIs(self.main.app.user_middleware[1].cls, APIErrorMiddleware)


if __name__ == "__main__":
    unittest.main()
