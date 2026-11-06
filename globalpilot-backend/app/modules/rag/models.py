from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "brand_id"], ["brands.team_id", "brands.id"],
            name="fk_knowledge_documents_team_brand", ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    team_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    brand_id: Mapped[UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(127), nullable=True)
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    structured: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
    )


class KnowledgeAsset(Base):
    """Original parser image assets, replaced atomically with their document."""

    __tablename__ = "knowledge_assets"

    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), primary_key=True,
    )
    asset_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(127), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
