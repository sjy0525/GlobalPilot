"""Opt-in PostgreSQL acceptance tests for transactional memory invalidation.

Enable with GLOBALPILOT_MEMORY_PG_TESTS=1 against an initialized Store schema.
Only random test namespaces are written/deleted; default discovery loads no
application settings and makes no network calls.
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
class MemoryInvalidationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = importlib.import_module("app.modules.memory.schemas.brand_memory")
        cls.storage = importlib.import_module("app.modules.memory.store")
        cls.service = importlib.import_module("app.modules.memory.services.brand_memory")

        embeddings = cls.storage.embeddings
        # Existing OpenAI resources retain retry configuration; change their
        # clients only in this test process, then restore both attributes.
        for name in ("client", "async_client"):
            resource = getattr(embeddings, name)
            limited = resource._client.with_options(
                timeout=30.0, max_retries=0,
            ).embeddings
            resource_patch = patch.object(embeddings, name, limited)
            resource_patch.start()
            cls.addClassCleanup(resource_patch.stop)

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
                brief_id=uuid4(), checkpoint_id="pg-invalidation-fixture",
                quote="以后文案保持简洁直接。",
            )],
        )

        def clear_fixture():
            with cls.open_store() as store:
                store.delete(
                    cls.storage.memory_namespace(cls.fixture_scope), str(cls.fixture.id),
                )

        cls.addClassCleanup(clear_fixture)
        # One real embedding supplies authentic vectors for all cloned seeds.
        # Invalidation itself must never call the embedding provider.
        with cls.open_store() as store:
            cls.storage.save_memory(store, cls.fixture_scope, cls.fixture)

    def setUp(self):
        self.namespaces = set()
        self.addCleanup(self.clear_namespaces)
        self.scope = self.schema.MemoryScope(team_id=self.scope.team_id,
            user_id=uuid4(), brand_profile_id=uuid4(),
        )
        self.source = self.schema.MemorySource(
            brief_id=uuid4(), checkpoint_id="pg-invalidation-feedback",
            quote="以后不用保留这个品牌的简洁表达要求。",
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
                    "DELETE FROM store WHERE prefix = %s", (self.prefix(namespace),),
                )

    def seed(self, **overrides):
        memory = self.schema.BrandMemory(**{
            **self.fixture.model_dump(), "id": uuid4(), **overrides,
        })
        self.track(self.scope, memory.id)
        namespace = self.storage.memory_namespace(self.scope)
        with self.open_store() as store:
            with store.conn.transaction():
                store.put(namespace, str(memory.id), memory.model_dump(mode="json"), index=False)
                store.conn.execute(
                    """INSERT INTO store_vectors (prefix, key, field_name, embedding)
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
        return self.schema.InvalidateMemoryArgs(**{
            "memory_id": memory.id,
            "expected_version": memory.version,
            "reason": "用户已撤回品牌文案必须简洁的长期偏好。",
            **overrides,
        })

    def read(self, memory=None, store=None):
        memory = memory or self.old
        if store is not None:
            return self.storage.get_memory(store, self.scope, memory.id)
        with self.open_store() as opened:
            return self.storage.get_memory(opened, self.scope, memory.id)

    def history(self, memory=None, version=1, store=None):
        namespace = self.service.history_namespace(self.scope, (memory or self.old).id)
        if store is not None:
            return store.get(namespace, str(version))
        with self.open_store() as opened:
            return opened.get(namespace, str(version))

    def snapshot(self, memory=None, scope=None, store=None):
        """Read body, history, vectors and row timestamps for exact rollback checks."""
        memory = memory or self.old
        scope = scope or self.scope
        namespace = self.prefix(self.storage.memory_namespace(scope))
        history_ns = self.prefix(self.service.history_namespace(scope, memory.id))

        def read_rows(opened):
            values = opened.conn.execute(
                """SELECT prefix, key, value, created_at, updated_at
                   FROM store WHERE (prefix = %s AND key = %s) OR prefix = %s
                   ORDER BY prefix, key""",
                (namespace, str(memory.id), history_ns),
            ).fetchall()
            vectors = opened.conn.execute(
                """SELECT prefix, key, field_name, embedding::text AS embedding,
                          created_at, updated_at
                   FROM store_vectors
                   WHERE (prefix = %s AND key = %s) OR prefix = %s
                   ORDER BY prefix, key, field_name""",
                (namespace, str(memory.id), history_ns),
            ).fetchall()
            return {"values": values, "vectors": vectors}

        if store is not None:
            return read_rows(store)
        with self.open_store() as opened:
            return read_rows(opened)

    @contextmanager
    def forbid_embeddings(self):
        embedding_type = type(self.storage.embeddings)
        with patch.object(
            embedding_type, "embed_documents",
            side_effect=AssertionError("Invalidation must not embed documents"),
        ) as documents, patch.object(
            embedding_type, "embed_query",
            side_effect=AssertionError("Invalidation must not embed queries"),
        ) as queries:
            yield
            documents.assert_not_called()
            queries.assert_not_called()

    def test_invalidation_preserves_content_sources_reason_and_vector_but_search_excludes_it(self):
        before = self.snapshot()
        self.assertTrue(before["vectors"], "The fixture must have a real vector")
        args = self.args()
        with self.forbid_embeddings():
            invalidated = self.service.invalidate_brand_memory(self.scope, self.source, args)
        self.assertEqual(self.read(), invalidated)
        self.assertEqual(invalidated.id, self.old.id)
        self.assertEqual(invalidated.content, self.old.content)
        self.assertEqual(invalidated.create_at, self.old.create_at)
        self.assertEqual(invalidated.regions, self.old.regions)
        self.assertEqual(invalidated.platforms, self.old.platforms)
        self.assertEqual(invalidated.duration, self.old.duration)
        self.assertEqual(invalidated.status, "inactive")
        self.assertEqual(invalidated.version, self.old.version + 1)
        self.assertGreaterEqual(invalidated.update_at, self.old.update_at)
        self.assertEqual(invalidated.sources, [*self.old.sources, self.source])
        self.assertEqual(invalidated.invalidation_reason, args.reason)
        self.assertEqual(self.history().value, self.old.model_dump(mode="json"))
        self.assertEqual(self.snapshot()["vectors"], before["vectors"])
        # Restore the real provider before exercising semantic search.
        with self.open_store() as store:
            self.assertEqual(self.storage.search_memories(store, self.scope, self.old.content), [])

    def test_existing_feedback_source_is_not_duplicated(self):
        old = self.seed(sources=[*self.old.sources, self.source])
        with self.forbid_embeddings():
            invalidated = self.service.invalidate_brand_memory(self.scope, self.source, self.args(old))
        self.assertEqual(invalidated.sources, old.sources)
        self.assertEqual(self.history(old).value, old.model_dump(mode="json"))

    def test_rejections_leave_all_current_history_and_vector_rows_unchanged(self):
        inactive = self.seed(status="inactive", invalidation_reason="Earlier withdrawal")
        task_only = self.seed(duration="task_only")
        missing = self.old.model_copy(update={"id": uuid4()})
        collision = self.seed()
        with self.open_store() as store:
            store.put(
                self.service.history_namespace(self.scope, collision.id), "1",
                collision.model_dump(mode="json"), index=False,
            )

        wrong_user = self.schema.MemoryScope(team_id=self.scope.team_id,
            user_id=uuid4(), brand_profile_id=self.scope.brand_profile_id,
        )
        wrong_brand = self.schema.MemoryScope(team_id=self.scope.team_id,
            user_id=self.scope.user_id, brand_profile_id=uuid4(),
        )
        cases = [
            ("version_conflict", self.scope, self.old, self.source, {"expected_version": 2}),
            ("not_found", wrong_user, self.old, self.source, {}),
            ("not_found", wrong_brand, self.old, self.source, {}),
            ("not_found", self.scope, missing, self.source, {}),
            ("not_editable", self.scope, inactive, self.source, {}),
            ("not_editable", self.scope, task_only, self.source, {}),
            ("invalid_source", self.scope, self.old,
             self.source.model_copy(update={"quote": " \n\t "}), {}),
            ("invalid_source", self.scope, self.old,
             self.source.model_copy(update={"checkpoint_id": " \n\t "}), {}),
            ("history_conflict", self.scope, collision, self.source, {}),
        ]
        for code, scope, memory, source, overrides in cases:
            self.track(scope, memory.id)
            self.track(self.scope, memory.id)
            with self.subTest(code=code, memory_id=memory.id):
                before = self.snapshot(memory, scope)
                original_scope_before = self.snapshot(memory)
                with self.forbid_embeddings():
                    with self.assertRaises(self.service.MemoryUpdateError) as raised:
                        self.service.invalidate_brand_memory(
                            scope, source, self.args(memory, **overrides),
                        )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.snapshot(memory, scope), before)
                self.assertEqual(self.snapshot(memory), original_scope_before)

    def race(self, operations):
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

        def run(operation):
            try:
                return "ok", operation()
            except self.service.MemoryUpdateError as exc:
                return exc.code, None

        with patch.object(self.service, "open_memory_store", concurrent_open):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run, operation) for operation in operations]
                results = [future.result(timeout=60) for future in futures]
        self.assertEqual(len(set(connection_ids)), 2)
        return results

    def assert_single_winner(self, results):
        self.assertEqual(sum(code == "ok" for code, _ in results), 1)
        winner = next(value for code, value in results if code == "ok")
        self.assertEqual(winner.version, self.old.version + 1)
        self.assertEqual(self.read(), winner)
        self.assertEqual(self.history().value, self.old.model_dump(mode="json"))
        self.assertIsNone(self.history(version=2))

    def test_two_invalidations_on_physical_connections_have_one_winner_and_one_conflict(self):
        operation = lambda: self.service.invalidate_brand_memory(
            self.scope, self.source, self.args(),
        )
        before = self.snapshot()["vectors"]
        with self.forbid_embeddings():
            results = self.race([operation, operation])
        self.assertCountEqual([code for code, _ in results], ["ok", "version_conflict"])
        self.assert_single_winner(results)
        self.assertEqual(self.read().status, "inactive")
        self.assertEqual(self.snapshot()["vectors"], before)

    def test_update_and_invalidation_share_the_same_lock_and_only_one_can_commit(self):
        update_args = self.schema.UpdateMemoryArgs(
            memory_id=self.old.id, expected_version=self.old.version,
            content="品牌文案长期保持简洁克制，避免绝对化措辞。",
        )
        operations = [
            lambda: self.service.update_brand_memory(self.scope, self.source, update_args),
            lambda: self.service.invalidate_brand_memory(self.scope, self.source, self.args()),
        ]
        results = self.race(operations)
        self.assert_single_winner(results)
        loser_code = next(code for code, _ in results if code != "ok")
        # Existing update checks active status before version. If invalidation
        # wins, that update may report not_editable instead of version_conflict.
        self.assertIn(loser_code, {"version_conflict", "not_editable"})

    def assert_failure_rolls_back(self, fail_after_history):
        before = self.snapshot()
        original_open = self.open_store
        history_ns = self.service.history_namespace(self.scope, self.old.id)
        current_ns = self.storage.memory_namespace(self.scope)
        target_namespace = history_ns if fail_after_history else current_ns
        reached_injection = []

        @contextmanager
        def failing_open():
            with original_open() as store:
                original_put = store.put

                def write_then_fail(namespace, key, value, **kwargs):
                    result = original_put(namespace, key, value, **kwargs)
                    if namespace == target_namespace:
                        reached_injection.append(True)
                        self.assertIsNotNone(self.history(store=store))
                        if not fail_after_history:
                            self.assertEqual(self.read(store=store).status, "inactive")
                            self.assertEqual(self.read(store=store).version, 2)
                            self.assertEqual(self.snapshot(store=store)["vectors"], before["vectors"])
                        raise RuntimeError("injected invalidation transaction failure")
                    return result

                with patch.object(store, "put", side_effect=write_then_fail):
                    yield store

        with self.forbid_embeddings():
            with patch.object(self.service, "open_memory_store", failing_open):
                with self.assertRaisesRegex(RuntimeError, "injected invalidation transaction failure"):
                    self.service.invalidate_brand_memory(self.scope, self.source, self.args())
        self.assertEqual(reached_injection, [True])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.read(), self.old)
        self.assertIsNone(self.history())

    def test_error_after_history_insert_rolls_back_archive_and_preserves_current(self):
        self.assert_failure_rolls_back(fail_after_history=True)

    def test_error_after_current_write_rolls_back_body_archive_and_vector_state(self):
        self.assert_failure_rolls_back(fail_after_history=False)


if __name__ == "__main__":
    unittest.main()
