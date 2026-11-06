"""Execution ownership and optional real-PostgreSQL checkpoint fencing tests.

Set WORKFLOW_TEST_DATABASE_URL to an isolated, disposable PostgreSQL database to
also run integration tests. They never use the application's configured DB URL.
"""

import contextvars
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from typing import TypedDict

from app.modules.marketing.workflow import execution
from app.modules.marketing.workflow.checkpointer import FencedPostgresSaver


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        self.brief_id = uuid4()
        self.connection = MagicMock(closed=False, broken=False)
        self.connection.execute.return_value.fetchone.return_value = {"acquired": True}
        self.connect = patch.object(execution.psycopg, "connect", return_value=self.connection)
        self.connect.start()
        self.addCleanup(self.connect.stop)

    def scope(self):
        return execution.workflow_execution(self.brief_id, database_url="postgresql://unused")

    def test_busy_execution_returns_conflict_and_closes_physical_connection(self):
        self.connection.execute.return_value.fetchone.return_value = {"acquired": False}
        with self.assertRaises(HTTPException) as caught:
            with self.scope():
                self.fail("Busy workflow acquired a second owner")
        self.assertEqual(caught.exception.status_code, 409)
        self.connection.close.assert_called_once()
        self.assertIsNone(execution.current_workflow_execution(self.brief_id))

    def test_projection_transaction_uses_the_lock_connection(self):
        with self.scope() as guard:
            self.assertIs(execution.require_workflow_execution(self.brief_id), guard)
            with guard.transaction() as cursor:
                cursor.execute("SELECT 1")
            self.connection.transaction.assert_called_once_with()
            self.connection.cursor.assert_called_once_with(row_factory=dict_row)
        self.connection.close.assert_called_once()
        self.assertIsNone(execution.current_workflow_execution(self.brief_id))

    def test_wrong_brief_and_unowned_checkpoint_writes_are_rejected(self):
        saver = FencedPostgresSaver(Mock())
        config = {"configurable": {"thread_id": str(self.brief_id)}}
        with self.assertRaisesRegex(RuntimeError, "active workflow execution"):
            saver.put(config, empty_checkpoint(), {}, {})
        with self.assertRaisesRegex(RuntimeError, "active workflow execution"):
            saver.put_writes(config, [], "task")
        with self.scope():
            other = {"configurable": {"thread_id": str(uuid4())}}
            with self.assertRaisesRegex(RuntimeError, "does not own"):
                saver.put(other, empty_checkpoint(), {}, {})

    def test_checkpoint_writes_share_the_owned_connection_and_reentrant_lock(self):
        saver = FencedPostgresSaver(Mock())
        config = {"configurable": {"thread_id": str(self.brief_id)}}
        checkpoint = empty_checkpoint()
        bound_saver = Mock()
        with patch.object(execution, "PostgresSaver", return_value=bound_saver) as factory:
            with self.scope() as guard:
                saver.put(config, checkpoint, {}, {})
                saver.put_writes(config, [("result", "ok")], "task")
                self.assertIs(bound_saver.lock, guard._lock)
        for call in factory.call_args_list:
            self.assertIs(call.args[0], self.connection)
        bound_saver.put.assert_called_once_with(config, checkpoint, {}, {})
        bound_saver.put_writes.assert_called_once_with(config, [("result", "ok")], "task", "")

    def test_old_copied_context_is_fenced_after_scope_closes(self):
        with self.scope():
            copied = contextvars.copy_context()
        with self.assertRaises(execution.WorkflowExecutionLostError):
            copied.run(execution.require_workflow_execution, self.brief_id)

    def test_execution_error_is_preserved_when_connection_cleanup_also_fails(self):
        original = RuntimeError("model failed")
        self.connection.close.side_effect = RuntimeError("close failed")
        with self.assertLogs(execution.logger, level="ERROR"):
            with self.assertRaises(RuntimeError) as caught:
                with self.scope():
                    raise original
        self.assertIs(caught.exception, original)
        self.assertIsNone(execution.current_workflow_execution(self.brief_id))

    def test_broken_connection_blocks_checkpoint_and_projection_writes(self):
        with self.scope() as guard:
            self.connection.broken = True
            with self.assertRaises(execution.WorkflowExecutionLostError):
                with guard.transaction():
                    self.fail("Projection was allowed through a broken owner session")
            with self.assertRaises(execution.WorkflowExecutionLostError):
                with guard.checkpoint_saver(Mock()):
                    self.fail("Checkpoint was allowed through a broken owner session")

    def test_stream_guard_survives_fresh_worker_contexts_and_close(self):
        def stream():
            with self.scope() as guard:
                yield execution.require_workflow_execution(self.brief_id) is guard
                yield execution.require_workflow_execution(self.brief_id) is guard

        iterator = execution.preserve_execution_context(stream())
        with ThreadPoolExecutor(max_workers=2) as workers:
            for _ in range(2):
                self.assertTrue(workers.submit(contextvars.Context().run, next, iterator).result(timeout=2))
            workers.submit(contextvars.Context().run, iterator.close).result(timeout=2)
        self.connection.close.assert_called_once()

    def test_key_is_stable_across_equivalent_uuid_spellings(self):
        self.assertEqual(
            execution.workflow_lock_key(self.brief_id),
            execution.workflow_lock_key(str(self.brief_id).upper()),
        )
        self.assertNotEqual(execution.workflow_lock_key(self.brief_id), execution.workflow_lock_key(uuid4()))


DATABASE_URL = os.environ.get("WORKFLOW_TEST_DATABASE_URL")


class CounterState(TypedDict, total=False):
    count: int
    decision: str


