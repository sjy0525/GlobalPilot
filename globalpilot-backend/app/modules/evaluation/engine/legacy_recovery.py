"""Explicit offline recovery for pre-lease evaluation runs.

Old executors do not participate in the lease protocol. The caller must stop
every old worker first; neither a missing lease nor this code can prove that.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.evaluation.engine.lease import lease_is_live, lock_slot
from app.modules.evaluation.engine.persistence import _mark_run_failed, recover_expired_run
from app.modules.evaluation.models import EvalRun


class WorkersNotStopped(ValueError):
    pass


class ActiveEvaluationLease(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyRecoveryResult:
    legacy_runs_recovered: int
    expired_lease_recovered: bool


def recover_legacy_evaluations(
    db: Session, *, workers_stopped: bool = False,
) -> LegacyRecoveryResult:
    """Recover under the caller's transaction; keep all runs and task history."""
    if workers_stopped is not True:
        raise WorkersNotStopped("必须先停止所有旧版评测 worker，并明确确认")

    lease = lock_slot(db)
    if lease_is_live(db, lease):
        raise ActiveEvaluationLease("存在未过期的评测租约，拒绝离线恢复")
    expired_recovered = recover_expired_run(db, lease)

    # The expired leased run has already been flushed as failed. Remaining
    # queued/running rows are unowned legacy work. Holding the global row also
    # prevents a new executor from claiming a run during this maintenance.
    runs = db.scalars(
        select(EvalRun)
        .where(EvalRun.status.in_(("queued", "running")))
        .order_by(EvalRun.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for run in runs:
        _mark_run_failed(db, run, "旧版执行器已停止，离线回收未完成的评测")
    db.flush()
    return LegacyRecoveryResult(len(runs), expired_recovered)
