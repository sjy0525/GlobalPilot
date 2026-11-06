"""Read-only plans for an explicitly selected database, team and actor.

No app startup, migrations, PGVector constructor, saver.setup(), DDL, DML or
ANALYZE command runs here. Every database operation uses one READ ONLY,
REPEATABLE READ transaction with a 10-second statement timeout. EXPLAIN ANALYZE
executes only SELECT statements. Query results/content are never saved.
"""

import argparse
import importlib.metadata
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psycopg
from langchain_postgres import PGVector
from langchain_postgres.vectorstores import DistanceStrategy, _get_embedding_collection_store
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import any_, asc, bindparam, create_engine, func, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.briefs.models import Brief
from app.modules.evaluation.models import EvalExecutorLease, EvalRun, EvalTask
from app.modules.rag.models import KnowledgeDocument
from app.modules.users.models import User
from scripts._query_audit_scope import brief_list_statement

UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+")
VECTOR_PATTERN = re.compile(r"\[(?:\s*[-+0-9.eE]+\s*,){20,}\s*[-+0-9.eE]+\s*\]")


class Redactor:
    def __init__(self):
        self.values = {}

    def add(self, value, label):
        if value is not None:
            self.values[str(value)] = f"<{label}>"

    def clean(self, value):
        if isinstance(value, dict):
            return {str(key): self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, UUID):
            value = str(value)
        if hasattr(value, "obj"):
            return self.clean(value.obj)
        if isinstance(value, str):
            for sensitive, label in sorted(self.values.items(), key=lambda pair: len(pair[0]), reverse=True):
                value = value.replace(sensitive, label)
            value = UUID_PATTERN.sub("<uuid>", value)
            value = EMAIL_PATTERN.sub("<email>", value)
            return VECTOR_PATTERN.sub("[<embedding-redacted>]", value)
        return value


def bound_query(statement, dialect):
    """Compile and apply the installed dialect's bind adapters without executing."""
    compiled = statement.compile(dialect=dialect, compile_kwargs={"render_postcompile": True})
    parameters = dict(compiled.params)
    for name, processor in compiled._bind_processors.items():
        if name in parameters:
            parameters[name] = processor(parameters[name])
    return str(compiled), parameters


def model_missing(model, columns):
    table = model.__table__
    present = columns.get(table.name)
    if present is None:
        return [f"{table.name} (table missing)"]
    return [f"{table.name}.{name}" for name in table.columns.keys() if name not in present]


def explain(connection, redactor, label, query, parameters, rounds, *, unavailable=None, notes=None):
    result = {"label": label, "sql": query, "parameters": parameters, "notes": notes or []}
    if unavailable:
        result.update(status="blocked_by_schema", missing=unavailable)
        print(f"{label}: blocked by existing schema", flush=True)
        return redactor.clean(result)
    if not query.lstrip().upper().startswith("SELECT") or ";" in query:
        raise AssertionError("Audit permits only a single SELECT per EXPLAIN")
    plans = []
    for _ in range(rounds):
        try:
            # Savepoints isolate SELECT timeout/schema failures without relaxing
            # the outer transaction's READ ONLY restriction.
            with connection.transaction():
                plan = connection.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, SERIALIZE TEXT, SETTINGS, FORMAT JSON) " + query,
                    parameters,
                ).fetchone()[0][0]
            plans.append(plan)
        except psycopg.Error as exc:
            result.update(status="query_unavailable", sqlstate=exc.sqlstate, error_type=type(exc).__name__)
            break
    if len(plans) == rounds:
        result.update(status="measured", rounds=rounds,
                      median_execution_ms=statistics.median(plan["Execution Time"] for plan in plans),
                      median_planning_ms=statistics.median(plan["Planning Time"] for plan in plans))
    result["plans"] = plans
    print(f"{label}: {result['status']}", flush=True)
    return redactor.clean(result)


