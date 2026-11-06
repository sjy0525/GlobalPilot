"""Durable targeted-revision records; source snapshots are never chat state."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, JSON,
    String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


DOCUMENT_JSON = JSONB().with_variant(JSON(), "sqlite")


class RevisionDraft(Base):
    __tablename__ = "marketing_revision_drafts"
    __table_args__ = (
        UniqueConstraint("brief_id", "platform", name="uq_revision_draft_brief_platform"),
        CheckConstraint("version >= 1", name="ck_revision_draft_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    brief_id: Mapped[UUID] = mapped_column(ForeignKey("briefs.id", ondelete="RESTRICT"), index=True)
    platform: Mapped[str] = mapped_column(String(50))
    version: Mapped[int] = mapped_column(Integer, default=1)
    body_hash: Mapped[str] = mapped_column(String(64))
    blocks: Mapped[list] = mapped_column(DOCUMENT_JSON)
    layout: Mapped[list] = mapped_column(DOCUMENT_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RevisionTask(Base):
    __tablename__ = "marketing_revision_tasks"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", "idempotency_key", name="uq_revision_task_request"),
        CheckConstraint("version >= 1", name="ck_revision_task_version"),
        CheckConstraint("max_revisions BETWEEN 0 AND 3", name="ck_revision_task_budget"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    brief_id: Mapped[UUID] = mapped_column(ForeignKey("briefs.id", ondelete="RESTRICT"), index=True)
    draft_id: Mapped[UUID] = mapped_column(ForeignKey("marketing_revision_drafts.id", ondelete="RESTRICT"))
    platform: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_revisions: Mapped[int] = mapped_column(Integer, default=3)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    original_draft: Mapped[dict] = mapped_column(DOCUMENT_JSON)
    original_requirements: Mapped[dict] = mapped_column(DOCUMENT_JSON)
    requirements: Mapped[dict] = mapped_column(DOCUMENT_JSON)
    review_context: Mapped[dict] = mapped_column(DOCUMENT_JSON)
    selected_document_ids: Mapped[list] = mapped_column(DOCUMENT_JSON, default=list)
    source_content: Mapped[dict] = mapped_column(DOCUMENT_JSON)
    source_content_hash: Mapped[str] = mapped_column(String(64))
    source_workflow_version: Mapped[int] = mapped_column(BigInteger)
    source_layout: Mapped[list] = mapped_column(DOCUMENT_JSON)
    state: Mapped[dict] = mapped_column(DOCUMENT_JSON, default=dict)
    history: Mapped[list] = mapped_column(DOCUMENT_JSON, default=list)
    adopted_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RevisionOperation(Base):
    __tablename__ = "marketing_revision_operations"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_revision_operation_request"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("marketing_revision_tasks.id", ondelete="RESTRICT"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(DOCUMENT_JSON)
    status: Mapped[str] = mapped_column(String(20), default="running")
    response: Mapped[dict | None] = mapped_column(DOCUMENT_JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RevisionSample(Base):
    __tablename__ = "marketing_revision_samples"
    __table_args__ = (UniqueConstraint("task_id", name="uq_revision_sample_task"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("marketing_revision_tasks.id", ondelete="RESTRICT"), index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    brief_id: Mapped[UUID] = mapped_column(ForeignKey("briefs.id", ondelete="RESTRICT"), index=True)
    data: Mapped[dict] = mapped_column(DOCUMENT_JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
