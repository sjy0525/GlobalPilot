"""Reproducible Brief/Auth/PostgresSaver index audit; never reads app DB settings.

Example (PostgreSQL 17+, disposable local cluster only):
  .venv/bin/python scripts/audit_workflow_indexes.py --admin-url "$AUDIT_ADMIN_URL"

Creates refactor_indexes_workflow, refusing an existing DB unless --reset is
explicitly passed. Benchmark-only indexes stay in that isolated database.
"""

import argparse
import hashlib
import importlib.metadata
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import Base
from app.modules.brands.models import Brand
from app.modules.briefs.models import Brief
from app.modules.users.models import User
from app.modules.teams.models import Team
from scripts._query_audit_scope import brief_list_statement


def identity(label):
    return UUID(hashlib.md5(label.encode()).hexdigest())


def compile_query(statement):
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def brief_query(user_id, projected):
    team_id = identity("workflow-audit-team")
    with Session() as session:
        if projected:
            # Capture the actual service query without executing it. The baseline
            # below is its previous whole-entity SELECT, with identical predicates.
            statement = brief_list_statement(session, team_id=team_id, actor_user_id=user_id)
        else:
            statement = select(Brief).where(
                Brief.user_id == user_id, Brief.team_id == team_id, Brief.workflow_status.isnot(None),
            ).order_by(Brief.updated_at.desc())
    return compile_query(statement)


def value_bytes(value):
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, (dict, list)):
        return len(json.dumps(value, ensure_ascii=False, default=str).encode())
    return len(str(value).encode())


def measure(connection, label, query, params, rounds):
    # No enable_seqscan overrides, cache flushing, or fabricated LIMIT clauses.
    # SERIALIZE accounts for TOAST/output conversion that normal EXPLAIN omits.
    explain = "EXPLAIN (ANALYZE, BUFFERS, SERIALIZE TEXT, SETTINGS, FORMAT JSON) " + query
    first_plan = connection.execute(explain, params).fetchone()[0][0]
    plans, fetch_ms, row_count, payload_bytes = [], [], 0, 0
    for iteration in range(rounds):
        plans.append(connection.execute(explain, params).fetchone()[0][0])
        started = time.perf_counter()
        rows = connection.execute(query, params).fetchall()
        fetch_ms.append((time.perf_counter() - started) * 1000)
        row_count = len(rows)
        if iteration == 0:
            # Application-value size, not an assertion about protocol wire size.
            payload_bytes = sum(value_bytes(value) for row in rows for value in row)
    result = {
        "label": label, "sql": query, "parameters": params, "rounds": rounds,
        "first_observed_plan": first_plan, "plans": plans,
        "median_execution_ms": statistics.median(plan["Execution Time"] for plan in plans),
        "median_planning_ms": statistics.median(plan["Planning Time"] for plan in plans),
        "fetch_all_wall_ms": fetch_ms, "median_fetch_all_wall_ms": statistics.median(fetch_ms),
        "row_count": row_count, "materialized_value_bytes": payload_bytes,
    }
    print(f"{label}: rows={row_count} explain={result['median_execution_ms']:.3f}ms fetch={result['median_fetch_all_wall_ms']:.3f}ms", flush=True)
    return result


def seed_application(connection):
    connection.execute("""
        INSERT INTO teams(id,name,create_at)
        VALUES(md5('workflow-audit-team')::uuid,'Synthetic workflow team',timestamp '2026-01-01')
    """)
    connection.execute("""
        INSERT INTO users(id,email,password_hash,google_sub,nickname,created_at,updated_at)
        SELECT md5('user-'||g)::uuid,'person'||g||'@example.invalid','test-only-hash',
            CASE WHEN g % 4 = 0 THEN 'google-sub-'||g END,'Person '||g,
            timestamp '2026-01-01',timestamp '2026-01-01'
        FROM generate_series(1,20000) g
    """)
    connection.execute("""
        INSERT INTO brands(id,team_id,name,created_at)
        VALUES(md5('brand-1')::uuid,md5('workflow-audit-team')::uuid,'Audit brand',timestamp '2026-01-01')
    """)
    connection.execute("""
        INSERT INTO briefs(
            id,team_id,user_id,brand_id,goal,selling_points,region,user_persona,social_media,
            purchase_channels,competitor_url,workflow_status,workflow_version,
            workflow_checkpoint_id,strategy,content,final_output,revision_count,created_at,updated_at
        )
        SELECT md5('brief-'||g)::uuid,md5('workflow-audit-team')::uuid,
            md5('user-'||CASE WHEN g<=6000 THEN 1 WHEN g<=12000 THEN 2
                 WHEN g<=12005 THEN 3 ELSE 4+g%200 END)::uuid,
            md5('brand-1')::uuid,'Campaign '||g,left(payload,160),'eu-na','Readers',ARRAY['linkedin'],
            'Web',NULL,
            CASE WHEN (g<=6000 AND g%4<>0) OR (g>6000 AND g<=12000 AND g%100=0)
                    OR (g>12000 AND g<=12005) OR (g>12005 AND g%3<>0)
                 THEN CASE WHEN g%5=0 THEN 'awaiting_review' ELSE 'completed' END END,
            3,'synthetic-checkpoint-'||g,left(payload,512),
            jsonb_build_object('body',payload||reverse(payload)),payload||reverse(payload),
            g%3,timestamp '2026-01-01',timestamp '2026-01-01'+(g/4)*interval '1 minute'
        FROM generate_series(1,40000) g
        CROSS JOIN LATERAL (
            SELECT string_agg(md5(g::text||':'||part::text),'') payload FROM generate_series(1,32) part
        ) p
    """)
    for table in ("users", "brands", "briefs"):
        connection.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(table)))


