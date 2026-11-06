from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.modules.teams.dependencies import get_current_member_from_query
from app.modules.auth.security import get_current_user
from app.db.database import get_db
from app.modules.teams.models import TeamMember
from app.modules.users.models import User
from app.modules.memory.schemas.memory_job import BriefMemoriesOutput
from app.modules.memory.services.memory_job import get_brief_memories

router = APIRouter()


@router.get("/briefs/{brief_id}/memories", response_model=BriefMemoriesOutput)
def read_brief_memories(
    brief_id: UUID,
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_current_member_from_query),
    user: User = Depends(get_current_user),
):
    return get_brief_memories(
        db, user, brief_id, team_id=member.team_id,
    )
