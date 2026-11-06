import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from anyio import CancelScope
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.modules.auth.security import get_current_user
from app.modules.teams.dependencies import get_current_member_from_query
from app.modules.teams.models import TeamMember
from app.db.database import get_db
from app.modules.users.models import User
from app.modules.marketing.schemas.marketing_workflow import (
    MarketingWorkflowInput,
    MarketingWorkflowMiddleOutput,
    MarketingWorkflowReviewInput,
)
from app.modules.marketing.service import (
    get_workflow,
    iter_review_stream,
    iter_start_stream,
    load_workflow,
    review,
    validate_review,
    start,
)

router = APIRouter()
logger = logging.getLogger(__name__)

STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def _preflight(db, user, brief_id, payload=None, *, team_id):
    user_id = user.id
    _, _, snapshot = load_workflow(db, user, brief_id, team_id=team_id)
    if payload is not None:
        validate_review(snapshot, payload)
    # Authentication and ownership only read this request Session. Release it
    # before a long graph invocation or streaming response begins.
    db.rollback()
    return user_id


async def _drain_stream_task(task):
    """Finish a worker without forwarding repeated consumer cancellation to it."""
    cancelled = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    try:
        return task.result(), None, cancelled
    except BaseException as exc:
        return None, exc, cancelled


class WorkflowStreamingResponse(StreamingResponse):
    async def stream_response(self, send):
        failure = None
        try:
            await super().stream_response(send)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            # Starlette does not close an async body iterator when transport
            # send() fails while the iterator is suspended at a yielded frame.
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with CancelScope(shield=True):
                    _, error, cancelled = await _drain_stream_task(asyncio.create_task(close()))
                if error is not None:
                    if failure is None and cancelled is None:
                        raise error
                    logger.error(
                        "Workflow body cleanup failed after transport stopped",
                        exc_info=(type(error), error, error.__traceback__),
                    )
                if failure is None and cancelled is not None:
                    raise cancelled


async def _stream_body(iterator):
    pending = None
    failure = None
    cancelled = None

    def advance():
        # StopIteration cannot cross the asyncio Future boundary.
        try:
            return True, next(iterator)
        except StopIteration:
            return False, None

    try:
        while True:
            pending = asyncio.create_task(run_in_threadpool(advance))
            await asyncio.wait({pending})
            has_chunk, chunk = pending.result()
            pending = None
            if not has_chunk:
                break
            yield chunk
    except BaseException as exc:
        failure = exc
        raise
    finally:
        errors = []
        # Task.cancel() bypasses AnyIO's shield. Drain the active next() even
        # after repeated native cancellation before closing the same generator.
        with CancelScope(shield=True):
            if pending is not None:
                _, error, interrupted = await _drain_stream_task(pending)
                cancelled = cancelled or interrupted
                if error is not None and error is not failure:
                    errors.append(error)
            _, error, interrupted = await _drain_stream_task(
                asyncio.create_task(run_in_threadpool(iterator.close))
            )
            cancelled = cancelled or interrupted
            if error is not None:
                errors.append(error)

        if failure is None and cancelled is None and errors:
            raise errors[0]
        for error in errors:
            logger.error(
                "Workflow stream cleanup failed after its consumer stopped",
                exc_info=(type(error), error, error.__traceback__),
            )
        if failure is None and cancelled is not None:
            raise cancelled


@router.post("/briefs/workflow", response_model=MarketingWorkflowMiddleOutput)
def start_marketing_workflow(
    body: MarketingWorkflowInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    user_id = _preflight(db, user, body.brief_id, team_id=team_id)
    return start(user_id, body.brief_id, team_id=team_id)


@router.post("/briefs/workflow/stream")
def start_marketing_workflow_stream(
    body: MarketingWorkflowInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    user_id = _preflight(db, user, body.brief_id, team_id=team_id)
    return WorkflowStreamingResponse(
        _stream_body(iter_start_stream(user_id, body.brief_id, team_id=team_id)),
        media_type="application/x-ndjson",
        headers=STREAM_HEADERS,
    )


@router.post("/briefs/workflow/review", response_model=MarketingWorkflowMiddleOutput)
def review_marketing_workflow(
    body: MarketingWorkflowReviewInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    user_id = _preflight(db, user, body.brief_id, body, team_id=team_id)
    return review(user_id, body, team_id=team_id)


@router.post("/briefs/workflow/review/stream")
def review_marketing_workflow_stream(
    body: MarketingWorkflowReviewInput,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    team_id = member.team_id
    user_id = _preflight(db, user, body.brief_id, body, team_id=team_id)
    return WorkflowStreamingResponse(
        _stream_body(iter_review_stream(user_id, body, team_id=team_id)),
        media_type="application/x-ndjson",
        headers=STREAM_HEADERS,
    )


@router.get("/briefs/workflow", response_model=MarketingWorkflowMiddleOutput)
def get_marketing_workflow(
    brief_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    member: TeamMember = Depends(get_current_member_from_query),
):
    return get_workflow(db, user, brief_id, team_id=member.team_id)