def audit(rounds, *, database_url, team_id, actor_user_id, rag_fetch_k=20):
    # Engine construction is lazy; only its dialect is needed for bind adapters.
    # All database work below uses the explicitly read-only psycopg connection.
    engine = create_engine(database_url)
    dialect = engine.dialect
    conninfo = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    redactor = Redactor()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "explicit database and effective team/actor; read-only plans against existing records",
        "policy": {"read_only": True, "isolation": "repeatable read", "statement_timeout": "10s",
                   "rounds": rounds, "no_initialization_or_migrations": True,
                   "no_ddl_dml_analyze_command": True, "no_seqscan_override": True,
                   "redacted": ["UUIDs", "emails", "Google subject", "embedding vector"],
                   "content_saved": False},
        "versions": {name: importlib.metadata.version(name) for name in (
            "langgraph-checkpoint-postgres", "langchain-postgres", "sqlalchemy", "psycopg")},
        "queries": [],
        "interpretation": [
            "Small existing tables can legitimately use Seq Scan; this does not establish an index defect.",
            "Plans include server output serialization but no application, LLM, network, or full HTTP latency.",
            "Repeated observations do not constitute a cold-cache benchmark; no cache or statistics were changed.",
            "Schema-blocked paths are reported, never repaired by this script.",
        ],
    }
    try:
        with psycopg.connect(conninfo, connect_timeout=10) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '10s'")
            report["transaction"] = {
                key: connection.execute("SELECT current_setting(%s)", (key,)).fetchone()[0]
                for key in ("transaction_read_only", "transaction_isolation", "statement_timeout", "enable_seqscan")
            }
            report["versions"]["postgresql"] = connection.execute("SHOW server_version").fetchone()[0]
            member = connection.execute("""
                SELECT m.user_id FROM team_members m JOIN users u ON u.id=m.user_id
                WHERE m.team_id=%s AND m.user_id=%s AND m.status='active'
                  AND m.role IN ('admin','employee') AND u.is_active
            """, (team_id, actor_user_id)).fetchone()
            if member is None:
                raise RuntimeError("The explicitly selected actor must be an effective member of the selected team")
            redactor.add(team_id, "selected_team_id")
            redactor.add(actor_user_id, "selected_actor_id")
            columns = {}
            for table, column in connection.execute(
                "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=current_schema()"
            ):
                columns.setdefault(table, set()).add(column)
            report["schema_gaps"] = {model.__table__.name: model_missing(model, columns)
                for model in (Brief, KnowledgeDocument, EvalExecutorLease, EvalRun, EvalTask, User)
                if model_missing(model, columns)}
            report["current_path_limits"] = []
            if model_missing(Brief, columns):
                report["current_path_limits"].append(
                    "Current whole-entity Brief loads in workflow ownership/start/GET require missing columns, even when the narrower list query succeeds."
                )
            if model_missing(EvalExecutorLease, columns):
                report["current_path_limits"].append(
                    "Evaluation SELECT plans below do not run recovery. Current endpoint recovery requires the missing lease table; these are query plans, not endpoint acceptance."
                )
            if model_missing(KnowledgeDocument, columns):
                report["current_path_limits"].append(
                    "Current document ORM/metadata-list paths require knowledge_documents.version; vector-search SELECT plans remain independently measurable."
                )
            scoped_counts = {
                "briefs": ("SELECT count(*) FROM briefs WHERE team_id=%s AND user_id=%s", (team_id, actor_user_id)),
                "eval_runs": ("SELECT count(*) FROM eval_runs WHERE team_id=%s AND user_id=%s", (team_id, actor_user_id)),
                "knowledge_documents": ("SELECT count(*) FROM knowledge_documents WHERE team_id=%s", (team_id,)),
                "langchain_pg_embedding": ("SELECT count(*) FROM langchain_pg_embedding WHERE cmetadata->>'team_id'=%s", (str(team_id),)),
            }
            report["row_counts"] = {table: connection.execute(query, params).fetchone()[0]
                for table, (query, params) in scoped_counts.items()
                if table in columns and (table == "langchain_pg_embedding" or "team_id" in columns[table])}
            report["indexes"] = [dict(zip(("table", "name", "definition"), row)) for row in connection.execute(
                "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() ORDER BY tablename,indexname"
            )]

            def measure(label, statement, *, unavailable=None, notes=None):
                query, params = bound_query(statement, dialect)
                report["queries"].append(explain(connection, redactor, label, query, params, rounds,
                                                unavailable=unavailable, notes=notes))

            owned = connection.execute(
                "SELECT user_id FROM briefs WHERE team_id=%s AND user_id=%s LIMIT 1",
                (team_id, actor_user_id),
            ).fetchone() if "team_id" in columns.get("briefs", set()) else None
            auth = connection.execute("SELECT id,email,google_sub FROM users WHERE id=%s", (actor_user_id,)).fetchone()
            if owned:
                user_id = owned[0]
                redactor.add(user_id, "brief_owner_id")
                measure("brief_list_previous_full_entity", select(Brief).where(
                    Brief.user_id == user_id, Brief.team_id == team_id, Brief.workflow_status.isnot(None)).order_by(Brief.updated_at.desc()),
                    unavailable=model_missing(Brief, columns), notes=["Unbounded original query; no LIMIT added."])
                if model_missing(Brief, columns):
                    measure("brief_list_existing_schema_full_row_probe", select(*[
                        column for column in Brief.__table__.columns if column.name in columns.get("briefs", set())
                    ]).where(Brief.user_id == user_id, Brief.team_id == team_id, Brief.workflow_status.isnot(None)).order_by(Brief.updated_at.desc()),
                        notes=["Full-row comparison on the existing legacy schema. Missing new fields are omitted ONLY in this probe; the current whole-entity ORM path above remains blocked."])
                with Session() as session:
                    statement = brief_list_statement(session, team_id=team_id, actor_user_id=user_id)
                measure("brief_list_current_projection", statement, notes=["Actual current service query; response fields only, still no LIMIT."])
            if auth:
                user_id, email, subject = auth
                redactor.add(user_id, "auth_user_id")
                redactor.add(email, "existing_email")
                measure("auth_user_primary_key", select(User).where(User.id == user_id).limit(1), unavailable=model_missing(User, columns))
                measure("auth_login_existing_email", select(User).where(User.email == email.strip().lower()).limit(1), unavailable=model_missing(User, columns))
                google = connection.execute("SELECT google_sub FROM users WHERE id=%s AND google_sub IS NOT NULL", (actor_user_id,)).fetchone()
                if google:
                    redactor.add(google[0], "google_subject")
                    measure("auth_google_subject", select(User).where(User.google_sub == google[0]).limit(1), unavailable=model_missing(User, columns))

            head = connection.execute("""
                SELECT c.thread_id,c.checkpoint_ns FROM checkpoints c
                JOIN briefs b ON c.thread_id=b.id::text
                WHERE b.team_id=%s AND b.user_id=%s
                GROUP BY c.thread_id,c.checkpoint_ns ORDER BY count(*) DESC LIMIT 1
            """, (team_id, actor_user_id)).fetchone() if "checkpoints" in columns else None
            if head:
                redactor.add(head[0], "checkpoint_thread_id")
                query = PostgresSaver.SELECT_SQL + "WHERE thread_id = %s AND checkpoint_ns = %s ORDER BY checkpoint_id DESC LIMIT 1"
                report["queries"].append(explain(connection, redactor, "checkpoint_head_complete_installed_sql", query, head, rounds))

            owner = connection.execute(
                "SELECT user_id FROM eval_runs WHERE team_id=%s AND user_id=%s LIMIT 1",
                (team_id, actor_user_id),
            ).fetchone() if "team_id" in columns.get("eval_runs", set()) else None
            if owner:
                eval_user = owner[0]
                redactor.add(eval_user, "evaluation_owner_id")
                run_ids = [row[0] for row in connection.execute(
                    "SELECT id FROM eval_runs WHERE user_id=%s AND team_id=%s ORDER BY created_at DESC", (eval_user, team_id),
                )]
                for index, run_id in enumerate(run_ids):
                    redactor.add(run_id, f"run_id_{index + 1}")
                measure("evaluation_run_list", select(EvalRun).where(
                    EvalRun.user_id == eval_user, EvalRun.team_id == team_id,
                ).order_by(EvalRun.created_at.desc()), unavailable=model_missing(EvalRun, columns))
                with Session() as session:
                    original = session.query(EvalTask).filter(EvalTask.run_id == run_ids[0]).subquery()
                    done = session.query(EvalTask).filter(EvalTask.run_id == run_ids[0], EvalTask.execution_status.notin_(("pending", "running"))).subquery()
                measure("evaluation_original_single_run_total_count", select(func.count()).select_from(original), unavailable=model_missing(EvalTask, columns))
                measure("evaluation_original_single_run_done_count", select(func.count()).select_from(done), unavailable=model_missing(EvalTask, columns))
                measure("evaluation_current_batched_counts", select(
                    EvalTask.run_id, func.count().label("total"),
                    func.count().filter(EvalTask.execution_status.notin_(("pending", "running"))).label("done"),
                ).where(EvalTask.run_id == any_(bindparam("run_ids", run_ids, type_=ARRAY(PG_UUID(as_uuid=True))))).group_by(EvalTask.run_id))

            vector_row = connection.execute("""
                SELECT e.collection_id,e.cmetadata->>'brand_id',e.embedding::text
                FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON c.uuid=e.collection_id
                JOIN brands b ON b.id::text=e.cmetadata->>'brand_id'
                WHERE c.name='brand_knowledge' AND e.embedding IS NOT NULL AND e.cmetadata->>'brand_id' IS NOT NULL
                  AND e.cmetadata->>'team_id'=%s AND b.team_id=%s
                ORDER BY e.id LIMIT 1
            """, (str(team_id), team_id)).fetchone() if "langchain_pg_embedding" in columns else None
            if vector_row:
                collection_id, brand_id, vector_text = vector_row
                vector = json.loads(vector_text)
                redactor.add(collection_id, "vector_collection_id")
                redactor.add(brand_id, "vector_brand_id")
                redactor.add(vector_text, f"embedding_{len(vector)}_dimensions")
                report["vector_parameter"] = {"source": "one existing team embedding", "dimensions": len(vector), "values": "redacted", "k": rag_fetch_k}
                # Mapping creation and __new__ do not run PGVector.__init__ or
                # setup. Only its installed pure SQL expression helpers run.
                embedding_table, collection_table = _get_embedding_collection_store()
                store = object.__new__(PGVector)
                store.EmbeddingStore, store.CollectionStore = embedding_table, collection_table
                store._distance_strategy, store.use_jsonb = DistanceStrategy.COSINE, True
                for label, filters in (("rag_original_jsonpath_exact_cosine", {"brand_id": brand_id, "team_id": str(team_id)}),
                                       ("rag_current_text_in_exact_cosine", {"brand_id": {"$in": [brand_id]}, "team_id": {"$in": [str(team_id)]}})):
                    statement = select(embedding_table, store.distance_strategy(vector).label("distance")).filter(
                        embedding_table.collection_id == collection_id, store._create_filter_clause(filters),
                    ).order_by(asc("distance")).join(
                        collection_table, embedding_table.collection_id == collection_table.uuid,
                    ).limit(rag_fetch_k)
                    measure(label, statement, notes=["Full installed native PGVector SELECT shape; exact cosine, no approximate index and no PGVector initialization."])
            connection.rollback()
    finally:
        engine.dispose()
    # Run the final pass after every sampled identity has been registered, so an
    # earlier plan cannot retain an identity discovered by a later query.
    return redactor.clean(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="Explicit PostgreSQL target; never inferred from .env")
    parser.add_argument("--team-id", required=True, type=UUID)
    parser.add_argument("--actor-user-id", required=True, type=UUID)
    parser.add_argument("--rag-fetch-k", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=3, choices=range(3, 6))
    parser.add_argument("--output", type=Path, default=ROOT / "docs/index-audit/business-plans.json")
    args = parser.parse_args()
    report = audit(
        args.rounds, database_url=args.database_url, team_id=args.team_id,
        actor_user_id=args.actor_user_id, rag_fetch_k=args.rag_fetch_k,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(f"Saved redacted query plans to {args.output}", flush=True)


if __name__ == "__main__":
    main()
