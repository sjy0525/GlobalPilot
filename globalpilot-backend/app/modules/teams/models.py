from uuid import uuid4,UUID
from datetime import datetime

from app.db.base import Base
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,DateTime,func,ForeignKey,UniqueConstraint,CheckConstraint

class Team(Base):
    __tablename__ = "teams"

    id:Mapped[UUID]=mapped_column(
        primary_key=True,
        nullable=False,
        default=uuid4
    )
    name:Mapped[str]=mapped_column(
        String(100),
        nullable=False
    )

    create_at:Mapped[datetime]=mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )


class TeamMember(Base):
    __tablename__ = "team_members"

    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "user_id",
            name="uq_team_members_team_user",
        ),
        CheckConstraint(
            "role IN ('admin', 'employee')",
            name="ck_team_members_role",
        ),
        CheckConstraint(
            "status IN ('active', 'removed')",
            name="ck_team_members_status",
        ),
    )

    id:Mapped[UUID]=mapped_column(
        primary_key=True,
        nullable=False,
        default=uuid4
    )

    team_id:Mapped[UUID]=mapped_column(
        ForeignKey('teams.id',ondelete="RESTRICT"),
        nullable=False,
    )

    user_id:Mapped[UUID]=mapped_column(
        ForeignKey('users.id',ondelete="RESTRICT"),
        nullable=False,
        index=True
    )

    role:Mapped[str]=mapped_column(
        String(50),
        nullable=False,
        default="employee",
    )
    status:Mapped[str]=mapped_column(
        String(50),
        nullable=False,
        default="active"
    )

    join_at:Mapped[datetime]=mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    remove_at:Mapped[datetime|None]=mapped_column(
        DateTime(timezone=True),
        nullable=True
    )


#增加成员操作日志模型
class TeamMemberAuditLog(Base):
    __tablename__="team_member_audit_logs"

    id:Mapped[UUID]=mapped_column(
        primary_key=True,
        default=uuid4
    )

    team_id:Mapped[UUID]=mapped_column(
        ForeignKey('teams.id',ondelete='RESTRICT'),
        nullable=False
    )

    actor_user_id:Mapped[UUID]=mapped_column(
        ForeignKey('users.id',ondelete='RESTRICT'),
        nullable=False
    )

    target_user_id:Mapped[UUID]=mapped_column(
        ForeignKey('users.id',ondelete='RESTRICT'),
        nullable=False
    )

    action:Mapped[str]=mapped_column(
        String(100),
        nullable=False
    )

    before_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    after_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    before_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    after_status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    create_at:Mapped[datetime]=mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