def seed_checkpoints(connection):
    PostgresSaver(connection).setup()
    # The installed saver schema and full SELECT are real. History is synthetic:
    # 5,000 versions for one hot thread; 100 each for 200 ordinary threads.
    connection.execute("""
        INSERT INTO checkpoints(thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,checkpoint,metadata)
        SELECT 'audit-thread-'||thread,'',
            '1f000000-0000-6000-8000-'||lpad(version::text,12,'0'),
            CASE WHEN version>1 THEN '1f000000-0000-6000-8000-'||lpad((version-1)::text,12,'0') END,
            jsonb_build_object('v',4,'id','1f000000-0000-6000-8000-'||lpad(version::text,12,'0'),
              'ts','2026-01-01T00:00:00+00:00','channel_values','{}'::jsonb,'versions_seen','{}'::jsonb,
              'channel_versions',jsonb_build_object('context',version::text,'retrieved_context',version::text,
                 'strategy',version::text,'content',version::text,'self_review',version::text,'feedback_log',version::text)),
            jsonb_build_object('source','loop','step',version)
        FROM generate_series(0,200) thread
        CROSS JOIN LATERAL generate_series(1,CASE WHEN thread=0 THEN 5000 ELSE 100 END) version
    """)
    connection.execute("""
        INSERT INTO checkpoint_blobs(thread_id,checkpoint_ns,channel,version,type,blob)
        SELECT thread_id,checkpoint_ns,channel,version,'json',
            convert_to(to_json(repeat(md5(thread_id||channel||version),16))::text,'UTF8')
        FROM checkpoints CROSS JOIN LATERAL jsonb_each_text(checkpoint->'channel_versions') AS cv(channel,version)
    """)
    connection.execute("""
        INSERT INTO checkpoint_writes(thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,blob,task_path)
        SELECT thread_id,checkpoint_ns,checkpoint_id,'task-'||task,0,
            CASE WHEN task=1 THEN 'content' ELSE '__interrupt__' END,'json',
            convert_to(to_json(repeat(md5(thread_id||checkpoint_id||task),8))::text,'UTF8'),'~node'
        FROM checkpoints CROSS JOIN generate_series(1,2) task
    """)
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        connection.execute(sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(table)))


