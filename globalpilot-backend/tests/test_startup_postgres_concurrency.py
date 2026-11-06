"""Real multi-process cold startup against newly created disposable databases.

Set STARTUP_TEST_ADMIN_URL to a disposable PostgreSQL cluster with CREATEDB and
pgvector available. Each test creates and drops only its own uniquely named DB.
The application's configured DATABASE_URL is never used by these tests.
"""

import asyncio
import multiprocessing
import os
import queue
import time
import traceback
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def _startup_worker(url, single_connection_pool, barrier, results):
    # Spawned interpreters must select the disposable DB before importing app.
    os.environ["DATABASE_URL"] = url
    phases = []
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.db import database

        database.engine.echo = False
        if single_connection_pool:
            database.async_engine = create_async_engine(
                url, pool_size=1, max_overflow=0, pool_timeout=3,
            )
            database.async_sessionLocal = async_sessionmaker(
                bind=database.async_engine, autoflush=False, expire_on_commit=False,
            )

        from app import main

        conninfo = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)

        def assert_schema_locked(phase):
            with psycopg.connect(conninfo, autocommit=True) as probe:
                acquired = probe.execute("SELECT pg_try_advisory_lock(7140214001)").fetchone()[0]
                if acquired:
                    probe.execute("SELECT pg_advisory_unlock(7140214001)")
                    raise AssertionError(f"{phase} ran outside the startup schema mutex")
            phases.append(phase)

        initialize_vector = main.ainitialize_vector_store
        prepare_schema = main._prepare_schema
        setup_checkpointer = main.setup_checkpointer
        ensure_query_indexes = main.ensure_query_indexes

        async def vector():
            assert_schema_locked("vector")
            return await initialize_vector()

        def schema(*args, **kwargs):
            assert_schema_locked("application")
            return prepare_schema(*args, **kwargs)

        def checkpoint():
            assert_schema_locked("checkpoint")
            return setup_checkpointer()

        def indexes(*args, **kwargs):
            assert_schema_locked("query_indexes")
            return ensure_query_indexes(*args, **kwargs)

        main.ainitialize_vector_store = vector
        main._prepare_schema = schema
        main.setup_checkpointer = checkpoint
        main.ensure_query_indexes = indexes
        barrier.wait(timeout=30)

        async def run():
            initialized = False
            try:
                await main.initialize_app()
                initialized = True
            finally:
                # initialize_app already cleans up its own failed startup.
                if initialized:
                    await main.shutdown_app()

        asyncio.run(run())
        results.put({"ok": True, "phases": phases})
    except BaseException:
        results.put({"ok": False, "error": traceback.format_exc()})


@unittest.skipUnless(os.getenv("STARTUP_TEST_ADMIN_URL"), "requires disposable STARTUP_TEST_ADMIN_URL")
class StartupPostgresConcurrencyTests(unittest.TestCase):
    def setUp(self):
        admin = make_url(os.environ["STARTUP_TEST_ADMIN_URL"]).set(drivername="postgresql")
        self.admin_conninfo = admin.render_as_string(hide_password=False)
        self.database_name = f"refactor_startup_test_{uuid4().hex}"
        with psycopg.connect(self.admin_conninfo, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database_name)))
        self.addCleanup(self.drop_database)
        self.conninfo = admin.set(database=self.database_name).render_as_string(hide_password=False)
        self.url = admin.set(drivername="postgresql+psycopg", database=self.database_name).render_as_string(hide_password=False)

    def drop_database(self):
        with psycopg.connect(self.admin_conninfo, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.database_name)))

    def assert_parallel_cold_start(self, *, single_connection_pool):
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(4), context.Queue()
        processes = [context.Process(target=_startup_worker, args=(
            self.url, single_connection_pool, barrier, results,
        )) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            deadline = time.monotonic() + 60
            for process in processes:
                process.join(timeout=max(0, deadline - time.monotonic()))
            self.assertTrue(all(not process.is_alive() for process in processes), "Startup deadlocked")
            output = []
            for _ in processes:
                try:
                    output.append(results.get(timeout=2))
                except queue.Empty:
                    self.fail(f"Startup worker returned no result; exitcodes={[p.exitcode for p in processes]}")
            self.assertTrue(all(result["ok"] for result in output), output)
            self.assertEqual([p.exitcode for p in processes], [0, 0, 0, 0])
            self.assertTrue(all(result["phases"] == ["vector", "application", "query_indexes", "checkpoint"] for result in output), output)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                if process.pid is not None:
                    process.join(timeout=5)
            results.close()
            results.join_thread()

        from langgraph.checkpoint.postgres import PostgresSaver
        with psycopg.connect(self.conninfo) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM checkpoint_migrations").fetchone()[0], len(PostgresSaver.MIGRATIONS))
            self.assertEqual(connection.execute("SELECT count(*) FROM langchain_pg_collection WHERE name = 'brand_knowledge'").fetchone()[0], 1)
            # A cold installation has no controlled teams yet; no global brands
            # should be created outside a team's data boundary.
            self.assertEqual(connection.execute("SELECT count(*) FROM brands").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM eval_test_sets").fetchone()[0], 1)
            self.assertEqual(connection.execute("""SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                WHERE c.relname IN ('ix_embedding_collection_brand_document','ix_briefs_user_updated_active','ix_eval_tasks_run_status')
                AND i.indisvalid""").fetchone()[0], 3)

    def test_four_workers_initialize_a_completely_empty_database(self):
        self.assert_parallel_cold_start(single_connection_pool=False)

    def test_startup_mutex_does_not_deadlock_a_single_connection_pool(self):
        self.assert_parallel_cold_start(single_connection_pool=True)


if __name__ == "__main__":
    unittest.main()
