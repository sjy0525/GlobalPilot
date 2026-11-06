"""One physical PostgreSQL session owns each mutable workflow execution.

The advisory lock, checkpoint writes and final projection use this same session.
If PostgreSQL drops it, an old worker cannot reconnect through a pool and keep
writing after another worker acquires the lock.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import RLock
from typing import Any, TypeVar
from uuid import UUID

import psycopg
from fastapi import HTTPException
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg.rows import dict_row

from app.core.config import settings

logger = logging.getLogger(__name__)


def to_psycopg_conninfo(database_url: str) -> str:
    if "://" not in database_url:
        return database_url
    scheme, rest = database_url.split("://", 1)
    dialect = scheme.split("+", 1)[0]
    if dialect == "postgresql":
        return f"postgresql://{rest}"
    return database_url


def workflow_lock_key(brief_id: UUID | str) -> int:
    identity = UUID(str(brief_id))
    digest = hashlib.blake2b(
        b"globalpilot:marketing-workflow:" + identity.bytes, digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


class WorkflowExecutionLostError(RuntimeError):
    """The physical session that owned a workflow is no longer usable."""


class WorkflowExecution:
    def __init__(self, brief_id: UUID, connection: psycopg.Connection):
        self.brief_id = brief_id
        self.connection = connection
        self._lock = RLock()
        self._active = True

    def _assert_active(self) -> None:
        if not self._active or self.connection.closed or self.connection.broken:
            raise WorkflowExecutionLostError("Workflow execution connection was lost")

    @contextmanager
    def checkpoint_saver(self, serde: SerializerProtocol) -> Iterator[PostgresSaver]:
        # LangGraph writes checkpoints on background threads. They share this
        # session with projection persistence and must never interleave cursors.
        with self._lock:
            self._assert_active()
            saver = PostgresSaver(self.connection, serde=serde)
            saver.lock = self._lock
            yield saver

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor[dict[str, Any]]]:
        """Yield a cursor for a short projection transaction on the lock session."""
        with self._lock:
            self._assert_active()
            with self.connection.transaction():
                with self.connection.cursor(row_factory=dict_row) as cursor:
                    yield cursor

    def close(self) -> None:
        with self._lock:
            self._active = False
            # This is a dedicated connection, never a pooled checkout. Closing
            # the physical session releases its lock even after a failed query.
            self.connection.close()


_current_execution: ContextVar[WorkflowExecution | None] = ContextVar(
    "marketing_workflow_execution", default=None
)


def require_workflow_execution(brief_id: UUID | str) -> WorkflowExecution:
    guard = _current_execution.get()
    if guard is None:
        raise RuntimeError("Checkpoint writes require an active workflow execution")
    if guard.brief_id != UUID(str(brief_id)):
        raise RuntimeError("Workflow execution does not own this brief")
    guard._assert_active()
    return guard


def current_workflow_execution(brief_id: UUID | str) -> WorkflowExecution | None:
    if _current_execution.get() is None:
        return None
    return require_workflow_execution(brief_id)


@contextmanager
def workflow_execution(
    brief_id: UUID | str, *, database_url: str | None = None
) -> Iterator[WorkflowExecution]:
    if _current_execution.get() is not None:
        raise RuntimeError("Nested workflow execution scopes are not supported")

    identity = UUID(str(brief_id))
    connection = psycopg.connect(
        to_psycopg_conninfo(database_url or settings.database_url),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        connect_timeout=10,
    )
    guard = WorkflowExecution(identity, connection)
    token = None
    failure = None
    try:
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%s::bigint) AS acquired",
            (workflow_lock_key(identity),),
        ).fetchone()
        if row is None or not row["acquired"]:
            raise HTTPException(status_code=409, detail="该任务正在处理中，请稍后重试")
        token = _current_execution.set(guard)
        yield guard
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            if token is not None:
                _current_execution.reset(token)
        finally:
            try:
                guard.close()
            except Exception:
                if failure is None:
                    raise
                logger.exception("Failed to close workflow connection after execution failure")


T = TypeVar("T")


def preserve_execution_context(iterator: Iterator[T]) -> Iterator[T]:
    """Keep one Context while Starlette advances a sync stream in worker threads.

    Each AnyIO offload normally receives a new Context copy. A scope spanning
    generator yields otherwise loses its guard and cannot reset its token.
    The consumer must still close this iterator when streaming is cancelled.
    """
    context = copy_context()

    def iterate() -> Iterator[T]:
        try:
            while True:
                try:
                    item = context.run(next, iterator)
                except StopIteration:
                    return
                yield item
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                context.run(close)

    return iterate()
