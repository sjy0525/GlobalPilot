"""Persist accepted feedback and execute it outside the marketing request.

The queue owns no model transaction. A separate PostgreSQL session lock
serializes each user/category across processes. Failed/abandoned jobs are never
replayed automatically because a tool may have committed before the failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from langchain_core.callbacks import BaseCallbackHandler
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import select

from app.core.config import settings
from app.modules.briefs.models import Brief
from app.modules.memory.models import MemoryJob
from app.modules.memory.schemas.brand_memory import BrandMemory, MemoryScope, MemorySource
from app.modules.memory.schemas.memory_job import BriefMemoriesOutput, MemoryItemOutput, MemoryJobOutput
from app.modules.briefs.authorization import (
    load_owned_team_brief_cursor, require_owned_team_brief,
)
from app.modules.marketing.workflow.execution import to_psycopg_conninfo
from app.modules.memory.store import memory_namespace, open_memory_store

logger = logging.getLogger(__name__)
_lifecycle_lock = RLock()
_stop = Event()
_worker: Thread | None = None
WRITE_TOOLS = {"create_memory", "update_memory", "invalidate_memory"}


def enqueue_memory_feedback_events(cursor, brief, events) -> None:
    """Use the caller's projection transaction; never open a second connection."""
    for event in events or []:
        if not isinstance(event, dict) or event.get("action") not in {"approved", "rejected"}:
            continue
        feedback = str(event.get("feedback") or "").strip()
        checkpoint = str(event.get("source_checkpoint_id") or "").strip()
        if not feedback or not checkpoint or len(checkpoint) > 128:
            continue
        payload = {
            "feedback": feedback,
            "regions": brief.region,
            "platforms": list(brief.social_media or []),
        }
        cursor.execute(
            """INSERT INTO memory_jobs
               (id, brief_id, user_id, brand_id, source_checkpoint_id, payload,
                status, summary, error, tool_calls, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending', NULL, NULL, %s, now(), now())
               ON CONFLICT (brief_id, source_checkpoint_id) DO NOTHING""",
            (uuid4(), brief.id, brief.user_id, brief.brand_id, checkpoint,
             Jsonb(payload), Jsonb([])),
        )


def memory_job_lock_key(user_id, brand_id) -> int:
    """A distinct lock domain from the memory tools' short transaction locks."""
    identity = f"{UUID(str(user_id))}:{UUID(str(brand_id))}".encode()
    return int.from_bytes(hashlib.blake2b(
        b"globalpilot:memory-job-worker:" + identity, digest_size=8,
    ).digest(), "big", signed=True)


def _connect():
    return psycopg.connect(
        to_psycopg_conninfo(settings.database_url), autocommit=True,
        row_factory=dict_row, connect_timeout=10, prepare_threshold=0,
    )


def _json_value(value):
    if hasattr(value, "content"):
        value = value.content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            pass
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def summarize_tool_calls(calls) -> str:
    counts = {name: 0 for name in WRITE_TOOLS}
    for call in calls:
        if call.get("status") == "completed" and call.get("name") in counts:
            counts[call["name"]] += 1
    parts = [f"{label} {counts[name]} 条" for name, label in (
        ("create_memory", "新增"), ("update_memory", "更新"), ("invalidate_memory", "失效"),
    ) if counts[name]]
    return "；".join(parts) if parts else "本次未写入长期偏好"


