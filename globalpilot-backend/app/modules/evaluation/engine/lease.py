"""PostgreSQL lease primitives. Callers own the transaction and its commit.

All executor writes lock the global lease, then the run, then its tasks. The
lease row remains locked through the business write, so validation and fencing
cannot race with a takeover. External model calls never run under these locks.
"""
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.evaluation.models import EvalExecutorLease

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 10


@dataclass(frozen=True)
class LeaseToken:
    run_id: UUID
    owner: UUID
    generation: int


class LeaseLost(RuntimeError):
    pass


def lock_slot(db: Session) -> EvalExecutorLease:
    db.execute(
        insert(EvalExecutorLease)
        .values(slot="global", generation=0)
        .on_conflict_do_nothing(index_elements=[EvalExecutorLease.slot])
    )
    return db.scalars(
        select(EvalExecutorLease)
        .where(EvalExecutorLease.slot == "global")
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def lease_is_live(db: Session, lease: EvalExecutorLease) -> bool:
    # Read wall-clock time AFTER acquiring the lock, including after lock waits.
    now = db.scalar(select(func.clock_timestamp()))
    return lease.owner is not None and lease.lease_until is not None and lease.lease_until > now


def claim_locked_slot(db: Session, lease: EvalExecutorLease, run_id: UUID) -> LeaseToken:
    if lease.owner is not None:
        raise RuntimeError("The execution slot must be recovered before it is claimed")
    lease.owner = uuid4()
    lease.run_id = run_id
    lease.generation += 1
    lease.lease_until = db.scalar(select(func.clock_timestamp())) + timedelta(seconds=LEASE_SECONDS)
    db.flush()
    return LeaseToken(run_id=run_id, owner=lease.owner, generation=lease.generation)


def lock_owned_lease(db: Session, token: LeaseToken) -> EvalExecutorLease:
    lease = db.scalar(
        select(EvalExecutorLease)
        .where(EvalExecutorLease.slot == "global")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        lease is None
        or lease.owner != token.owner
        or lease.run_id != token.run_id
        or lease.generation != token.generation
        or not lease_is_live(db, lease)
    ):
        raise LeaseLost("评测执行租约已失效")
    return lease


def renew_lease(db: Session, token: LeaseToken) -> None:
    lease = lock_owned_lease(db, token)
    lease.lease_until = db.scalar(select(func.clock_timestamp())) + timedelta(seconds=LEASE_SECONDS)
    db.flush()


def clear_locked_slot(db: Session, lease: EvalExecutorLease) -> None:
    lease.owner = None
    lease.run_id = None
    lease.lease_until = None
    db.flush()
