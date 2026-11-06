import asyncio
import json
import logging
from contextlib import asynccontextmanager

import psycopg
from anyio import CancelScope
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.engine import make_url
from starlette.concurrency import run_in_threadpool
from app.api.router import api_router
from app.core.config import settings
from app.core.http_errors import install_exception_handlers
from app.db.base import Base
from app.db.database import async_engine, async_sessionLocal
from app.db.concurrency_migrations import apply_concurrency_migrations
from app.db.team_scope_migrations import apply_team_scope_migrations
from app.db.query_indexes import ensure_query_indexes
from app.db.model_registry import (
    Brand,
    Brief,
    KnowledgeDocument,
    EvalCase,
    EvalExecutorLease,
    EvalRun,
    EvalTask,
    EvalTestSet,
    User,
    Team,
    MemoryJob,
)
from app.modules.rag.processing.document_processing import close_document_processing, initialize_document_processing
from app.modules.rag.retrieval.rerank import aclose_rerank_client, initialize_async_rerank_client
from app.modules.rag.retrieval.vector_store import aclose_vector_store, ainitialize_vector_store, close_vector_store
from app.modules.evaluation.service import ensure_test_set, recover_interrupted_runs
from app.modules.brands.service import seed_all_team_brands
from app.modules.marketing.workflow.checkpointer import (
    close_checkpointer,
    setup_checkpointer,
)

logger = logging.getLogger(__name__)

def _ensure_brief_social_media(conn) -> None:
    conn.execute(
        text(
            "ALTER TABLE briefs "
            "ADD COLUMN IF NOT EXISTS social_media VARCHAR(50)[] "
            "NOT NULL DEFAULT '{}'"
        )
    )

def _ensure_brief_workflow_columns(conn) -> None:
    statements = (
        "ALTER TABLE briefs ADD COLUMN IF NOT EXISTS workflow_status VARCHAR(32)",
        "ALTER TABLE briefs ADD COLUMN IF NOT EXISTS strategy TEXT",
        "ALTER TABLE briefs ADD COLUMN IF NOT EXISTS final_output TEXT",
        "ALTER TABLE briefs "
        "ADD COLUMN IF NOT EXISTS revision_count INTEGER NOT NULL DEFAULT 0",
    )
    for statement in statements:
        conn.execute(text(statement))
    _ensure_brief_content_jsonb(conn)


def _ensure_brief_content_jsonb(conn) -> None:
    udt = conn.execute(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'briefs' AND column_name = 'content'"
        )
    ).scalar()
    if udt == "jsonb":
        return
    if udt is None:
        conn.execute(text("ALTER TABLE briefs ADD COLUMN content JSONB"))
        return
    conn.execute(text("ALTER TABLE briefs ADD COLUMN IF NOT EXISTS content_json JSONB"))
    rows = conn.execute(text("SELECT id, content FROM briefs WHERE content IS NOT NULL")).all()
    for brief_id, raw in rows:
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            conn.execute(
                text("UPDATE briefs SET content_json = CAST(:payload AS jsonb) WHERE id = :id"),
                {"payload": json.dumps(parsed, ensure_ascii=False), "id": brief_id},
            )
    conn.execute(text("ALTER TABLE briefs DROP COLUMN content"))
    conn.execute(text("ALTER TABLE briefs RENAME COLUMN content_json TO content"))


def _ensure_knowledge_document_body(conn) -> None:
    conn.execute(text("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS body TEXT"))
    conn.execute(text("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS media_type VARCHAR(127)"))
    conn.execute(text("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS file_bytes BYTEA"))
    conn.execute(text("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS structured JSONB"))


@asynccontextmanager
async def _startup_session():
    db = async_sessionLocal()
    operation_failed = False
    try:
        yield db
    except BaseException:
        operation_failed = True
        raise
    finally:
        try:
            await db.close()
        except BaseException as exc:
            if not operation_failed:
                raise
            await _log_failure("Startup Session cleanup failed after its operation failed", exc)


