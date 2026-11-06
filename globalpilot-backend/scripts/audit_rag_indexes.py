"""Reproduce RAG index plans using synthetic data in an explicitly selected test DB.

No application .env, embedding API or application startup is loaded. The script
creates and drops only its own random schema, and refuses non-test DB names.
Example: .venv/bin/python scripts/audit_rag_indexes.py --database-url "$RAG_INDEX_AUDIT_DATABASE_URL"
"""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import time
from uuid import UUID, uuid4
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np
from langchain_core.embeddings import Embeddings
from langchain_postgres import PGVector
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.rag.models import KnowledgeDocument
from app.modules.teams.models import Team

AUDIT_TEAM_ID = UUID(int=2_000_000_000)


INDEX_DDL = """CREATE INDEX ix_embedding_collection_brand_document
ON langchain_pg_embedding (collection_id, (cmetadata ->> 'brand_id'), (cmetadata ->> 'document_id'))"""


class LocalEmbeddings(Embeddings):
    def embed_documents(self, texts):
        raise AssertionError("Benchmark supplies its vectors directly")

    def embed_query(self, value):
        raise AssertionError("Benchmark supplies its vectors directly")


@contextmanager
def capture(engine):
    statements = []

    def record(_conn, _cursor, sql, parameters, _context, _many):
        if sql.startswith("SELECT") and " AS distance" in sql:
            statements.append((sql, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def bound_statement(engine, statement):
    """Capture dialect-bound SQL/parameters, including JSON and Vector adapters."""
    found = []

    def record(_conn, _cursor, sql, parameters, _context, _many):
        found.append((sql, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        with engine.connect() as conn:
            conn.execute(statement)
            conn.rollback()  # DELETE is measured only on this isolated schema.
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return found[-1]


async def native_search_statements(url, schema, query, brand_id, app_queries):
    """Use the same native async PGVector query path as the HTTP service."""
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema},public"},
                                 execution_options={"schema_translate_map": {None: schema}})
    try:
        store = PGVector(connection=engine, embeddings=LocalEmbeddings(), collection_name="brand_knowledge",
                         use_jsonb=True, create_extension=False, async_mode=True)
        await store.__apost_init__()
        statements, ids = {}, {}
        for label, filter_value in (("jsonpath", {"brand_id": str(brand_id), "team_id": str(AUDIT_TEAM_ID)}),
                                    ("text_in", app_queries.brand_filter(str(brand_id), team_id=str(AUDIT_TEAM_ID)))):
            with capture(engine.sync_engine) as captured:
                results = await store.asimilarity_search_with_score_by_vector(query, k=20, filter=filter_value)
            statements[label] = captured[-1]
            ids[label] = [doc.id for doc, _ in results]
        return statements, ids
    finally:
        await engine.dispose()


def measure(engine, statement, rounds, *, fetch=False):
    sql, params = statement
    plans, fetch_ms = [], []
    payload_bytes = None
    with engine.connect() as conn:
        # Warm the query/data once; never disable sequential scans.
        conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).scalar_one()
        conn.rollback()
        for _ in range(rounds):
            plan = conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params).scalar_one()[0]
            plans.append(plan)
            conn.rollback()
            if fetch:
                start = time.perf_counter()
                rows = conn.exec_driver_sql(sql, params).all()
                fetch_ms.append((time.perf_counter() - start) * 1000)
                # Force TOAST materialization and count returned body/file bytes.
                payload_bytes = sum(len(v) if isinstance(v, (str, bytes)) else len(str(v))
                                    for row in rows for v in row if v is not None)
                conn.rollback()
    result = {"sql": sql, "execution_ms_median": statistics.median(p["Execution Time"] for p in plans),
              "rounds": plans}
    if fetch:
        result.update(fetch_ms_median=statistics.median(fetch_ms), fetched_payload_bytes=payload_bytes)
    return result


