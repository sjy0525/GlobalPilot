from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DEFAULT_BRAND_NAMES = ("Wearables", "Audio", "Smart home")


class Brand(Base):
    __tablename__ = "brands"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_brands_team_name"),
        UniqueConstraint("team_id", "id", name="uq_brands_team_id_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # NULL preserves legacy rows until their actual team has been established.
    team_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
