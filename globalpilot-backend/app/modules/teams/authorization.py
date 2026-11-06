"""Team permission checks, independent of FastAPI dependency injection.

These helpers never commit or roll back. Writers must acquire the team lock
before checking permissions, and retain it until their transaction finishes.
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.http_errors import AppHTTPException
from app.modules.auth.security import ensure_account_active
from app.modules.teams.models import Team, TeamMember
from app.modules.users.models import User


def lock_team(db: Session, *, team_id: UUID) -> Team:
    team = (
        db.query(Team)
        .filter(Team.id == team_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if team is None:
        raise AppHTTPException(404, "团队不存在", code="TEAM_NOT_FOUND")
    return team


def require_active_team_member(
    db: Session, *, team_id: UUID, user_id: UUID
) -> TeamMember:
    # Refresh both entities: this Session may have cached them before a lock wait.
    row = (
        db.query(TeamMember, User)
        .join(User, User.id == TeamMember.user_id)
        .filter(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        .populate_existing()
        .one_or_none()
    )
    if row is None:
        raise AppHTTPException(
            403, "不属于该团队的有效成员", code="TEAM_ACCESS_DENIED"
        )
    member, user = row
    if member.status != "active" or member.role not in ("admin", "employee"):
        raise AppHTTPException(
            403, "不属于该团队的有效成员", code="TEAM_ACCESS_DENIED"
        )
    ensure_account_active(user)
    return member


def require_team_admin(member: TeamMember) -> TeamMember:
    """Check the role of a member returned by require_active_team_member."""
    if member.role != "admin":
        raise AppHTTPException(
            403, "该操作必须由管理员执行", code="ADMIN_REQUIRED"
        )
    return member


def lock_team_and_require_member(
    db: Session, *, team_id: UUID, actor_user_id: UUID
) -> TeamMember:
    lock_team(db, team_id=team_id)
    return require_active_team_member(db, team_id=team_id, user_id=actor_user_id)


def lock_team_and_require_admin(
    db: Session, *, team_id: UUID, actor_user_id: UUID
) -> TeamMember:
    member = lock_team_and_require_member(
        db, team_id=team_id, actor_user_id=actor_user_id
    )
    return require_team_admin(member)


async def require_active_team_member_async(
    db: AsyncSession, *, team_id: UUID, user_id: UUID
) -> TeamMember:
    """The asynchronous equivalent of the same database-backed membership rule."""
    row = (await db.execute(
        select(TeamMember, User)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        .execution_options(populate_existing=True)
    )).one_or_none()
    if row is None:
        raise AppHTTPException(403, "不属于该团队的有效成员", code="TEAM_ACCESS_DENIED")
    member, user = row
    if member.status != "active" or member.role not in ("admin", "employee"):
        raise AppHTTPException(403, "不属于该团队的有效成员", code="TEAM_ACCESS_DENIED")
    ensure_account_active(user)
    return member


async def lock_team_and_require_member_async(
    db: AsyncSession, *, team_id: UUID, actor_user_id: UUID
) -> TeamMember:
    team = (await db.execute(
        select(Team).where(Team.id == team_id)
        .execution_options(populate_existing=True).with_for_update()
    )).scalar_one_or_none()
    if team is None:
        raise AppHTTPException(404, "团队不存在", code="TEAM_NOT_FOUND")
    return await require_active_team_member_async(
        db, team_id=team_id, user_id=actor_user_id
    )


def get_active_team_member_ids(
    db: Session, *, team_id: UUID, user_ids: Iterable[UUID]
) -> set[UUID]:
    """Return valid assignment candidates in this team, allowing repeated IDs."""
    candidates = set(user_ids)
    if not candidates:
        return set()
    rows = (
        db.query(TeamMember.user_id)
        .join(User, User.id == TeamMember.user_id)
        .filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id.in_(candidates),
            TeamMember.status == "active",
            TeamMember.role.in_(("admin", "employee")),
            User.is_active.is_(True),
        )
        .all()
    )
    return {row.user_id for row in rows}
