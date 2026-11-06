"""Evaluation persistence shared by live executors and offline recovery.

These helpers use only the caller's Session and never commit independently.
Callers acquire the global slot (when executing/recovering), then run and task
locks. Importing this module does not initialize providers or application DBs.
"""
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.modules.evaluation.engine.lease import clear_locked_slot, lease_is_live
from app.modules.evaluation.engine.metrics import TaskMetric, summarize
from app.modules.evaluation.engine.report import render_report
from app.modules.evaluation.engine.scoring import blocked_scores, failure_reasons
from app.modules.evaluation.models import EvalCase, EvalRun, EvalTask


def _mark_run_failed(db, run: EvalRun, reason: str) -> None:
    tasks = db.scalars(
        select(EvalTask)
        .where(EvalTask.run_id == run.id, EvalTask.execution_status.in_(("pending", "running")))
        .order_by(EvalTask.id).with_for_update()
    ).all()
    for task in tasks:
        scores = blocked_scores(reason)
        task.execution_status = "error"
        task.qualified = False
        task.error = reason
        task.scores = scores
        task.auto_scores = scores
        task.failure_reasons = failure_reasons(scores)
    run.status = "failed"
    run.error = reason
    run.finished_at = datetime.now()
    refresh_run_artifacts(db, run)


def recover_expired_run(db, lease) -> bool:
    """Caller holds the global lease lock; live owners are never recovered."""
    if lease.owner is None or lease_is_live(db, lease):
        return False
    run = db.scalar(
        select(EvalRun).where(EvalRun.id == lease.run_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is not None and run.status in {"queued", "running"}:
        _mark_run_failed(db, run, "评测执行租约过期，任务已中断")
    clear_locked_slot(db, lease)
    return True


def task_metrics(db, run_id: UUID) -> tuple[list[TaskMetric], list[dict]]:
    rows = (
        db.query(EvalTask, EvalCase)
        .join(EvalCase, EvalCase.id == EvalTask.case_id)
        .filter(EvalTask.run_id == run_id)
        .order_by(EvalCase.ordinal)
        .all()
    )
    metrics: list[TaskMetric] = []
    views: list[dict] = []
    for task, case in rows:
        scores = task.scores or {}
        facts = (scores.get("facts") or {}).get("verdict") or ""
        reasons = list(task.failure_reasons or [])
        cost = None if task.api_cost_usd is None else float(task.api_cost_usd)
        metrics.append(TaskMetric(
            case_code=case.code,
            title=case.title,
            qualified=bool(task.qualified),
            revision_count=task.revision_count or 0,
            facts_verdict=facts,
            execution_status=task.execution_status,
            empty_output=bool(task.empty_output),
            timed_out=bool(task.timed_out),
            duration_ms=task.duration_ms,
            api_cost_usd=cost,
            failure_reasons=reasons,
        ))
        views.append({
            "code": case.code,
            "title": case.title,
            "qualified": bool(task.qualified),
            "timed_out": bool(task.timed_out),
            "empty_output": bool(task.empty_output),
            "execution_status": task.execution_status,
            "error": task.error,
            "failure_reasons": reasons,
            "human_note": task.human_note,
        })
    return metrics, views


def refresh_run_artifacts(db, run: EvalRun) -> None:
    # The session uses autoflush=False. Persist this transaction's task edits
    # before querying its aggregate; callers hold the parent run lock.
    db.flush()
    metrics, views = task_metrics(db, run.id)
    run.summary = summarize(metrics)
    run.report_markdown = render_report(
        {
            "label": run.label,
            "provenance": run.provenance,
            "summary": run.summary,
        },
        views,
    )
