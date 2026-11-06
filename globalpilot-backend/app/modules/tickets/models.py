from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

class Ticket(Base):
    __tablename__='tickets'

    __table_args__=(
        CheckConstraint(
            "status IN"
            "('processing','awaiting_review','needs_info','completed')",
            name='ck_tickets_status'
        ),
    )

    id:Mapped[UUID]=mapped_column(
        primary_key=True,
        default=uuid4
    )

    team_id:Mapped[UUID]=mapped_column(
        ForeignKey('teams.id',ondelete='RESTRICT'),
        nullable=False,
        index=True
    )

    title:Mapped[str]=mapped_column(
        Text,
        nullable=False
    )

    description:Mapped[str]=mapped_column(Text,nullable=False)

    processing_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[UUID] = mapped_column(
        ForeignKey('users.id', ondelete='RESTRICT'),
        nullable=False,
    )

    assignee_id:Mapped[UUID]=mapped_column(
        ForeignKey('users.id',ondelete='RESTRICT'),
        nullable=False
    )

    reviewer_id:Mapped[UUID]=mapped_column(
        ForeignKey('users.id',ondelete='RESTRICT'),
        nullable=False
    )

    status:Mapped[str]=mapped_column(
        String(32),
        nullable=False,
        default='processing',
        server_default='processing',
    )

    created_at:Mapped[datetime]=mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at:Mapped[datetime]=mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        # Review/reassignment requests use this value as a snapshot token. It
        # must change even for multiple updates within one DB transaction.
        onupdate=text(
            "GREATEST(clock_timestamp(), updated_at + INTERVAL '1 microsecond')"
        ),
    )


class TicketAssignmentAuditLog(Base):
    __tablename__ = "ticket_assignment_audit_logs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    ticket_id: Mapped[UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    before_assignee_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    after_assignee_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    before_reviewer_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    after_reviewer_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
