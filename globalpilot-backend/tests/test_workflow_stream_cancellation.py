"""Cancellation drains running thread work before closing the workflow owner."""

import asyncio
import os
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from anyio import CancelScope
from starlette.requests import ClientDisconnect

from app.modules.marketing.router import WorkflowStreamingResponse, _stream_body
from app.modules.marketing.workflow import execution


class WorkflowStreamCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = MagicMock(closed=False, broken=False)
        self.connection.execute.return_value.fetchone.return_value = {"acquired": True}
        self.connect = patch.object(execution.psycopg, "connect", return_value=self.connection)
        self.connect.start()
        self.addCleanup(self.connect.stop)

    async def blocked_stream(self, *, anyio_cancel=False, repeat=False, fail=False):
        entered = asyncio.Event()
        release = threading.Event()
        closed = threading.Event()
        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)
        brief_id = uuid4()
        self.connection.close.side_effect = closed.set
        scope = None

        def source():
            with execution.workflow_execution(brief_id, database_url="postgresql://unused"):
                yield b"first"
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("Test did not release the model worker")
                if fail:
                    raise RuntimeError("model failed while cancelled")
                yield b"second"

        async def consume():
            nonlocal scope
            with CancelScope() as scope:
                async for _ in _stream_body(execution.preserve_execution_context(source())):
                    pass

        actor = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if anyio_cancel:
                scope.cancel()
            else:
                actor.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(actor.done())
            self.assertFalse(closed.is_set())
            if repeat:
                actor.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(actor.done())
            release.set()
            if anyio_cancel:
                await asyncio.wait_for(actor, 2)
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(actor, 2)
            self.assertTrue(closed.is_set())
            self.connection.close.assert_called_once()
            await asyncio.sleep(0)
            self.assertEqual(loop_errors, [])
        finally:
            release.set()
            await asyncio.gather(actor, return_exceptions=True)

    async def test_native_cancellation_waits_for_next_then_closes_owner(self):
        await self.blocked_stream()

    async def test_repeated_native_cancellation_cannot_skip_owner_cleanup(self):
        await self.blocked_stream(repeat=True)

    async def test_anyio_disconnect_cancellation_closes_owner(self):
        await self.blocked_stream(anyio_cancel=True)

    async def test_worker_failure_after_cancellation_does_not_replace_cancellation(self):
        with self.assertLogs("app.modules.marketing.router", level="ERROR"):
            await self.blocked_stream(repeat=True, fail=True)

    async def test_cancellation_during_close_drains_close_and_preserves_cancellation(self):
        entered = asyncio.Event()
        release = threading.Event()
        closed = threading.Event()
        loop = asyncio.get_running_loop()

        class Iterator:
            def __next__(self):
                raise StopIteration

            def close(self):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("Test did not release cleanup")
                closed.set()

        async def consume():
            async for _ in _stream_body(Iterator()):
                pass

        actor = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            actor.cancel()
            await asyncio.sleep(0.01)
            actor.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(actor.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(actor, 2)
            self.assertTrue(closed.is_set())
        finally:
            release.set()
            await asyncio.gather(actor, return_exceptions=True)

    def response(self):
        def source():
            with execution.workflow_execution(uuid4(), database_url="postgresql://unused"):
                yield b"first"
                yield b"second"
        return WorkflowStreamingResponse(_stream_body(execution.preserve_execution_context(source())))

    async def test_send_failure_closes_owner_while_body_is_suspended_at_yield(self):
        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("disconnected while sending")

        async def receive():
            return {"type": "http.disconnect"}

        response = self.response()
        with self.assertRaises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.connection.close.assert_called_once()

    async def test_cancelled_send_drains_owner_close_despite_repeated_cancellation(self):
        sending, closing = asyncio.Event(), asyncio.Event()
        release_close = threading.Event()
        loop = asyncio.get_running_loop()

        def close_connection():
            loop.call_soon_threadsafe(closing.set)
            if not release_close.wait(5):
                raise AssertionError("Test did not release connection cleanup")

        self.connection.close.side_effect = close_connection

        async def send(message):
            if message["type"] == "http.response.body":
                sending.set()
                await asyncio.Event().wait()

        async def receive():
            return {"type": "http.disconnect"}

        response = self.response()
        actor = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        try:
            await asyncio.wait_for(sending.wait(), 2)
            actor.cancel()
            await asyncio.wait_for(closing.wait(), 2)
            actor.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(actor.done())
            release_close.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(actor, 2)
            self.connection.close.assert_called_once()
        finally:
            release_close.set()
            await asyncio.gather(actor, return_exceptions=True)

    async def test_legacy_asgi_disconnect_listener_closes_suspended_body(self):
        sending = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                sending.set()
                await asyncio.Event().wait()

        async def receive():
            await sending.wait()
            return {"type": "http.disconnect"}

        await asyncio.wait_for(
            self.response()({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send),
            2,
        )
        self.connection.close.assert_called_once()


DATABASE_URL = os.environ.get("WORKFLOW_TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "WORKFLOW_TEST_DATABASE_URL is not set")
class WorkflowStreamPostgresCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_failure_releases_real_advisory_lock(self):
        brief_id = uuid4()
        owned_connection = []

        def source():
            with execution.workflow_execution(brief_id, database_url=DATABASE_URL) as guard:
                owned_connection.append(guard.connection)
                yield b"first"
                yield b"second"

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        async def receive():
            return {"type": "http.disconnect"}

        response = WorkflowStreamingResponse(_stream_body(execution.preserve_execution_context(source())))
        with self.assertRaises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertTrue(owned_connection[0].closed)
        with execution.workflow_execution(brief_id, database_url=DATABASE_URL):
            pass

    async def test_cancelled_stream_releases_real_advisory_lock_before_finishing(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        brief_id = uuid4()
        owned_connection = []

        def source():
            with execution.workflow_execution(brief_id, database_url=DATABASE_URL) as guard:
                owned_connection.append(guard.connection)
                yield b"first"
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("Test did not release the model worker")
                yield b"second"

        async def consume():
            async for _ in _stream_body(execution.preserve_execution_context(source())):
                pass

        actor = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            actor.cancel()
            await asyncio.sleep(0.01)
            actor.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(actor.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(actor, 2)
            self.assertTrue(owned_connection[0].closed)
            # A fresh physical session can acquire the very same workflow now.
            with execution.workflow_execution(brief_id, database_url=DATABASE_URL):
                pass
        finally:
            release.set()
            await asyncio.gather(actor, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