def _seed_brands(db) -> None:
    seed_all_team_brands(db)
    db.commit()

def _lock_startup_schema(conn) -> None:
    # Protect standalone migration callers for the lifetime of their transaction.
    conn.execute(text("SELECT pg_advisory_xact_lock(7140214001)"))


def _prepare_team_scope_schema(conn):
    # Existing brands may predate team_id while a dependent table is missing.
    # Upgrade those referenced keys before create_all creates composite FKs.
    schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    if schema is None:
        raise RuntimeError("A current schema is required for application initialization")
    # checkfirst must not mistake a same-named public/fallback table for a table
    # in the intended schema. Every ORM DDL target and FK is explicitly bound.
    ddl_conn = conn.execution_options(schema_translate_map={None: schema})
    Team.__table__.create(bind=ddl_conn, checkfirst=True)
    apply_team_scope_migrations(conn)
    return ddl_conn


def _prepare_schema(conn, *, startup_lock_held: bool = False) -> None:
    # Retain protection when this helper is used outside initialize_app too.
    if not startup_lock_held:
        _lock_startup_schema(conn)
    ddl_conn = _prepare_team_scope_schema(conn)
    Base.metadata.create_all(bind=ddl_conn)
    _ensure_brief_social_media(conn)
    _ensure_brief_workflow_columns(conn)
    _ensure_knowledge_document_body(conn)
    apply_concurrency_migrations(conn)


@asynccontextmanager
async def _startup_schema_lock():
    # A dedicated session avoids consuming the only pool connection while
    # PGVector and application migrations borrow their own connections.
    conninfo = make_url(settings.database_url).set(drivername="postgresql")
    connection = await psycopg.AsyncConnection.connect(
        conninfo.render_as_string(hide_password=False),
        autocommit=True,
        connect_timeout=10,
    )
    operation_failed = False
    try:
        # A blocking advisory-lock SELECT keeps a transaction snapshot while it
        # waits. PostgresSaver's CREATE INDEX CONCURRENTLY can then wait for that
        # snapshot, deadlocking the owner with its waiters. Each autocommit try
        # finishes before the asynchronous backoff, releasing the snapshot.
        while True:
            cursor = await connection.execute("SELECT pg_try_advisory_lock(7140214001)")
            if (await cursor.fetchone())[0]:
                break
            await asyncio.sleep(0.05)
        yield
    except BaseException:
        operation_failed = True
        raise
    finally:
        with CancelScope(shield=True):
            closing = asyncio.create_task(connection.close())
            try:
                await asyncio.wait({closing})
                closing.result()
            except asyncio.CancelledError:
                try:
                    await _finish_worker_task(closing)
                except BaseException as exc:
                    await _finish_worker_task(asyncio.create_task(_log_failure(
                        "Startup lock cleanup failed while cancelled", exc
                    )))
                if not operation_failed:
                    raise
            except BaseException as exc:
                if not operation_failed:
                    raise
                await _finish_worker_task(asyncio.create_task(_log_failure(
                    "Startup lock cleanup failed after initialization failed", exc
                )))


def initialize_memory_resources() -> None:
    from app.modules.memory.store import open_memory_store
    from app.modules.memory.services.memory_job import initialize_memory_worker

    with open_memory_store() as store:
        store.setup()
    initialize_memory_worker()


def close_memory_resources() -> None:
    from app.modules.memory.services.memory_job import close_memory_worker

    close_memory_worker()


def _initialize_sync_resources() -> None:
    # The startup session already owns the schema mutex. Concurrent index DDL
    # must use a separate autocommit connection, outside _prepare_schema's tx.
    ensure_query_indexes(settings.database_url, acquire_lock=False)
    # The existing synchronous LangGraph workflow owns this checkpointer.
    setup_checkpointer()
    initialize_document_processing()
    initialize_async_rerank_client()
    initialize_memory_resources()


