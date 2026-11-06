from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.http_errors import AppHTTPException
from app.modules.auth.security import ensure_account_active
from app.modules.teams.models import Team, TeamMember, TeamMemberAuditLog
from app.modules.tickets.models import Ticket
from app.modules.users.models import User
from app.modules.teams.schemas import TeamMemberOption, TeamMembershipOption
from app.modules.teams.authorization import lock_team, lock_team_and_require_admin
from app.modules.brands.service import seed_team_brands

#初始化管理员
def initialize_team_admin(
    db: Session,
    *,
    admin_email: str,
    team_name: str | None = None,
    team_id: UUID | None = None,
) -> dict[str, str]:
    # 新建团队和初始化已有空团队，必须且只能选择一种。
    if (team_name is None) == (team_id is None):
        raise AppHTTPException(
            status_code=400,
            detail="团队名称和团队 ID 必须且只能提供一个",
            code="INVALID_INIT_ARGUMENTS",
        )

    if team_id is None:
        name = team_name.strip()
        if not 1 <= len(name) <= 100:
            raise AppHTTPException(
                status_code=400,
                detail="团队名称长度必须为 1 到 100",
                code="INVALID_TEAM_NAME",
            )
        team = Team(name=name)
        db.add(team)
        db.flush()
    else:
        # 同一团队的初始化串行执行，避免同时创建首个管理员。
        team = lock_team(db, team_id=team_id)

    # 两个分支都要创建管理员；这里只检查当前团队，不能混入其他团队。
    existing_member = (
        db.query(TeamMember.id)
        .filter(TeamMember.team_id == team.id)
        .first()
    )
    if existing_member is not None:
        raise AppHTTPException(
            status_code=409,
            detail="团队已有成员，不能重复初始化",
            code="TEAM_ALREADY_INITIALIZED",
        )

    email = admin_email.strip().lower()
    user = (
        db.query(User)
        .filter(User.email == email)
        .with_for_update()
        .one_or_none()
    )
    if user is None:
        raise AppHTTPException(
            status_code=404,
            detail="请先注册管理员账号",
            code="USER_NOT_FOUND",
        )
    ensure_account_active(user)

    member = TeamMember(
        team_id=team.id,
        user_id=user.id,
        role="admin",
        status="active",
    )
    db.add(member)
    db.flush()

    seed_team_brands(db, team_id=team.id)

    # 事务由调用方提交；UUID 转字符串后才能直接用于 JSON 输出。
    return {
        "team_id": str(team.id),
        "team_name": team.name,
        "admin_user_id": str(user.id),
        "role": member.role,
    }


def list_my_teams(db: Session, *, user_id: UUID) -> list[TeamMembershipOption]:
    rows = (
        db.query(
            Team.id.label("team_id"),
            Team.name.label("team_name"),
            TeamMember.user_id,
            TeamMember.role,
            TeamMember.status,
        )
        .select_from(TeamMember)
        .join(Team, Team.id == TeamMember.team_id)
        .join(User, User.id == TeamMember.user_id)
        .filter(
            TeamMember.user_id == user_id,
            TeamMember.status == "active",
            TeamMember.role.in_(("admin", "employee")),
            User.is_active.is_(True),
        )
        .order_by(Team.create_at.asc(), Team.id.asc())
        .all()
    )
    return [
        TeamMembershipOption(
            team_id=row.team_id,
            team_name=row.team_name,
            user_id=row.user_id,
            role=row.role,
            status=row.status,
        )
        for row in rows
    ]


# 查看团队内可指派成员。
def list_active_team_member(
    db: Session,
    *,
    team_id: UUID,
) -> list[TeamMemberOption]:
    rows = (
        db.query(
            User.id.label("user_id"),
            User.email,
            User.nickname,
            TeamMember.role,
        )
        .join(TeamMember, TeamMember.user_id == User.id)
        .filter(
            TeamMember.team_id == team_id,
            TeamMember.status == "active",
            TeamMember.role.in_(("admin", "employee")),
            User.is_active.is_(True),
        )
        .order_by(TeamMember.join_at.asc(), TeamMember.id.asc())
        .all()
    )
    return [
        TeamMemberOption(
            user_id=row.user_id,
            email=row.email,
            nickname=row.nickname,
            role=row.role,
        )
        for row in rows
    ]

