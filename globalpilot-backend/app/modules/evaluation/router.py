from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.modules.auth.security import get_current_user
from app.modules.teams.dependencies import get_current_member_from_query
from app.db.database import get_db
from app.modules.users.models import User
from app.modules.teams.models import TeamMember
from app.modules.evaluation.schemas import (
    EvalCompareOut,
    EvalReviewInput,
    EvalRunCreate,
    EvalRunOut,
    EvalTaskOut,
    EvalTestSetOut,
)
from app.modules.evaluation.service import (
    compare_runs,
    get_run,
    get_snapshot,
    get_test_set,
    list_runs,
    list_tasks,
    rebuild_report,
    review_task,
    set_baseline,
    start_run,
)

router = APIRouter(prefix="/evals")


def _write_user_id(db: Session, user: User) -> UUID:
    user_id = user.id
    # Authentication only read this request Session. Release its connection
    # before the write service acquires a dedicated transaction, avoiding pool
    # starvation when many authenticated requests arrive together.
    db.rollback()
    return user_id


@router.get("/snapshot")
def read_snapshot(
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return get_snapshot(db, team_id=member.team_id, actor_user_id=member.user_id)


@router.get("/testset", response_model=EvalTestSetOut)
def read_test_set(
    db: Session = Depends(get_db),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return get_test_set(db, team_id=member.team_id, actor_user_id=member.user_id)


@router.get("/runs", response_model=list[EvalRunOut])
def read_runs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return list_runs(db, user, team_id=member.team_id)


@router.post("/runs", response_model=EvalRunOut, status_code=202)
def create_run(
    body: EvalRunCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    run = start_run(_write_user_id(db, user), body, team_id=team_id)
    response.headers["Location"] = str(
        request.url_for("read_run", run_id=str(run.id)).include_query_params(team_id=str(team_id))
    )
    return run


@router.get("/runs/{run_id}", response_model=EvalRunOut)
def read_run(
    run_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return get_run(db, user, run_id, team_id=member.team_id)


@router.get("/runs/{run_id}/tasks", response_model=list[EvalTaskOut])
def read_tasks(
    run_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return list_tasks(db, user, run_id, team_id=member.team_id)


@router.post("/runs/{run_id}/baseline", response_model=EvalRunOut)
def mark_baseline(
    run_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    return set_baseline(_write_user_id(db, user), run_id, team_id=team_id)


@router.post("/runs/{run_id}/report", response_model=EvalRunOut)
def refresh_report(
    run_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    return rebuild_report(_write_user_id(db, user), run_id, team_id=team_id)


@router.get("/compare", response_model=EvalCompareOut)
def read_compare(
    left: UUID,
    right: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return compare_runs(db, user, left, right, team_id=member.team_id)


@router.patch("/tasks/{task_id}", response_model=EvalTaskOut)
def patch_task(
    task_id: UUID,
    body: EvalReviewInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    return review_task(_write_user_id(db, user), task_id, body, team_id=team_id)