def _close_sync_resources() -> None:
    first_error = None
    for close in (close_memory_resources, close_checkpointer, close_vector_store, close_document_processing):
        try:
            close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            else:
                logger.exception("Additional synchronous resource cleanup failed")
    if first_error is not None:
        raise first_error


async def _log_failure(message: str, exc: BaseException) -> None:
    await run_in_threadpool(logger.error, message, exc_info=(type(exc), exc, exc.__traceback__))


async def shutdown_app() -> None:
    first_error = None
    for close in (aclose_rerank_client, aclose_vector_store, async_engine.dispose):
        try:
            await close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            else:
                await _log_failure("Additional asynchronous resource cleanup failed", exc)
    try:
        await run_in_threadpool(_close_sync_resources)
    except BaseException as exc:
        if first_error is None:
            first_error = exc
        else:
            await _log_failure("Synchronous resource cleanup also failed", exc)
    if first_error is not None:
        raise first_error


async def initialize_app() -> None:
    try:
        async with _startup_schema_lock():
            await ainitialize_vector_store()
            async with async_engine.begin() as conn:
                # The dedicated session already owns the shared schema lock.
                await conn.run_sync(_prepare_schema, startup_lock_held=True)
            await run_in_threadpool(_initialize_sync_resources)
        async with _startup_session() as db:
            # Legacy ORM routines run through the same asynchronous connection;
            # run_sync here uses greenlets, not a thread or synchronous driver.
            await db.run_sync(_seed_brands)
            await db.run_sync(recover_interrupted_runs)
            await db.run_sync(ensure_test_set)
    except BaseException:
        try:
            await shutdown_app()
        except BaseException as exc:
            await _log_failure("Application cleanup failed after startup failed", exc)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup = asyncio.create_task(initialize_app())
    try:
        await asyncio.wait({startup})
        startup.result()
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            try:
                await _finish_worker_task(startup)
            except BaseException as exc:
                # initialize_app already releases resources when startup fails.
                await _finish_worker_task(asyncio.create_task(run_in_threadpool(
                    logger.error,
                    "Application startup failed while lifespan was cancelled",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )))
            else:
                try:
                    await _finish_worker_task(asyncio.create_task(shutdown_app()))
                except BaseException as exc:
                    await _finish_worker_task(asyncio.create_task(run_in_threadpool(
                        logger.error,
                        "Application cleanup failed after startup was cancelled",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )))
        raise
    lifespan_failed = False
    try:
        yield
    except BaseException:
        lifespan_failed = True
        raise
    finally:
        with CancelScope(shield=True):
            shutdown = asyncio.create_task(shutdown_app())
            try:
                await asyncio.wait({shutdown})
                shutdown.result()
            except asyncio.CancelledError:
                # AnyIO shielding does not cover direct Task.cancel(). Keep the
                # native close operations alive and drain them before exiting.
                try:
                    await _finish_worker_task(shutdown)
                except BaseException as exc:
                    await _finish_worker_task(asyncio.create_task(_log_failure(
                        "Application cleanup failed while lifespan was cancelled", exc
                    )))
                if not lifespan_failed:
                    raise
            except BaseException as exc:
                if not lifespan_failed:
                    raise
                await _finish_worker_task(asyncio.create_task(_log_failure(
                    "Application cleanup failed after lifespan failed", exc
                )))


async def _finish_worker_task(task: asyncio.Task):
    """Drain an already-running lifecycle operation despite repeated cancellation."""
    while True:
        try:
            await asyncio.wait({task})
            return task.result()
        except asyncio.CancelledError:
            if task.done():
                return task.result()

app = FastAPI(lifespan=lifespan)
install_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.auth_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Location", "WWW-Authenticate"],
)

app.include_router(api_router)

@app.get('/')
def root():
    return {
        "message":"GlobalPilot is running" 
    }
