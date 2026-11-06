from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.modules.teams.dependencies import get_current_member, require_admin
from app.modules.auth.security import get_current_user
from app.db.database import get_db
from app.modules.teams.models import TeamMember
from app.modules.users.models import User
from app.modules.teams.schemas import (
    TeamMemberOption,
    TeamMemberCreate,
    TeamMemberRoleUpdate,
    TeamMembershipOption,
)

from app.modules.teams.service import (
    add_team_member,
    list_active_team_member,
    list_my_teams,
    remove_team_member,
    update_team_member_role,
)

router = APIRouter(prefix="/team", tags=["team"])


@router.get("", response_model=list[TeamMembershipOption])
def get_my_teams(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_my_teams(db, user_id=user.id)


@router.get("/{team_id}/me")
def get_membership(member: TeamMember = Depends(get_current_member)):
    return {
        "user_id": member.user_id,
        "team_id": member.team_id,
        "role": member.role,
        "status": member.status,
    }

# 查询团队内所有可指派的有效成员。
@router.get("/{team_id}/members/available", response_model=list[TeamMemberOption])
def get_available_members(
    member: TeamMember = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    return list_active_team_member(
        db,
        team_id=member.team_id,
    )

# 管理员添加已经注册的员工。
@router.post("/{team_id}/members", response_model=TeamMemberOption, status_code=201)
def create_team_member(
    body: TeamMemberCreate,
    admin: TeamMember = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        result = add_team_member(
            db,
            team_id=admin.team_id,
            actor_user_id=admin.user_id,
            email=body.email,
        )
        db.commit()

    except Exception:
        db.rollback()
        raise

    return result


@router.patch("/{team_id}/members/{user_id}/role", response_model=TeamMemberOption)
def change_team_member_role(
    user_id: UUID,
    body: TeamMemberRoleUpdate,
    admin: TeamMember = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        result = update_team_member_role(
            db,
            team_id=admin.team_id,
            target_user_id=user_id,
            role=body.role,
            actor_user_id=admin.user_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


@router.delete("/{team_id}/members/{user_id}", status_code=204, response_class=Response)
def delete_team_member(
    user_id: UUID,
    admin: TeamMember = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        remove_team_member(
            db,
            team_id=admin.team_id,
            target_user_id=user_id,
            actor_user_id=admin.user_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(status_code=204)