class MemoryToolTrace(BaseCallbackHandler):
    """Record actual tool results, never infer successful writes from chat text."""

    raise_error = True

    def __init__(self, connection, job_id, *, authorize=None):
        self.connection = connection
        self.job_id = job_id
        self.calls: list[dict] = []
        self._runs: dict[str, dict] = {}
        self._lock = RLock()
        self._authorize = authorize

    def _save(self):
        # The live connection also owns the session lock. Loss aborts callbacks
        # rather than silently reconnecting and continuing an unfenced worker.
        row = self.connection.execute(
            """UPDATE memory_jobs SET tool_calls = %s, updated_at = now()
               WHERE id = %s AND status = 'running' RETURNING id""",
            (Jsonb(self.calls), self.job_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Memory job no longer owns its running state")

    def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, **kwargs):
        with self._lock:
            if self._authorize is not None:
                self._authorize()
            call = {
                "name": (serialized or {}).get("name") or kwargs.get("name") or "unknown",
                "arguments": _json_value(inputs if inputs is not None else input_str),
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            self.calls.append(call)
            self._runs[str(run_id)] = call
            self._save()

    def on_tool_end(self, output, *, run_id, **kwargs):
        with self._lock:
            call = self._runs[str(run_id)]
            result = _json_value(output)
            call["result"] = result
            call["finished_at"] = datetime.now(timezone.utc).isoformat()
            is_error = getattr(output, "status", None) == "error"
            if isinstance(result, dict) and result.get("ok") is False:
                is_error = True
            if call["name"] in WRITE_TOOLS:
                # Create returns {memory_id,version}; other mutations also
                # return ok=True. Missing write confirmation is an error.
                if not isinstance(result, dict) or not result.get("memory_id") or not result.get("version"):
                    is_error = True
            call["status"] = "error" if is_error else "completed"
            self._save()

    def on_tool_error(self, error, *, run_id, **kwargs):
        with self._lock:
            call = self._runs.get(str(run_id))
            if call is not None:
                call.update(status="error", error=type(error).__name__,
                            finished_at=datetime.now(timezone.utc).isoformat())
                self._save()


def _recover_abandoned(connection):
    scopes = connection.execute(
        "SELECT DISTINCT user_id, brand_id FROM memory_jobs WHERE status = 'running'",
    ).fetchall()
    for scope in scopes:
        key = memory_job_lock_key(scope["user_id"], scope["brand_id"])
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(%s::bigint) AS acquired", (key,),
        ).fetchone()["acquired"]
        if not acquired:
            continue
        try:
            connection.execute(
                """UPDATE memory_jobs SET status = 'needs_review', updated_at = now(),
                       error = '后台任务中断，可能已部分写入；请核对已有偏好，不会自动重跑。'
                   WHERE user_id = %s AND brand_id = %s AND status = 'running'""",
                (scope["user_id"], scope["brand_id"]),
            )
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s::bigint)", (key,))


def _claim_job(connection):
    with connection.transaction():
        # Batch selection lets independent categories progress while one scope
        # is held by another process. Only this short claim holds row locks.
        rows = connection.execute(
            """SELECT j.* FROM memory_jobs j WHERE j.status = 'pending'
               AND NOT EXISTS (SELECT 1 FROM memory_jobs blocked
                   WHERE blocked.user_id = j.user_id AND blocked.brand_id = j.brand_id
                     AND blocked.status IN ('running', 'needs_review'))
               ORDER BY j.created_at, j.id LIMIT 20 FOR UPDATE OF j SKIP LOCKED""",
        ).fetchall()
        for job in rows:
            key = memory_job_lock_key(job["user_id"], job["brand_id"])
            if not connection.execute(
                "SELECT pg_try_advisory_lock(%s::bigint) AS acquired", (key,),
            ).fetchone()["acquired"]:
                continue
            connection.execute(
                "UPDATE memory_jobs SET status = 'running', updated_at = now() WHERE id = %s",
                (job["id"],),
            )
            return job, key
    return None