def index_inventory(connection):
    return [dict(zip(("table", "index", "ddl", "bytes"), row)) for row in connection.execute("""
        SELECT tablename,indexname,indexdef,pg_relation_size(indexname::regclass)
        FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname
    """).fetchall()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", required=True, help="Explicit disposable PostgreSQL admin database URL")
    parser.add_argument("--database", default="refactor_indexes_workflow")
    parser.add_argument("--rounds", type=int, default=5, choices=range(3, 11))
    parser.add_argument("--reset", action="store_true", help="Drop/recreate only the named refactor_indexes_* audit DB")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/index-audit/workflow.json")
    args = parser.parse_args()
    admin = make_url(args.admin_url).set(drivername="postgresql")
    if not args.database.startswith("refactor_indexes_") or args.database == admin.database:
        parser.error("Database must be a separate refactor_indexes_* disposable database")
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as connection:
        exists = connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (args.database,)).fetchone()
        if exists and not args.reset:
            parser.error("Audit database exists; explicitly pass --reset to recreate it")
        if exists:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(args.database)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.database)))
    database = admin.set(database=args.database)
    url = database.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    engine = create_engine(url)
    Base.metadata.create_all(engine, tables=[User.__table__, Team.__table__, Brand.__table__, Brief.__table__])
    engine.dispose()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": database.render_as_string(hide_password=True),
        "package": {"langgraph-checkpoint-postgres": importlib.metadata.version("langgraph-checkpoint-postgres")},
        "method": {
            "rounds": args.rounds, "explain": "ANALYZE BUFFERS SERIALIZE TEXT SETTINGS FORMAT JSON",
            "cache": "One observed first plan, followed by repeated warm executions; no OS/PG cache reset. Hot/cold describe user cardinality, not cold buffers.",
            "fetch": "Real psycopg execute+fetchall, including decoding. Materialized bytes estimate application values, not protocol bytes.",
            "limits": "Synthetic isolated workload, not production latency. Actual Brief query has NO LIMIT in every variant. No enable_seqscan overrides.",
        },
        "brief_cases": [], "auth_cases": [], "checkpoint_cases": [],
    }
    with psycopg.connect(database.render_as_string(hide_password=False), autocommit=True) as connection:
        # Keep the historical baseline reproducible after this recommendation is
        # added to application metadata. This is only the newly created audit DB.
        connection.execute("DROP INDEX IF EXISTS ix_briefs_user_updated_active")
        report["method"]["baseline"] = "Application schema with the newly recommended Brief partial index omitted; candidate is added only between measurement phases."
        report["method"]["psycopg_prepare_threshold"] = connection.prepare_threshold
        report["server_version"] = connection.execute("SELECT version()").fetchone()[0]
        report["settings"] = dict(connection.execute("SELECT name,setting FROM pg_settings WHERE name IN ('work_mem','shared_buffers','effective_cache_size','random_page_cost','enable_seqscan')").fetchall())
        print("Seeding 20,000 users and 40,000 briefs", flush=True)
        seed_application(connection)
        print("Seeding 25,000 checkpoints, 150,000 blobs and 50,000 writes", flush=True)
        seed_checkpoints(connection)
        report["initial_indexes"] = index_inventory(connection)
        report["row_counts"] = {table: connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]
            for table in ("users", "brands", "briefs", "checkpoints", "checkpoint_blobs", "checkpoint_writes")}
        profiles = {"hot_user": identity("user-1"), "null_heavy_user": identity("user-2"), "cold_user": identity("user-3")}
        report["brief_distribution"] = {label: dict(zip(("total", "active", "null_status"), connection.execute(
            "SELECT count(*),count(*) FILTER(WHERE workflow_status IS NOT NULL),count(*) FILTER(WHERE workflow_status IS NULL) FROM briefs WHERE user_id=%s", (user_id,)
        ).fetchone())) for label, user_id in profiles.items()}
        for indexed in (False, True):
            if indexed:
                index_sql = "CREATE INDEX audit_briefs_user_updated_active ON briefs(user_id, updated_at DESC) WHERE workflow_status IS NOT NULL"
                connection.execute(index_sql)
                report["candidate_index_sql"] = index_sql
                connection.execute("ANALYZE briefs")
            for projected in (False, True):
                for label, user_id in profiles.items():
                    query, params = brief_query(user_id, projected)
                    report["brief_cases"].append(measure(connection,
                        f"{label}/{'partial_index' if indexed else 'existing_indexes'}/{'projection' if projected else 'full_entity'}",
                        query, params, args.rounds))
        user_queries = {
            "current_user_by_pk": select(User).where(User.id == identity("user-1")).limit(1),
            "login_by_email": select(User).where(User.email == "person1@example.invalid").limit(1),
            "google_login_by_subject": select(User).where(User.google_sub == "google-sub-4").limit(1),
            "unknown_email": select(User).where(User.email == "absent@example.invalid").limit(1),
        }
        for label, statement in user_queries.items():
            query, params = compile_query(statement)
            report["auth_cases"].append(measure(connection, label, query, params, args.rounds))
        query = PostgresSaver.SELECT_SQL + "WHERE thread_id = %s AND checkpoint_ns = %s ORDER BY checkpoint_id DESC LIMIT 1"
        for label, thread in (("hot_thread_5000_versions", "audit-thread-0"), ("ordinary_thread_100_versions", "audit-thread-1"), ("missing_thread", "audit-thread-missing")):
            report["checkpoint_cases"].append(measure(connection, label, query, (thread, ""), args.rounds))
        report["final_indexes"] = index_inventory(connection)
        report["relation_sizes"] = {table: connection.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0]
            for table in report["row_counts"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(f"Saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
