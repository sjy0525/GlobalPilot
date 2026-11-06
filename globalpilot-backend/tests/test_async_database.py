"""Validate native async configuration and Session ownership without connecting."""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.util import await_only


class AsyncDatabaseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        config = types.ModuleType("app.core.config")
        config.settings = types.SimpleNamespace(database_url="postgresql+psycopg://unused")
        path = Path(__file__).resolve().parents[1] / "app/db/database.py"
        spec = importlib.util.spec_from_file_location("isolated_async_database", path)
        cls.database = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"app.core.config": config}):
            spec.loader.exec_module(cls.database)

    async def test_async_engine_uses_native_psycopg_driver_and_no_expiration(self):
        self.assertTrue(self.database.async_engine.dialect.is_async)
        self.assertEqual(self.database.async_engine.dialect.driver, "psycopg")
        self.assertFalse(self.database.async_sessionLocal.kw["expire_on_commit"])
        self.assertIsInstance(self.database.async_sessionLocal(), AsyncSession)

    async def test_url_conversion_keeps_connection_options(self):
        url = self.database.async_database_url("postgresql+psycopg2://user:secret@localhost:5434/database?sslmode=require")
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual((url.username, url.password, url.host, url.port, url.database), ("user", "secret", "localhost", 5434, "database"))
        self.assertEqual(url.query["sslmode"], "require")

    async def test_each_request_has_a_separate_session_closed_on_success_or_error(self):
        sessions = []

        class Session:
            def __init__(self):
                self.closed = False
                sessions.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                self.closed = True

        with patch.object(self.database, "async_sessionLocal", Session):
            first, second = self.database.get_async_db(), self.database.get_async_db()
            a, b = await anext(first), await anext(second)
            self.assertIsNot(a, b)
            await first.aclose()
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                await second.athrow(RuntimeError("request failed"))
        self.assertTrue(all(session.closed for session in sessions))

    async def test_sqlalchemy_run_sync_yields_when_the_async_driver_waits(self):
        # run_sync is SQLAlchemy's greenlet bridge, not threadpool offloading.
        entered, release = asyncio.Event(), asyncio.Event()

        async def driver_wait():
            entered.set()
            await release.wait()
            return "database result"

        def legacy_orm_operation(sync_session):
            self.assertIsNotNone(sync_session)
            return await_only(driver_wait())

        async with AsyncSession() as session:
            operation = asyncio.create_task(session.run_sync(legacy_orm_operation))
            await asyncio.wait_for(entered.wait(), 2)
            self.assertFalse(operation.done())
            await asyncio.sleep(0)
            release.set()
            self.assertEqual(await operation, "database result")


if __name__ == "__main__":
    unittest.main()