@unittest.skipUnless(DATABASE_URL, "WORKFLOW_TEST_DATABASE_URL is not set")
class WorkflowPostgresFencingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conninfo = execution.to_psycopg_conninfo(DATABASE_URL)
        cls.pool = ConnectionPool(
            cls.conninfo, min_size=1, max_size=4, open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        cls.saver = FencedPostgresSaver(cls.pool)
        cls.saver.setup()
        with psycopg.connect(cls.conninfo, autocommit=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflow_execution_test_projection "
                "(brief_id uuid PRIMARY KEY, value integer NOT NULL)"
            )

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def setUp(self):
        self.brief_id = uuid4()
        self.config = {"configurable": {"thread_id": str(self.brief_id)}}

    def scope(self):
        return execution.workflow_execution(self.brief_id, database_url=DATABASE_URL)

    def graph(self, node):
        builder = StateGraph(CounterState)
        builder.add_node("work", node)
        builder.add_edge(START, "work")
        builder.add_edge("work", END)
        return builder.compile(checkpointer=self.saver)

    def test_same_brief_is_excluded_and_release_allows_next_owner(self):
        with self.scope():
            def contend():
                with self.scope():
                    return "incorrect second owner"

            with ThreadPoolExecutor(max_workers=1) as workers:
                with self.assertRaises(HTTPException) as caught:
                    workers.submit(contend).result(timeout=5)
            self.assertEqual(caught.exception.status_code, 409)
        with self.scope() as guard:
            with guard.transaction() as cursor:
                cursor.execute("SELECT 1 AS value")
                self.assertEqual(cursor.fetchone()["value"], 1)

    def test_projection_transaction_rolls_back_before_releasing_ownership(self):
        with self.assertRaisesRegex(RuntimeError, "abort projection"):
            with self.scope() as guard:
                with guard.transaction() as cursor:
                    cursor.execute(
                        "INSERT INTO workflow_execution_test_projection VALUES (%s, %s)",
                        (self.brief_id, 1),
                    )
                    raise RuntimeError("abort projection")
        with self.scope() as guard:
            with guard.transaction() as cursor:
                cursor.execute(
                    "SELECT value FROM workflow_execution_test_projection WHERE brief_id = %s", (self.brief_id,)
                )
                self.assertIsNone(cursor.fetchone())

    def test_real_graph_background_writes_receive_guard_and_avoid_pool(self):
        main_thread = threading.get_ident()
        checkpoint_threads = []
        original_put = self.saver.put

        def track_put(*args, **kwargs):
            checkpoint_threads.append(threading.get_ident())
            return original_put(*args, **kwargs)

        def node(state):
            execution.require_workflow_execution(self.brief_id)
            return {"count": state.get("count", 0) + 1}

        graph = self.graph(node)
        with self.scope():
            with patch.object(self.saver, "put", side_effect=track_put), \
                 patch.object(self.pool, "connection", side_effect=AssertionError("Owned operations reached the read pool")):
                graph.invoke({"count": 0}, self.config)
                self.assertEqual(graph.get_state(self.config).values["count"], 1)
        self.assertTrue(checkpoint_threads)
        self.assertTrue(any(thread != main_thread for thread in checkpoint_threads))
        self.assertEqual(graph.get_state(self.config).values["count"], 1)

    def test_real_graph_interrupt_resume_and_stream_context(self):
        def review_node(state):
            decision = interrupt({"count": state.get("count", 0)})
            return {"count": state.get("count", 0) + 1, "decision": decision}

        graph = self.graph(review_node)

        def stream():
            with self.scope():
                yield from graph.stream({"count": 0}, self.config, stream_mode="updates")

        iterator = execution.preserve_execution_context(stream())
        with ThreadPoolExecutor(max_workers=2) as workers:
            while True:
                try:
                    workers.submit(contextvars.Context().run, next, iterator).result(timeout=5)
                except StopIteration:
                    break
        self.assertTrue(graph.get_state(self.config).interrupts)
        with self.scope():
            graph.invoke(Command(resume="approved"), self.config)
        self.assertEqual(graph.get_state(self.config).values["count"], 1)

    def test_terminated_owner_cannot_write_after_new_owner_acquires_lock(self):
        graph = self.graph(lambda state: {"count": state.get("count", 0) + 1})
        with self.scope() as old_guard:
            graph.invoke({"count": 0}, self.config)
            old_snapshot = graph.get_state(self.config)
            old_config = old_snapshot.config
            old_context = contextvars.copy_context()
            backend_pid = old_guard.connection.info.backend_pid
            with psycopg.connect(self.conninfo, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))

            def new_owner():
                with self.scope() as guard:
                    graph.invoke({"count": 10}, self.config)
                    with guard.transaction() as cursor:
                        cursor.execute(
                            "INSERT INTO workflow_execution_test_projection VALUES (%s, %s)",
                            (self.brief_id, 11),
                        )

            with ThreadPoolExecutor(max_workers=1) as workers:
                workers.submit(new_owner).result(timeout=10)

            with self.assertRaises((psycopg.Error, execution.WorkflowExecutionLostError)):
                old_context.run(self.saver.put_writes, old_config, [("count", -1)], "stale-task")
            with self.assertRaises((psycopg.Error, execution.WorkflowExecutionLostError)):
                with old_guard.transaction() as cursor:
                    cursor.execute(
                        "UPDATE workflow_execution_test_projection SET value = -1 WHERE brief_id = %s",
                        (self.brief_id,),
                    )
        self.assertEqual(graph.get_state(self.config).values["count"], 11)
        with psycopg.connect(self.conninfo) as check:
            row = check.execute(
                "SELECT value FROM workflow_execution_test_projection WHERE brief_id = %s", (self.brief_id,)
            ).fetchone()
        self.assertEqual(row[0], 11)


if __name__ == "__main__":
    unittest.main()