def _load_job_brief(connection, job):
    # Jobs derive scope from their immutable source Brief, never model output.
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT team_id FROM briefs WHERE id = %s AND user_id = %s
               AND brand_id = %s AND team_id IS NOT NULL""",
            (job["brief_id"], job["user_id"], job["brand_id"]),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(404, "营销任务不存在")
        return load_owned_team_brief_cursor(
            cursor, team_id=row["team_id"], actor_user_id=job["user_id"],
            brief_id=job["brief_id"],
        )


def _run_job(connection, job):
    trace = MemoryToolTrace(
        connection, job["id"], authorize=lambda: _load_job_brief(connection, job),
    )
    try:
        brief = _load_job_brief(connection, job)
        from langchain_deepseek import ChatDeepSeek
        from app.modules.memory.agent import run_memory_agent

        model = ChatDeepSeek(
            model=settings.deepseek_model, api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url, temperature=0, max_tokens=1024,
            timeout=35.0, max_retries=0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        payload = job["payload"]
        # Existing Brand IDs currently identify categories. They are bound by
        # the owned Brief, never accepted as a caller-supplied scope override.
        run_memory_agent(
            MemoryScope(
                team_id=brief["team_id"], user_id=job["user_id"],
                brand_profile_id=job["brand_id"],
            ),
            MemorySource(brief_id=job["brief_id"], checkpoint_id=job["source_checkpoint_id"],
                         quote=payload["feedback"]),
            regions=payload["regions"], platforms=payload["platforms"],
            model=model, callbacks=[trace],
        )
        _load_job_brief(connection, job)
        has_errors = any(call.get("status") != "completed" for call in trace.calls)
        attempted_write = any(call.get("name") in WRITE_TOOLS for call in trace.calls)
        status = ("needs_review" if attempted_write else "failed") if has_errors else "completed"
        error = ("部分工具未成功，可能已部分写入；请核对已有偏好，不会自动重跑。"
                 if attempted_write else "工具未成功，本次未执行写工具；不会自动重跑。") if has_errors else None
    except Exception as exc:
        attempted_write = any(call.get("name") in WRITE_TOOLS for call in trace.calls)
        status = "needs_review" if attempted_write else "failed"
        detail = "可能已部分写入，请核对已有偏好" if attempted_write else "本次未执行写工具"
        error = f"记忆整理失败（{type(exc).__name__}），{detail}；不会自动重跑。"
        logger.error("Memory job %s failed: %s", job["id"], type(exc).__name__)
    connection.execute(
        """UPDATE memory_jobs SET status = %s, summary = %s, error = %s,
               tool_calls = %s, updated_at = now() WHERE id = %s AND status = 'running'""",
        (status, summarize_tool_calls(trace.calls), error, Jsonb(trace.calls), job["id"]),
    )


def _worker_loop():
    while not _stop.is_set():
        try:
            with _connect() as connection:
                _recover_abandoned(connection)
                claimed = _claim_job(connection)
                if claimed is not None:
                    job, key = claimed
                    try:
                        _run_job(connection, job)
                    finally:
                        if not connection.closed and not connection.broken:
                            connection.execute("SELECT pg_advisory_unlock(%s::bigint)", (key,))
        except Exception as exc:
            logger.error("Memory worker iteration failed: %s", type(exc).__name__)
        _stop.wait(1.0)


def initialize_memory_worker() -> None:
    global _worker
    with _lifecycle_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = Thread(target=_worker_loop, name="brand-memory-worker", daemon=True)
        _worker.start()


def close_memory_worker() -> None:
    with _lifecycle_lock:
        _stop.set()
        worker = _worker
    if worker is not None:
        # In-flight tools finish their own transactions. Shutdown never closes
        # the worker's lock session from another thread or retries its job.
        worker.join(timeout=5.0)


def get_brief_memories(db, user, brief_id: UUID, *, team_id: UUID) -> BriefMemoriesOutput:
    actor_user_id = user.id
    brief = require_owned_team_brief(
        db, team_id=team_id, actor_user_id=actor_user_id, brief_id=brief_id,
    )
    user_id, brand_id = brief.user_id, brief.brand_id
    jobs = db.scalars(select(MemoryJob).where(
        MemoryJob.brief_id == brief_id, MemoryJob.user_id == user_id,
    ).order_by(MemoryJob.created_at.desc(), MemoryJob.id.desc()).limit(20)).all()
    job_outputs = [MemoryJobOutput(
        id=job.id, status=job.status, summary=job.summary, error=job.error,
        created_at=job.created_at, updated_at=job.updated_at, tool_calls=job.tool_calls,
    ) for job in jobs]
    db.rollback()
    scope = MemoryScope(team_id=team_id, user_id=user_id, brand_profile_id=brand_id)
    memories = []
    with open_memory_store() as store:
        offset = 0
        while True:
            batch = store.search(memory_namespace(scope),
                                 filter={"status": "active", "duration": "long_term"},
                                 limit=100, offset=offset)
            for item in batch:
                memory = BrandMemory.model_validate(item.value)
                memories.append(MemoryItemOutput(
                    memory_id=memory.id, content=memory.content, regions=memory.regions,
                    platforms=memory.platforms, version=memory.version, update_at=memory.update_at,
                ))
            if len(batch) < 100:
                break
            offset += len(batch)
    memories.sort(key=lambda item: (item.update_at, str(item.memory_id)), reverse=True)
    # Store IO may be slow: a revocation while reading must not expose results.
    try:
        require_owned_team_brief(
            db, team_id=team_id, actor_user_id=actor_user_id, brief_id=brief_id,
        )
    finally:
        db.rollback()
    return BriefMemoriesOutput(items=memories, jobs=job_outputs)
