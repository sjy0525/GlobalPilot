from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.modules.teams.dependencies import get_current_member_from_query
from app.modules.auth.security import get_current_user
from app.db.database import get_db
from app.modules.teams.models import TeamMember
from app.modules.users.models import User
from app.modules.briefs.schemas import BriefListItem, BriefRequest, BriefResponse
from app.modules.briefs.service import (
    create_brief as create_brief_service,
    list_briefs,
)

router = APIRouter()


@router.post("/briefs", response_model=BriefResponse, status_code=201)
def create_brief(
    body: BriefRequest,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_current_member_from_query),
    user: User = Depends(get_current_user),
):
    try:
        result = create_brief_service(
            db, user, body, team_id=member.team_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


@router.get("/briefs", response_model=list[BriefListItem])
def get_briefs(
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_current_member_from_query),
    user: User = Depends(get_current_user),
):
    return list_briefs(db, user, team_id=member.team_id)
