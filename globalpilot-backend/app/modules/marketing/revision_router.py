"""Authenticated backend entry points for targeted paragraph revisions."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.modules.auth.security import get_current_user
from app.modules.marketing import revision_service
from app.modules.marketing.schemas.revision import (
    DraftSnapshot,
    RevisionClarificationInput,
    RevisionCommandInput,
    RevisionCreateInput,
    RevisionCreateRequest,
    RevisionReviewInput,
    RevisionTaskOutput,
)
from app.modules.teams.dependencies import get_current_member_from_query
from app.modules.teams.models import TeamMember
from app.modules.users.models import User

router = APIRouter(tags=["targeted-revisions"])


@router.get("/briefs/{brief_id}/revisions/document", response_model=DraftSnapshot)
def get_revision_document(
    brief_id: UUID,
    platform: str = Query(min_length=1, max_length=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return revision_service.get_revision_document(
        db, user, brief_id, platform, team_id=member.team_id,
    )


@router.post("/briefs/{brief_id}/revisions", response_model=RevisionTaskOutput)
def create_revision_task(
    brief_id: UUID,
    body: RevisionCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    payload = RevisionCreateInput(
        brief_id=brief_id, **body.model_dump(),
    )
    return revision_service.create_revision_task(
        db, user, payload, team_id=member.team_id,
    )


@router.get("/briefs/{brief_id}/revisions", response_model=list[RevisionTaskOutput])
def list_revision_tasks(
    brief_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return revision_service.list_revision_tasks(
        db, user, brief_id, team_id=member.team_id,
    )


@router.get("/revision-tasks/{task_id}", response_model=RevisionTaskOutput)
def get_revision_task(
    task_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return revision_service.get_revision_task(
        db, user, task_id, team_id=member.team_id,
    )


def _preflight(db: Session, user: User, task_id: UUID, *, team_id: UUID) -> UUID:
    actor_id = user.id
    revision_service.get_revision_task(db, user, task_id, team_id=team_id)
    # The execution service rechecks permissions on its fenced connection.
    # Release the request transaction before any model IO begins.
    db.rollback()
    return actor_id


@router.post("/revision-tasks/{task_id}/run", response_model=RevisionTaskOutput)
def run_revision_task(
    task_id: UUID,
    body: RevisionCommandInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    actor_id = _preflight(db, user, task_id, team_id=team_id)
    return revision_service.run_revision_task(
        actor_id, task_id, body, team_id=team_id,
    )


@router.post("/revision-tasks/{task_id}/clarification", response_model=RevisionTaskOutput)
def clarify_revision_task(
    task_id: UUID,
    body: RevisionClarificationInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    actor_id = _preflight(db, user, task_id, team_id=team_id)
    return revision_service.clarify_revision_task(
        actor_id, task_id, body, team_id=team_id,
    )


@router.post("/revision-tasks/{task_id}/review", response_model=RevisionTaskOutput)
def review_revision_task(
    task_id: UUID,
    body: RevisionReviewInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    actor_id = _preflight(db, user, task_id, team_id=team_id)
    return revision_service.review_revision_task(
        actor_id, task_id, body, team_id=team_id,
    )


@router.get("/revision-tasks/{task_id}/sample")
def export_revision_sample(
    task_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return revision_service.export_revision_sample(
        db, user, task_id, team_id=member.team_id,
    )