def populate(engine, store, chunks, brands, dimensions):
    rng = np.random.default_rng(20261014)
    vectors = rng.standard_normal((chunks, dimensions), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    brand_ids = [UUID(int=i + 1) for i in range(brands)]
    document_ids = [UUID(int=1_000_000 + i) for i in range((chunks + 9) // 10)]
    with Session(engine) as session:
        collection_id = store.get_collection(session).uuid
    with engine.begin() as conn:
        conn.execute(Team.__table__.insert().values(id=AUDIT_TEAM_ID, name="Synthetic RAG audit team"))
        raw = conn.connection.driver_connection
        register_vector(raw)
        with raw.cursor() as cur, cur.copy(
            "COPY langchain_pg_embedding (id,collection_id,embedding,document,cmetadata) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types(["text", "uuid", "vector", "text", "jsonb"])
            for i, vector in enumerate(vectors):
                brand = brand_ids[(i // 10) % brands]
                document = document_ids[i // 10]
                copy.write_row((f"{brand}:{document}:{i % 10}", collection_id, vector,
                                f"synthetic chunk {i}: " + "knowledge " * 45,
                                Jsonb({"team_id": str(AUDIT_TEAM_ID), "brand_id": str(brand), "document_id": str(document),
                                       "chunk_index": i % 10, "source": f"document-{i // 10}.txt"})))
        conn.execute(Brand.__table__.insert(), [{"id": value, "team_id": AUDIT_TEAM_ID, "name": f"brand-{i}"}
                                               for i, value in enumerate(brand_ids)])
        now = datetime(2026, 10, 14)
        # Incompressible payloads expose the cost of fetching unused large fields.
        for start in range(0, len(document_ids), 100):
            rows = [{"id": doc, "team_id": AUDIT_TEAM_ID, "brand_id": brand_ids[i % brands], "source": f"document-{i}.txt",
                     "body": rng.bytes(4096).hex(), "file_bytes": rng.bytes(16384),
                     "media_type": "text/plain", "chunk_count": 10, "version": 1,
                     "created_at": now, "updated_at": now}
                    for i, doc in enumerate(document_ids[start:start + 100], start)]
            conn.execute(KnowledgeDocument.__table__.insert(), rows)
        conn.exec_driver_sql("ANALYZE langchain_pg_embedding")
        conn.exec_driver_sql("ANALYZE knowledge_documents")
    return collection_id, brand_ids[0], document_ids[0], vectors[0].tolist()


def application_queries():
    """Load actual query helpers without loading configuration or provider clients."""
    replacements = {}
    for name, attributes in {
        "app.core.config": {"settings": SimpleNamespace(database_url="unused")},
        "app.db.database": {"async_engine": None},
        "app.infra.ai.embeddings": {"embeddings": LocalEmbeddings()},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        replacements[name] = module
    path = Path(__file__).resolve().parents[1] / "app/modules/rag/retrieval/vector_store.py"
    spec = importlib.util.spec_from_file_location("index_audit_vector_queries", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, replacements):
        spec.loader.exec_module(module)
    return module


def audit(url, chunks, brands, dimensions, rounds):
    schema = "rag_index_audit_" + uuid4().hex
    admin = create_engine(url)
    engine = None
    try:
        with admin.begin() as conn:
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"},
                               execution_options={"schema_translate_map": {None: schema}})
        store = PGVector(connection=engine, embeddings=LocalEmbeddings(), collection_name="brand_knowledge",
                         use_jsonb=True, create_extension=False)
        with engine.begin() as conn:
            Base.metadata.create_all(conn, tables=[Team.__table__, Brand.__table__, KnowledgeDocument.__table__])
        cid, bid, did, query = populate(engine, store, chunks, brands, dimensions)
        print(f"Seeded {chunks} chunks x {dimensions} dimensions / {brands} brands", flush=True)
        table = store.EmbeddingStore
        app_queries = application_queries()
        native, ids = asyncio.run(native_search_statements(url, schema, query, bid, app_queries))
        if ids["jsonpath"] != ids["text_in"]:
            raise AssertionError("Exact cosine result order changed")
        preview_before = bound_statement(engine, select(table.document, table.cmetadata).where(
            table.collection_id == cid, store._create_filter_clause({"team_id": str(AUDIT_TEAM_ID), "brand_id": str(bid), "document_id": str(did)})))
        preview_after = bound_statement(engine, app_queries._document_text_statement(
            store, cid, str(bid), str(did), team_id=str(AUDIT_TEAM_ID),
        ))
        remove = bound_statement(engine, app_queries._document_chunks_delete(
            store, cid, str(bid), str(did), team_id=str(AUDIT_TEAM_ID),
        ))
        full_list = bound_statement(engine, select(KnowledgeDocument).where(
            KnowledgeDocument.brand_id == bid, KnowledgeDocument.team_id == AUDIT_TEAM_ID).order_by(KnowledgeDocument.updated_at.desc()))
        metadata_list = bound_statement(engine, select(*[getattr(KnowledgeDocument, name) for name in
            ("id", "brand_id", "source", "chunk_count", "version", "created_at", "updated_at")]).where(
            KnowledgeDocument.brand_id == bid, KnowledgeDocument.team_id == AUDIT_TEAM_ID).order_by(KnowledgeDocument.updated_at.desc()))
        with engine.connect() as conn:
            versions = {"postgresql": conn.exec_driver_sql("SHOW server_version").scalar_one(),
                        "vector": conn.exec_driver_sql("SELECT extversion FROM pg_extension WHERE extname='vector'").scalar_one()}
            indexes = list(conn.exec_driver_sql("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename='langchain_pg_embedding'").scalars())
        report = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "dataset": {"chunks": chunks, "brands": brands, "dimensions": dimensions, "seed": 20261014,
                               "documents": (chunks + 9) // 10, "rounds": rounds},
                  "versions": {**versions, **{name: importlib.metadata.version(name)
                      for name in ("langchain-postgres", "pgvector", "sqlalchemy", "psycopg")}},
                  "before_indexes": indexes, "candidate_index": INDEX_DDL,
                  "query_parameters": {"team_id": str(AUDIT_TEAM_ID), "collection_id": str(cid), "brand_id": str(bid), "document_id": str(did),
                                       "vector_sha256": hashlib.sha256(np.asarray(query, dtype=np.float32).tobytes()).hexdigest(), "k": 20},
                  "exact_top20_ids_equal": True, "baseline": {}, "after": {}}
        for name, stmt in (("search", native["jsonpath"]), ("search_text_in_no_index", native["text_in"]),
                           ("document_preview", preview_before), ("document_delete", remove)):
            report["baseline"][name] = measure(engine, stmt, rounds)
        report["baseline"]["document_list"] = measure(engine, full_list, rounds, fetch=True)
        with engine.begin() as conn:
            conn.exec_driver_sql(INDEX_DDL)
            conn.exec_driver_sql("ANALYZE langchain_pg_embedding")
        for name, stmt in (("search", native["text_in"]), ("jsonpath_with_new_index", native["jsonpath"]),
                           ("document_preview", preview_after), ("document_delete", remove)):
            report["after"][name] = measure(engine, stmt, rounds)
        report["after"]["document_list"] = measure(engine, metadata_list, rounds, fetch=True)
        _, after_ids = asyncio.run(native_search_statements(url, schema, query, bid, app_queries))
        if ids["jsonpath"] != after_ids["text_in"]:
            raise AssertionError("Adding the expression index changed exact neighbors")
        # Exercise psycopg's prepared statements beyond its default threshold.
        with engine.connect() as conn:
            for _ in range(15):
                conn.exec_driver_sql(*native["text_in"]).all()
            report["prepared_statement_check"] = conn.exec_driver_sql(
                "SELECT generic_plans, custom_plans FROM pg_prepared_statements WHERE statement LIKE 'SELECT%%' AND statement LIKE '%% AS distance%%'"
            ).mappings().all()
            report["prepared_statement_check"] = [dict(row) for row in report["prepared_statement_check"]]
        report["notes"] = ["All data is synthetic; these are scaling measurements, not production latency.",
                           "Each measurement warms once, then records the median of repeated warm-cache plans; no OS cache flush or cold-cache latency claim.",
                           "Exact cosine search remains exact; no ANN index or enable_seqscan override is used.",
                           "EXPLAIN ANALYZE DELETE runs in rolled-back transactions on the private schema.",
                           "List payload/fetch metrics include TOAST fetching; EXPLAIN alone does not measure network payload.",
                           "List remains unpaginated and grows with all documents in a brand."]
        return report
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("RAG_INDEX_AUDIT_DATABASE_URL"))
    parser.add_argument("--chunks", type=int, default=40000)
    parser.add_argument("--brands", type=int, default=100)
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--rounds", type=int, choices=range(3, 6), default=5)
    parser.add_argument("--output", type=Path, default=Path("docs/index-audit/rag.json"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("An explicit disposable database URL is required")
    url = make_url(args.database_url)
    if url.get_backend_name() != "postgresql" or not (url.database or "").startswith(("test_", "audit_", "refactor_")):
        parser.error("Refusing a database without a test_, audit_ or refactor_ name")
    if args.chunks < 10 or args.brands < 1 or args.brands > args.chunks // 10 or args.dimensions < 1:
        parser.error("Invalid synthetic dataset size")
    report = audit(url.set(drivername="postgresql+psycopg"), args.chunks, args.brands, args.dimensions, args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({phase: {name: result["execution_ms_median"] for name, result in report[phase].items()}
                      for phase in ("baseline", "after")}, indent=2))


if __name__ == "__main__":
    main()
