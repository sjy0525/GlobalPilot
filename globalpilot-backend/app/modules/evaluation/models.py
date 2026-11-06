from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EvalTestSet(Base):
    __tablename__ = "eval_test_sets"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    version: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class EvalCase(Base):
    __tablename__ = "eval_cases"
    __table_args__ = (UniqueConstraint("test_set_id", "code", name="uq_eval_case_code"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    test_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("eval_test_sets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    user_request: Mapped[str] = mapped_column(Text, nullable=False)
    brand_profile: Mapped[str] = mapped_column(Text, nullable=False)
    brand_name: Mapped[str] = mapped_column(String(100), nullable=False)
    platforms: Mapped[list] = mapped_column(JSONB, nullable=False)
    must_include: Mapped[list] = mapped_column(JSONB, nullable=False)
    forbidden_claims: Mapped[list] = mapped_column(JSONB, nullable=False)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    persona: Mapped[str] = mapped_column(String(255), nullable=False)
    purchase_channels: Mapped[str | None] = mapped_column(String(255), nullable=True)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        Index(
            "uq_eval_baseline", "team_id", "user_id", "test_set_id", unique=True,
            postgresql_where=text("is_baseline AND team_id IS NOT NULL"),
        ),
        Index(
            "uq_eval_baseline_legacy", "user_id", "test_set_id", unique=True,
            postgresql_where=text("is_baseline AND team_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Historical runs remain unassigned until their ownership is confirmed.
    team_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    test_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("eval_test_sets.id"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    is_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class EvalTask(Base):
    __tablename__ = "eval_tasks"
    __table_args__ = (UniqueConstraint("run_id", "case_id", name="uq_eval_task_case"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[UUID] = mapped_column(
        ForeignKey("eval_cases.id"),
        nullable=False,
        index=True,
    )
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    empty_output: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qualified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    generated_content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    strategy: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    judge_cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 8), nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    auto_scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    human_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)


class EvalExecutorLease(Base):
    """One database-wide execution slot; generation fences expired workers."""

    __tablename__ = "eval_executor_leases"
    __table_args__ = (
        CheckConstraint("slot = 'global'", name="ck_eval_lease_global"),
        CheckConstraint(
            "(owner IS NULL AND run_id IS NULL AND lease_until IS NULL) OR "
            "(owner IS NOT NULL AND run_id IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_eval_lease_state",
        ),
    )

    slot: Mapped[str] = mapped_column(String(16), primary_key=True)
    owner: Mapped[UUID | None] = mapped_column(nullable=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("eval_runs.id"), nullable=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