def _lock_active_team_member(
    db: Session,
    *,
    team_id: UUID,
    target_user_id: UUID,
) -> tuple[User, TeamMember]:
    # Restrict the target to this team before locking its User row.
    user = (
        db.query(User)
        .join(TeamMember, TeamMember.user_id == User.id)
        .filter(
            User.id == target_user_id,
            TeamMember.team_id == team_id,
            TeamMember.status == "active",
        )
        .populate_existing()
        .with_for_update(of=User)
        .one_or_none()
    )
    if user is None:
        raise AppHTTPException(
            status_code=404,
            detail="该团队没有这个有效成员",
            code="TEAM_MEMBER_NOT_FOUND",
        )
    member = (
        db.query(TeamMember)
        .filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == target_user_id,
            TeamMember.status == "active",
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if member is None:
        raise AppHTTPException(
            status_code=404,
            detail="该团队没有这个有效成员",
            code="TEAM_MEMBER_NOT_FOUND",
        )
    return user, member


def _ensure_another_active_admin(
    db: Session,
    *,
    team_id: UUID,
    target_user: User,
    target_member: TeamMember,
) -> None:
    if target_member.role != "admin" or not target_user.is_active:
        return
    another = (
        db.query(TeamMember.id)
        .join(User, User.id == TeamMember.user_id)
        .filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id != target_user.id,
            TeamMember.role == "admin",
            TeamMember.status == "active",
            User.is_active.is_(True),
        )
        .first()
    )
    if another is None:
        raise AppHTTPException(
            status_code=409,
            detail="团队必须至少保留一名有效管理员",
            code="LAST_ADMIN_REQUIRED",
        )


# 管理员添加已注册员工；调用方负责提交成员及审计记录。
def add_team_member(
    db: Session,
    *,
    team_id: UUID,
    email: str,
    actor_user_id: UUID,
) -> TeamMemberOption:
    lock_team_and_require_admin(db, team_id=team_id, actor_user_id=actor_user_id)
    user = (
        db.query(User)
        .filter(User.email == email.strip().lower())
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )

    if user is None:
        raise AppHTTPException(
            status_code=404,
            detail="用户还没注册",
            code="USER_NOT_FOUND",
        )

    ensure_account_active(user)

    existing = (
        db.query(TeamMember.id)
        .filter(
            TeamMember.team_id == team_id,
            TeamMember.user_id == user.id,
        )
        .first()
    )

    if existing is not None:
        raise AppHTTPException(
            status_code=409,
            detail="该团队已有该成员，请勿重复添加",
            code="TEAM_MEMBER_EXISTS",
        )

    member = TeamMember(
        team_id=team_id,
        user_id=user.id,
        role="employee",
        status="active",
    )

    log = TeamMemberAuditLog(
        team_id=team_id,
        actor_user_id=actor_user_id,
        target_user_id=user.id,
        action="member_added",
        after_role=member.role,
        after_status=member.status,
    )

    db.add_all([member, log])
    db.flush()

    return TeamMemberOption(
        user_id=user.id,
        email=user.email,
        nickname=user.nickname,
        role=member.role,
    )


def update_team_member_role(
    db: Session,
    *,
    team_id: UUID,
    target_user_id: UUID,
    role: Literal["admin", "employee"],
    actor_user_id: UUID,
) -> TeamMemberOption:
    lock_team_and_require_admin(db, team_id=team_id, actor_user_id=actor_user_id)
    user, member = _lock_active_team_member(
        db, team_id=team_id, target_user_id=target_user_id
    )
    ensure_account_active(user)
    if member.role == role:
        return TeamMemberOption(
            user_id=user.id, email=user.email, nickname=user.nickname, role=member.role
        )
    if member.role == "admin":
        _ensure_another_active_admin(
            db, team_id=team_id, target_user=user, target_member=member
        )

    before_role = member.role
    member.role = role
    db.add(TeamMemberAuditLog(
        team_id=team_id,
        actor_user_id=actor_user_id,
        target_user_id=user.id,
        action="member_role_changed",
        before_role=before_role,
        after_role=member.role,
        before_status=member.status,
        after_status=member.status,
    ))
    db.flush()
    return TeamMemberOption(
        user_id=user.id, email=user.email, nickname=user.nickname, role=member.role
    )


def remove_team_member(
    db: Session,
    *,
    team_id: UUID,
    target_user_id: UUID,
    actor_user_id: UUID,
) -> None:
    lock_team_and_require_admin(db, team_id=team_id, actor_user_id=actor_user_id)
    user, member = _lock_active_team_member(
        db, team_id=team_id, target_user_id=target_user_id
    )
    _ensure_another_active_admin(
        db, team_id=team_id, target_user=user, target_member=member
    )

    # 创建工单与移除成员共用团队锁，避免退出与新指派同时发生。
    unfinished_ticket = (
        db.query(Ticket.id)
        .filter(
            Ticket.team_id == team_id,
            Ticket.status != "completed",
            or_(
                Ticket.assignee_id == target_user_id,
                Ticket.reviewer_id == target_user_id,
            ),
        )
        .first()
    )
    if unfinished_ticket is not None:
        raise AppHTTPException(
            409,
            "该成员仍有未完成的处理或审核任务，请先交接",
            code="MEMBER_HAS_OPEN_TICKETS",
        )

    before_status = member.status
    member.status = "removed"
    member.remove_at = datetime.now(timezone.utc)
    db.add(TeamMemberAuditLog(
        team_id=team_id,
        actor_user_id=actor_user_id,
        target_user_id=user.id,
        action="member_removed",
        before_role=member.role,
        after_role=member.role,
        before_status=before_status,
        after_status=member.status,
    ))
    db.flush()
