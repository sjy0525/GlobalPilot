"""Recover pre-lease evaluations only after every old worker has been stopped.

Supply --database-url or GLOBALPILOT_RECOVERY_DATABASE_URL explicitly. This
command never reads the application's .env, starts workers, or deletes history.
The schema migration adding eval_executor_leases must already be installed.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys


def _run_recovery(database_url):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    from app.modules.evaluation.engine.legacy_recovery import recover_legacy_evaluations
    from app.modules.users.models import User  # noqa: F401: register the users foreign-key target.

    engine = create_engine(
        database_url, poolclass=NullPool, echo=False, hide_parameters=True,
        isolation_level="READ COMMITTED", connect_args={"connect_timeout": 10},
    )
    try:
        with Session(engine, autoflush=False) as db, db.begin():
            result = recover_legacy_evaluations(db, workers_stopped=True)
        return result
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-workers-stopped", action="store_true", required=True,
        help="确认所有旧版评测 worker 已停止；不允许对仍运行的旧执行器自动回收",
    )
    parser.add_argument(
        "--database-url", help="显式 PostgreSQL URL；也可使用 GLOBALPILOT_RECOVERY_DATABASE_URL",
    )
    args = parser.parse_args(argv)
    raw_url = args.database_url or os.environ.get("GLOBALPILOT_RECOVERY_DATABASE_URL")
    if not raw_url:
        parser.error("必须显式提供 --database-url 或 GLOBALPILOT_RECOVERY_DATABASE_URL")

    from sqlalchemy.engine import make_url

    try:
        url = make_url(raw_url)
        if url.get_backend_name() != "postgresql":
            raise ValueError("PostgreSQL required")
        url = url.set(drivername="postgresql+psycopg")
    except Exception:
        parser.error("数据库 URL 无效；只支持 PostgreSQL")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.modules.evaluation.engine.legacy_recovery import ActiveEvaluationLease

    try:
        result = _run_recovery(url)
    except ActiveEvaluationLease:
        print("拒绝恢复：存在未过期的评测租约，请先停止 worker 并等待租约过期。", file=sys.stderr)
        return 1
    except Exception as exc:
        # Driver exceptions can contain DSNs/passwords. Never print the
        # exception or traceback; retain only its non-sensitive type name.
        print(f"恢复未确认成功（{type(exc).__name__}）；请检查连接和数据库迁移状态。", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
