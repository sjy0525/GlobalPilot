"""Reproduce the eval run-list audit against an explicit disposable PostgreSQL DB.

Example (the database must already exist):
  EVAL_INDEX_AUDIT_DATABASE_URL=... python scripts/audit_eval_indexes.py \
    --confirm-disposable-database --output docs/index-audit/eval.json

This creates and drops one random schema, seeds synthetic data, and benchmarks
the real ORM service. It never loads the application database or calls providers.
The reported service timings exclude authentication and lease recovery, which
are identical across variants. PostgreSQL chooses its normal scan strategies.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from statistics import median
import sys
import time
import types
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db.base import Base  # noqa: E402
from app.modules.evaluation.models import EvalCase, EvalExecutorLease, EvalRun, EvalTask, EvalTestSet  # noqa: E402
from app.modules.users.models import User  # noqa: E402
from app.modules.teams.models import Team  # noqa: E402

AUDIT_TEAM_ID = UUID(hashlib.md5(b"eval-index-audit-team").hexdigest())


def load_service(engine):
    """Use the current service without importing configured databases/providers."""
    database = types.ModuleType("app.db.database")
    database.sessionLocal = sessionmaker(bind=engine, autoflush=False)
    runner = types.ModuleType("app.modules.evaluation.engine.runner")
    versions = types.ModuleType("app.modules.evaluation.engine.versions")
    authorization = types.ModuleType("app.modules.teams.authorization")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("The query audit must not run workers or external providers")

    for name in ("_fail_run", "launch", "recover_expired_run", "recover_expired_runs", "refresh_run_artifacts"):
        setattr(runner, name, forbidden)
    for name in ("provenance_snapshot", "workflow_version"):
        setattr(versions, name, forbidden)
    authorization.require_active_team_member = forbidden
    authorization.lock_team_and_require_member = forbidden
    with patch.dict(sys.modules, {
        "app.db.database": database, "app.modules.evaluation.engine.runner": runner, "app.modules.evaluation.engine.versions": versions,
        "app.modules.teams.authorization": authorization,
    }):
        spec = importlib.util.spec_from_file_location("eval_index_audit_service", ROOT / "app/modules/evaluation/service.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    # Query timings deliberately exclude this existing, unchanged preflight.
    module._recover_before_read = lambda _db, user, *, team_id: user.id
    return module


def seed(engine, runs, users, cases):
    hot = runs // 5
    warm = runs // 20
    with engine.begin() as db:
        db.execute(Team.__table__.insert().values(id=AUDIT_TEAM_ID, name="Synthetic evaluation team"))
        db.execute(text("""
            INSERT INTO users (id,email,created_at,updated_at)
            SELECT md5('eval-user-' || i)::uuid, 'synthetic-' || i || '@example.invalid',
                   TIMESTAMP '2026-01-01', TIMESTAMP '2026-01-01'
            FROM generate_series(1,:users) AS i
        """), {"users": users})
        db.execute(text("""
            INSERT INTO eval_test_sets (id,version,name,description,created_at)
            VALUES (md5('eval-test-set')::uuid,'synthetic-v1','synthetic audit','No business data',TIMESTAMP '2026-01-01')
        """))
        db.execute(text("""
            INSERT INTO eval_cases
                (id,test_set_id,code,ordinal,title,user_request,brand_profile,brand_name,
                 platforms,must_include,forbidden_claims,region,persona)
            SELECT md5('eval-case-' || i)::uuid, md5('eval-test-set')::uuid, 'C' || i, i,
                   'Synthetic case ' || i,repeat('request ',20),repeat('profile ',30),'synthetic',
                   '[]'::jsonb,'[]'::jsonb,'[]'::jsonb,'synthetic','synthetic'
            FROM generate_series(1,:cases) AS i
        """), {"cases": cases})
        db.execute(text("""
            INSERT INTO eval_runs
                (id,team_id,user_id,test_set_id,label,status,is_baseline,provenance,summary,report_markdown,created_at)
            SELECT md5('eval-run-' || i)::uuid,
                   :team_id,
                   md5('eval-user-' || CASE WHEN i <= :hot THEN 1 WHEN i <= :hot + :warm THEN 2
                        WHEN i <= :hot + :warm + 5 THEN 3 ELSE 4 + (i % (:users - 3)) END)::uuid,
                   md5('eval-test-set')::uuid,'Synthetic run ' || i,
                   CASE WHEN i = :hot THEN 'running' ELSE 'completed' END,FALSE,
                   jsonb_build_object('synthetic',TRUE,'test_set_version','synthetic-v1','padding',repeat('p',512)),
                   jsonb_build_object('qualified_count',:cases),repeat('Synthetic report. ',100),
                   TIMESTAMP '2026-01-01' + i * INTERVAL '1 minute'
            FROM generate_series(1,:runs) AS i
        """), {"runs": runs, "users": users, "hot": hot, "warm": warm, "cases": cases, "team_id": AUDIT_TEAM_ID})
        db.execute(text("""
            INSERT INTO eval_tasks
                (id,run_id,case_id,execution_status,empty_output,timed_out,revision_count,qualified,
                 generated_content,strategy,retrieved_context,scores,auto_scores,failure_reasons)
            SELECT md5('eval-task-' || r || '-' || c)::uuid,md5('eval-run-' || r)::uuid,
                   md5('eval-case-' || c)::uuid,
                   CASE WHEN r <> :hot OR c <= :cases / 3 THEN 'completed'
                        WHEN c = :cases / 3 + 1 THEN 'running' ELSE 'pending' END,
                   FALSE,FALSE,0,TRUE,jsonb_build_object('body',repeat('synthetic ',100)),
                   repeat('strategy ',40),repeat('retrieved context ',40),
                   '{"facts":{"verdict":"pass"}}'::jsonb,'{"facts":{"verdict":"pass"}}'::jsonb,'[]'::jsonb
            FROM generate_series(1,:runs) AS r CROSS JOIN generate_series(1,:cases) AS c
        """), {"runs": runs, "cases": cases, "hot": hot})
    # Stabilize visibility and statistics before measuring. This is scoped to
    # the disposable schema, never the surrounding database's other tables.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        for name in ("users", "eval_test_sets", "eval_cases", "eval_runs", "eval_tasks"):
            db.exec_driver_sql(f"VACUUM (ANALYZE) {name}")
    return {"hot": hot, "warm": warm, "cold": 5}


def query(service, db, user_id, variant):
    if variant == "batched":
        return service.list_runs(db, types.SimpleNamespace(id=user_id), team_id=AUDIT_TEAM_ID)
    # The exact pre-fix ORM shape, with the current response serializer. Counts
    # are passed explicitly so this branch retains the old two-count behavior.
    runs = db.query(EvalRun).filter(
        EvalRun.user_id == user_id, EvalRun.team_id == AUDIT_TEAM_ID,
    ).order_by(EvalRun.created_at.desc()).all()
    result = []
    for run in runs:
        total = db.query(EvalTask).filter(EvalTask.run_id == run.id).count()
        done = db.query(EvalTask).filter(
            EvalTask.run_id == run.id, EvalTask.execution_status.notin_(("pending", "running")),
        ).count()
        result.append(service._run_out(db, run, (total, done)))
    return result


def measured_query(engine, service, user_id, variant):
    statements = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    with Session(engine, autoflush=False) as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        event.listen(engine, "before_cursor_execute", capture)
        try:
            started = time.perf_counter()
            result = query(service, db, user_id, variant)
            elapsed_ms = (time.perf_counter() - started) * 1000
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    signature = [(str(row.id), row.task_total, row.task_done) for row in result]
    return elapsed_ms, statements, hashlib.sha256(json.dumps(signature).encode()).hexdigest()


def explain(engine, statements):
    totals = defaultdict(float)
    samples = {}
    with engine.connect() as db:
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        cursor = db.connection.driver_connection.cursor()
        try:
            for statement, parameters in statements:
                cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters)
                plan = cursor.fetchone()[0][0]
                totals["execution_time_ms"] += plan["Execution Time"]
                totals["planning_time_ms"] += plan["Planning Time"]
                # Root buffers already include children; summing every plan
                # node would count the same accesses more than once.
                for key in ("Shared Hit Blocks", "Shared Read Blocks", "Temp Read Blocks", "Temp Written Blocks"):
                    totals[key] += plan["Plan"].get(key, 0)
                samples.setdefault(statement, {"parameters": parameters, "plan": plan})
        finally:
            cursor.close()
    return dict(totals), list(samples.values())


def workload(engine, service, user_id, variant, rounds):
    # Each stage gets one warm-up. Shared buffers need not fit the whole table;
    # EXPLAIN records those reads, and no OS or PostgreSQL caches are cleared.
    measured_query(engine, service, user_id, variant)
    measurements = []
    grouped = {}
    signature = None
    for number in range(rounds):
        elapsed, statements, current_signature = measured_query(engine, service, user_id, variant)
        if signature is not None and current_signature != signature:
            raise AssertionError("Synthetic rows changed during the benchmark")
        signature = current_signature
        explained, samples = explain(engine, statements)
        measurements.append({
            "orm_elapsed_ms": elapsed, "business_select_count": len(statements),
            "explain_totals": explained, "representative_plans": samples,
        })
        if number == 0:
            for statement, parameters in statements:
                entry = grouped.setdefault(statement, {"sql": statement, "parameter_sets": []})
                entry["parameter_sets"].append(parameters)
    return {
        "count_result_sha256": signature,
        "median_orm_elapsed_ms": median(row["orm_elapsed_ms"] for row in measurements),
        "business_select_count": measurements[0]["business_select_count"],
        "median_explain_totals": {
            key: median(row["explain_totals"][key] for row in measurements)
            for key in measurements[0]["explain_totals"]
        },
        "captured_orm_queries": list(grouped.values()),
        "rounds": measurements,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable-database", action="store_true", required=True)
    parser.add_argument("--database-url", help="Or set EVAL_INDEX_AUDIT_DATABASE_URL; never reads .env")
    parser.add_argument("--runs", type=int, default=10000)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--cases", type=int, default=30)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/index-audit/eval.json")
    args = parser.parse_args(argv)
    raw_url = args.database_url or os.environ.get("EVAL_INDEX_AUDIT_DATABASE_URL")
    if not raw_url:
        parser.error("An explicit disposable database URL is required")
    if args.runs < 1000 or args.users < 4 or args.cases < 3 or not 3 <= args.rounds <= 5:
        parser.error("Require runs >= 1000, users >= 4, cases >= 3, and 3-5 rounds")
    try:
        url = make_url(raw_url)
        if (url.get_backend_name() != "postgresql" or not (url.database or "").startswith(("test_", "audit_", "refactor_"))
                or any(key.lower() in {"dbname", "database", "service", "servicefile"} for key in url.query)):
            raise ValueError("Explicitly named disposable PostgreSQL required")
        url = url.difference_update_query(["options"])
        url = url.set(drivername="postgresql+psycopg")
    except Exception:
        parser.error("Use a PostgreSQL test_, audit_ or refactor_ database without target overrides")
    admin = create_engine(url, echo=False, hide_parameters=True)
    schema = "eval_index_audit_" + uuid4().hex
    engine = None
    created = False
    try:
        with admin.begin() as db:
            db.execute(text(f"CREATE SCHEMA {schema}"))
        created = True
        engine = create_engine(url, echo=False, hide_parameters=True, connect_args={
            "options": f"-csearch_path={schema} -cstatement_timeout=120000 -clock_timeout=5000",
        })
        tables = [User.__table__, Team.__table__, EvalTestSet.__table__, EvalCase.__table__, EvalRun.__table__, EvalTask.__table__, EvalExecutorLease.__table__]
        Base.metadata.create_all(engine, tables=tables)
        # Preserve the historical baseline if a measured candidate is later
        # adopted into the ORM model. Only this freshly created schema is touched.
        quote = engine.dialect.identifier_preparer.quote
        with engine.begin() as db:
            for table, columns in (
                ("eval_runs", ["user_id", "created_at", "id"]),
                ("eval_tasks", ["run_id", "execution_status"]),
            ):
                for index in inspect(db).get_indexes(table):
                    if index["column_names"] == columns and not index["unique"]:
                        db.exec_driver_sql(f"DROP INDEX {quote(index['name'])}")
        distribution = seed(engine, args.runs, args.users, args.cases)
        service = load_service(engine)
        with engine.connect() as db:
            server = db.scalar(text("SELECT version()"))
            settings = dict(db.execute(text("SELECT name,setting FROM pg_settings WHERE name IN ('shared_buffers','work_mem','random_page_cost','effective_cache_size','max_parallel_workers_per_gather','enable_seqscan')")).all())
            existing_indexes = [dict(row) for row in db.execute(text("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=:schema ORDER BY tablename,indexname"), {"schema": schema}).mappings()]
            user_ids = {name: db.scalar(text("SELECT md5('eval-user-' || :number)::uuid"), {"number": number}) for number, name in enumerate(("hot", "warm", "cold"), start=1)}
        candidates = {
            "run_order": "CREATE INDEX ix_eval_runs_user_created_audit ON eval_runs (user_id, created_at DESC, id DESC)",
            "task_status": "CREATE INDEX ix_eval_tasks_run_status_audit ON eval_tasks (run_id, execution_status)",
        }
        report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_kind": "synthetic_only_no_business_data",
            "dataset": {"users": args.users, "runs": args.runs, "cases_per_run": args.cases, "tasks": args.runs * args.cases, "selected_user_run_counts": distribution, "large_fields": "compressible synthetic report/content placeholders; not a production payload distribution"},
            "method": {"rounds": args.rounds, "cache": "one warm-up per variant; no cache eviction; shared-buffer reads may remain; not a cold-disk benchmark", "visibility": "VACUUM ANALYZE after seed; static history can use index-only scans; concurrent production writes may cause heap fetches", "explains": "Every captured SELECT is replayed with EXPLAIN ANALYZE BUFFERS FORMAT JSON every round; representative plan per SQL shape retained", "timing_scope": "real ORM reads and response construction; excludes authentication, lease recovery, HTTP and JSON encoding", "buffers": "sum root-node buffers across statements, without double-counting child nodes", "scan_policy": "default PostgreSQL optimizer; enable_seqscan is not changed"},
            "postgres_version": server, "postgres_settings": settings, "existing_indexes": existing_indexes,
            "candidate_index_sql": candidates,
            "source_queries": {"list": "app/modules/evaluation/service.py:list_runs", "old_counts": "pre-fix _run_out: Query(EvalTask).filter(run_id).count() twice", "new_counts": "app/modules/evaluation/service.py:_task_counts", "baseline": "Model-created tables, excluding the two candidate index column signatures if later adopted into the ORM"},
            "variants": {},
        }
        report["candidate_index_sizes_bytes"] = {}
        for index_variant in ("existing_indexes", "run_order", "task_status"):
            if index_variant != "existing_indexes":
                with engine.begin() as db:
                    if index_variant == "task_status":
                        db.exec_driver_sql("DROP INDEX ix_eval_runs_user_created_audit")
                    db.exec_driver_sql(candidates[index_variant])
                    db.exec_driver_sql("ANALYZE eval_runs")
                    db.exec_driver_sql("ANALYZE eval_tasks")
                    candidate_name = "ix_eval_runs_user_created_audit" if index_variant == "run_order" else "ix_eval_tasks_run_status_audit"
                    report["candidate_index_sizes_bytes"][index_variant] = db.scalar(text("SELECT pg_relation_size(CAST(:name AS regclass))"), {"name": candidate_name})
            for variant in ("original", "batched"):
                label = variant + "_" + index_variant
                report["variants"][label] = {}
                for name, user_id in user_ids.items():
                    result = workload(engine, service, user_id, variant, args.rounds)
                    report["variants"][label][name] = result
                    print(json.dumps({"variant": label, "user": name, "selects": result["business_select_count"], "median_orm_ms": round(result["median_orm_elapsed_ms"], 3)}), flush=True)
        for name in user_ids:
            if len({variant[name]["count_result_sha256"] for variant in report["variants"].values()}) != 1:
                raise AssertionError("Old and new task counts differ")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        print(f"Audit saved to {args.output}", flush=True)
    except Exception as exc:
        print(f"Audit failed ({type(exc).__name__}); no database URL or driver details printed", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as db:
                db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
