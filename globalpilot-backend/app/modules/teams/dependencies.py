from uuid import UUID

from fastapi import Depends, Path, Query
from sqlalchemy.orm import Session

from app.modules.auth.security import get_current_user
from app.db.database import get_db
from app.modules.teams.models import TeamMember
from app.modules.users.models import User
from app.modules.teams.authorization import require_active_team_member, require_team_admin


def get_current_member(
    team_id: UUID = Path(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TeamMember:
    return require_active_team_member(db, team_id=team_id, user_id=user.id)


def get_current_member_from_query(
    team_id: UUID = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TeamMember:
    """Existing business URLs require an explicit team; never guess membership."""
    member = require_active_team_member(db, team_id=team_id, user_id=user.id)
    # Provider calls may take seconds. Keep a detached identity snapshot, not an
    # authentication transaction occupying a connection throughout model IO.
    db.expunge(member)
    db.rollback()
    return member


def require_admin(
    team_member: TeamMember = Depends(get_current_member),
) -> TeamMember:
    return require_team_admin(team_member)
