"""Measured query indexes, installed outside application transactions.

CREATE INDEX CONCURRENTLY permits existing workers to read and write while an
old database is upgraded. Reusing a name is safe only when its definition and
validity match; a failed concurrent build must not silently count as installed.
"""
from dataclasses import dataclass
import time

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class QueryIndex:
    name: str
    table: str
    keys: tuple[str, ...]
    descending: tuple[bool, ...]
    opclasses: tuple[str, ...]
    collations: tuple[str | None, ...]
    predicate: str | None = None


QUERY_INDEXES = (
    QueryIndex(
        "ix_embedding_collection_brand_document", "langchain_pg_embedding",
        ("collection_id", "(cmetadata ->> 'brand_id'::text)", "(cmetadata ->> 'document_id'::text)"),
        (False, False, False),
        ("pg_catalog.uuid_ops", "pg_catalog.text_ops", "pg_catalog.text_ops"),
        (None, "pg_catalog.default", "pg_catalog.default"),
    ),
    QueryIndex(
        "ix_briefs_user_updated_active", "briefs", ("user_id", "updated_at"),
        (False, True), ("pg_catalog.uuid_ops", "pg_catalog.timestamp_ops"),
        (None, None), "workflow_status IS NOT NULL",
    ),
    QueryIndex(
        "ix_eval_tasks_run_status", "eval_tasks", ("run_id", "execution_status"),
        (False, False),
        ("pg_catalog.uuid_ops", "pg_catalog.text_ops"), (None, "pg_catalog.default"),
    ),
)


def _index_state(connection, name):
    return connection.execute("""
        SELECT t.relname AS table_name, am.amname AS method,
               i.indisvalid, i.indisready, i.indisunique, i.indnatts,
               ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, TRUE)
                     FROM generate_series(1, i.indnkeyatts) AS k) AS keys,
               i.indoption::text AS options,
               ARRAY(SELECT n.nspname || '.' || op.opcname
                     FROM unnest(i.indclass) WITH ORDINALITY AS key(oid, position)
                     JOIN pg_opclass op ON op.oid = key.oid
                     JOIN pg_namespace n ON n.oid = op.opcnamespace
                     ORDER BY key.position) AS opclasses,
               ARRAY(SELECT CASE WHEN key.oid = 0 THEN NULL ELSE n.nspname || '.' || c.collname END
                     FROM unnest(i.indcollation) WITH ORDINALITY AS key(oid, position)
                     LEFT JOIN pg_collation c ON c.oid = key.oid
                     LEFT JOIN pg_namespace n ON n.oid = c.collnamespace
                     ORDER BY key.position) AS collations,
               pg_get_expr(i.indpred, i.indrelid) AS predicate
        FROM pg_class AS idx
        JOIN pg_namespace AS ns ON ns.oid = idx.relnamespace
        JOIN pg_index AS i ON i.indexrelid = idx.oid
        JOIN pg_class AS t ON t.oid = i.indrelid
        JOIN pg_am AS am ON am.oid = idx.relam
        WHERE ns.nspname = current_schema() AND idx.relname = %s
    """, (name,)).fetchone()


def _validate_index(state, index):
    # pg_get_indexdef(..., column) omits ordering; indoption records DESC and
    # NULLS FIRST (3), or ordinary ASC / NULLS LAST (0).
    expected_options = tuple(3 if desc else 0 for desc in index.descending)
    predicate = (state["predicate"] or "").strip("() ") if state else None
    if not state or not (
        state["indisvalid"] and state["indisready"] and not state["indisunique"]
        and state["table_name"] == index.table and state["method"] == "btree"
        and state["indnatts"] == len(index.keys) and tuple(state["keys"]) == index.keys
        and tuple(map(int, state["options"].split())) == expected_options
        and tuple(state["opclasses"]) == index.opclasses
        and tuple(state["collations"]) == index.collations
        and predicate == (index.predicate or "")
    ):
        raise RuntimeError(
            f"Index {index.name} has an unexpected definition or is invalid; "
            "inspect and repair it before retrying the query-index migration"
        )


def apply_query_indexes(connection) -> list[dict[str, str]]:
    """Caller holds the schema mutex. The connection must be in autocommit."""
    if not connection.autocommit:
        raise ValueError("Query indexes require an autocommit connection")
    schema = connection.execute("SELECT current_schema() AS name").fetchone()["name"]
    if schema is None:
        raise RuntimeError("No current schema for query-index migration")
    results = []
    for index in QUERY_INDEXES:
        state = _index_state(connection, index.name)
        existed = state is not None
        if not existed:
            keys = sql.SQL(", ").join(
                sql.SQL(key + (" DESC" if desc else ""))
                for key, desc in zip(index.keys, index.descending, strict=True)
            )
            statement = sql.SQL("CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON {} USING btree ({})").format(
                sql.Identifier(index.name), sql.Identifier(schema, index.table), keys,
            )
            if index.predicate:
                statement += sql.SQL(" WHERE " + index.predicate)
            connection.execute(statement)
            state = _index_state(connection, index.name)
        _validate_index(state, index)
        results.append({"name": index.name, "status": "existing" if existed else "created"})
    return results


def ensure_query_indexes(database_url: str, *, acquire_lock: bool = True) -> list[dict[str, str]]:
    conninfo = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(conninfo, autocommit=True, row_factory=dict_row, connect_timeout=10) as connection:
        if acquire_lock:
            # Avoid a blocking SELECT snapshot preventing concurrent index
            # creation by the lock owner from finishing.
            while not connection.execute("SELECT pg_try_advisory_lock(7140214001) AS acquired").fetchone()["acquired"]:
                time.sleep(0.05)
        # Closing the physical connection also releases any acquired mutex.
        return apply_query_indexes(connection)
