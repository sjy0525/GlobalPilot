from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Brief(Base):
    __tablename__ = "briefs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "brand_id"], ["brands.team_id", "brands.id"],
            name="fk_briefs_team_brand", ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # NULL is reserved for legacy rows whose team has not been confirmed.
    team_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    brand_id: Mapped[UUID] = mapped_column(
        ForeignKey("brands.id"),
        nullable=False,
        index=True,
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    selling_points: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(String(50), nullable=False)
    user_persona: Mapped[str] = mapped_column(String(255), nullable=False)
    social_media: Mapped[list[str]] = mapped_column(ARRAY(String(50)), nullable=False)
    purchase_channels: Mapped[str | None] = mapped_column(String(255), nullable=True)
    competitor_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workflow_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    workflow_checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    final_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
    )
