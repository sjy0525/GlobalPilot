"""Opt-in PostgreSQL/real-Embeddings acceptance tests for memory updates.

Run explicitly with GLOBALPILOT_MEMORY_PG_TESTS=1. The existing Store schema
must already be initialized. Only random test namespaces are written/deleted.
Default discovery imports no application settings and makes no network calls.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib
import os
from threading import Barrier, Lock
import unittest
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(
    os.getenv("GLOBALPILOT_MEMORY_PG_TESTS") == "1",
    "Set GLOBALPILOT_MEMORY_PG_TESTS=1 to use PostgreSQL and real Embeddings",
)
class MemoryUpdatePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Imports are intentionally inside this opt-in lifecycle method.
        cls.schema = importlib.import_module("app.modules.memory.schemas.brand_memory")
        cls.storage = importlib.import_module("app.modules.memory.store")
        cls.service = importlib.import_module("app.modules.memory.services.brand_memory")

        embeddings = cls.storage.embeddings
        # Adjust the real provider's existing clients only in this test process.
        # Changing the Pydantic max_retries field alone does not reconfigure them.
        clients = {
            "client": embeddings.client._client.with_options(
                timeout=30.0, max_retries=0,
            ).embeddings,
            "async_client": embeddings.async_client._client.with_options(
                timeout=30.0, max_retries=0,
            ).embeddings,
        }
        for name, client in clients.items():
            client_patch = patch.object(embeddings, name, client)
            client_patch.start()
            cls.addClassCleanup(client_patch.stop)

        original_open = cls.storage.open_memory_store

        @contextmanager
        def timed_open():
            with original_open() as store:
                store.conn.execute("SET statement_timeout = '30s'")
                store.conn.execute("SET lock_timeout = '30s'")
                yield store

        cls.open_store = staticmethod(timed_open)
        open_patch = patch.object(cls.service, "open_memory_store", timed_open)
        open_patch.start()
        cls.addClassCleanup(open_patch.stop)

        cls.fixture_scope = cls.schema.MemoryScope(team_id=self.scope.team_id,
            user_id=uuid4(), brand_profile_id=uuid4(),
        )
        cls.fixture = cls.schema.BrandMemory(
            content="品牌文案长期保持简洁直接，避免夸张。",
            regions="eu-na",
            platforms=["Instagram"],
            sources=[cls.schema.MemorySource(
                brief_id=uuid4(), checkpoint_id="pg-fixture", quote="以后文案简洁直接。",
            )],
        )
        fixture_ns = cls.storage.memory_namespace(cls.fixture_scope)

        def clear_fixture():
            with cls.open_store() as store:
                store.delete(fixture_ns, str(cls.fixture.id))

        cls.addClassCleanup(clear_fixture)
        # One real embedding seeds all fixtures. Per-test clones retain this
        # authentic vector; every successful update embeds its new content.
        with cls.open_store() as store:
            cls.storage.save_memory(store, cls.fixture_scope, cls.fixture)

    def setUp(self):
        self.namespaces = set()
        self.addCleanup(self.clear_namespaces)
        self.scope = self.schema.MemoryScope(team_id=self.scope.team_id,
            user_id=uuid4(), brand_profile_id=uuid4(),
        )
        self.source = self.schema.MemorySource(
            brief_id=uuid4(), checkpoint_id="pg-new-feedback",
            quote="以后保持简洁克制，避免绝对化措辞。",
        )
        self.old = self.seed()

    @staticmethod
    def prefix(namespace):
        # LangGraph PostgresStore persists a tuple namespace as dotted text.
        return ".".join(namespace)

    def track(self, scope, memory_id):
        self.namespaces.add(self.storage.memory_namespace(scope))
        self.namespaces.add(self.service.history_namespace(scope, memory_id))

    def clear_namespaces(self):
        with self.open_store() as store:
            for namespace in self.namespaces:
                # Exact random namespace only; FK cascade clears its vectors.
                store.conn.execute(
                    "DELETE FROM store WHERE prefix = %s",
                    (self.prefix(namespace),),
                )

    def seed(self, **overrides):
        memory = self.schema.BrandMemory(**{
            **self.fixture.model_dump(),
            "id": uuid4(),
            **overrides,
        })
        self.track(self.scope, memory.id)
        namespace = self.storage.memory_namespace(self.scope)
        with self.open_store() as store:
            with store.conn.transaction():
                # Cloning the one real fixture avoids redundant provider calls.
                store.put(namespace, str(memory.id), memory.model_dump(mode="json"), index=False)
                store.conn.execute(
                    """INSERT INTO store_vectors
                           (prefix, key, field_name, embedding)
                       SELECT %s, %s, field_name, embedding FROM store_vectors
                       WHERE prefix = %s AND key = %s""",
                    (
                        self.prefix(namespace), str(memory.id),
                        self.prefix(self.storage.memory_namespace(self.fixture_scope)),
                        str(self.fixture.id),
                    ),
                )
        return memory

    def args(self, memory=None, **overrides):
        memory = memory or self.old
        return self.schema.UpdateMemoryArgs(**{
            "memory_id": memory.id,
            "expected_version": memory.version,
            "content": "品牌文案长期保持简洁克制，避免绝对化措辞。",
            **overrides,
        })

    def read(self, memory=None):
        with self.open_store() as store:
            return self.storage.get_memory(store, self.scope, (memory or self.old).id)

    def history(self, memory=None, version=1, scope=None):
        with self.open_store() as store:
            return store.get(
                self.service.history_namespace(scope or self.scope, (memory or self.old).id),
                str(version),
            )

    def vector_snapshot(self, store=None):
        def read_vector(opened):
            return opened.conn.execute(
                """SELECT field_name, embedding::text AS embedding
                   FROM store_vectors WHERE prefix = %s AND key = %s
                   ORDER BY field_name""",
                (self.prefix(self.storage.memory_namespace(self.scope)), str(self.old.id)),
            ).fetchall()

        if store is not None:
            return read_vector(store)
        with self.open_store() as opened:
            return read_vector(opened)

    def test_update_preserves_identity_archives_complete_versions_and_deduplicates_sources(self):
        updated = self.service.update_brand_memory(self.scope, self.source, self.args())
        self.assertEqual(self.read(), updated)
        self.assertEqual(updated.id, self.old.id)
        self.assertEqual(updated.create_at, self.old.create_at)
        self.assertGreaterEqual(updated.update_at, self.old.update_at)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.regions, self.old.regions)
        self.assertEqual(updated.platforms, self.old.platforms)
        self.assertEqual(updated.sources, [*self.old.sources, self.source])
        self.assertEqual(self.history().value, self.old.model_dump(mode="json"))

        cleared = self.service.update_brand_memory(
            self.scope, self.source, self.args(updated, platforms=[]),
        )
        self.assertEqual(cleared.platforms, [])
        self.assertEqual(cleared.version, 3)
        self.assertEqual(cleared.sources, updated.sources)
        self.assertEqual(self.history(version=2).value, updated.model_dump(mode="json"))

        with self.open_store() as store:
            # The current semantic search never returns its archived versions.
            matches = self.storage.search_memories(store, self.scope, cleared.content)
            self.assertEqual([(m.id, m.version) for m in matches], [(cleared.id, 3)])
            self.assertTrue(self.vector_snapshot(store))
            history_vector_count = store.conn.execute(
                "SELECT count(*) AS count FROM store_vectors WHERE prefix = %s",
                (self.prefix(self.service.history_namespace(self.scope, self.old.id)),),
            ).fetchone()["count"]
            self.assertEqual(history_vector_count, 0)

    def test_rejected_updates_do_not_change_current_or_create_history(self):
        cases = [
            ("version_conflict", self.scope, self.old, {"expected_version": 2}),
            ("not_found", self.schema.MemoryScope(team_id=self.scope.team_id,
                user_id=uuid4(), brand_profile_id=self.scope.brand_profile_id,
            ), self.old, {}),
            ("not_found", self.schema.MemoryScope(team_id=self.scope.team_id,
                user_id=self.scope.user_id, brand_profile_id=uuid4(),
            ), self.old, {}),
            ("not_editable", self.scope, self.seed(status="inactive"), {}),
            ("not_editable", self.scope, self.seed(duration="task_only"), {}),
        ]
        for code, scope, memory, overrides in cases:
            self.track(scope, memory.id)
            with self.subTest(code=code, memory_id=memory.id):
                with self.assertRaises(self.service.MemoryUpdateError) as raised:
                    self.service.update_brand_memory(scope, self.source, self.args(memory, **overrides))
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.read(memory), memory)
                self.assertIsNone(self.history(memory, scope=scope))
                self.assertIsNone(self.history(memory))

    def test_two_physical_connections_with_same_version_have_exactly_one_winner(self):
        rendezvous = Barrier(2, timeout=15)
        connection_ids = []
        ids_lock = Lock()
        original_open = self.open_store

        @contextmanager
        def concurrent_open():
            with original_open() as store:
                with ids_lock:
                    connection_ids.append(store.conn.info.backend_pid)
                rendezvous.wait()
                yield store

        def update(content):
            try:
                value = self.service.update_brand_memory(
                    self.scope, self.source, self.args(content=content),
                )
                return "ok", value
            except self.service.MemoryUpdateError as exc:
                return exc.code, None

        with patch.object(self.service, "open_memory_store", concurrent_open):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(update, content) for content in (
                    "以后品牌文案简洁克制，避免绝对化措辞。",
                    "以后品牌文案保持简洁平实，避免夸张表达。",
                )]
                results = [future.result(timeout=60) for future in futures]

        self.assertEqual(len(set(connection_ids)), 2)
        self.assertCountEqual([status for status, _ in results], ["ok", "version_conflict"])
        winner = next(value for status, value in results if status == "ok")
        self.assertEqual(self.read(), winner)
        self.assertEqual(winner.version, 2)
        self.assertEqual(self.history().value, self.old.model_dump(mode="json"))
        self.assertIsNone(self.history(version=2))

    def test_error_after_history_write_rolls_back_archive(self):
        def fail_before_current(store, scope, memory):
            archive = store.get(self.service.history_namespace(scope, memory.id), "1")
            self.assertIsNotNone(archive)
            self.assertEqual(archive.value, self.old.model_dump(mode="json"))
            raise RuntimeError("injected failure after archive")

        before = self.vector_snapshot()
        with patch.object(self.service, "save_memory", side_effect=fail_before_current):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.service.update_brand_memory(self.scope, self.source, self.args())
        self.assertEqual(self.read(), self.old)
        self.assertIsNone(self.history())
        self.assertEqual(self.vector_snapshot(), before)

    def test_error_after_current_and_vector_write_rolls_back_everything(self):
        real_save = self.service.save_memory
        before = self.vector_snapshot()

        def save_then_fail(store, scope, memory):
            real_save(store, scope, memory)
            self.assertEqual(self.storage.get_memory(store, scope, memory.id), memory)
            self.assertNotEqual(self.vector_snapshot(store), before)
            self.assertIsNotNone(store.get(self.service.history_namespace(scope, memory.id), "1"))
            raise RuntimeError("injected failure after current and vector")

        with patch.object(self.service, "save_memory", side_effect=save_then_fail):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.service.update_brand_memory(self.scope, self.source, self.args())
        self.assertEqual(self.read(), self.old)
        self.assertIsNone(self.history())
        self.assertEqual(self.vector_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
