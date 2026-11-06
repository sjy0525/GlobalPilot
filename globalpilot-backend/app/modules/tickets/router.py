from fastapi import APIRouter,Depends

from app.modules.tickets.schemas import (
    TicketRead, TicketCreate, TicketResultSubmit, TicketReview, TicketReassign,
    TicketAssignmentLogRead,
)
from app.modules.teams.models import TeamMember
from app.modules.teams.dependencies import get_current_member, require_admin
from app.db.database import get_db
from app.modules.tickets.service import (
    create_ticket, get_ticket_list, get_ticket_detail, submit_ticket_result,
    review_ticket, reassign_ticket,
    get_ticket_assignment_logs,
)
from sqlalchemy.orm import Session

from uuid import UUID

router=APIRouter(prefix='/team/{team_id}/tickets',tags=['ticket'])

#创建工单
@router.post('',response_model=TicketRead,status_code=201)
def create_team_ticket(
    body:TicketCreate,
    member:TeamMember=Depends(get_current_member),
    db:Session=Depends(get_db)
):
    try:
        res=create_ticket(
            db,
            member.team_id,
            member.user_id,
            body
        )
        db.commit()

    except Exception:
        db.rollback()
        raise

    return res


#获取工单列表
@router.get('',response_model=list[TicketRead])
def get_team_tickets(
    db:Session=Depends(get_db),
    member:TeamMember=Depends(get_current_member)
):

    res=get_ticket_list(db,member.team_id,member.user_id)
    return res

#获取具体的工单详情
@router.get('/{ticket_id}',response_model=TicketRead)
def get_team_ticket_detail(
    ticket_id:UUID,
    db:Session=Depends(get_db),
    member:TeamMember=Depends(get_current_member),
):
    return get_ticket_detail(db,member.team_id,member.user_id,ticket_id)


@router.post('/{ticket_id}/submit', response_model=TicketRead)
def submit_team_ticket_result(
    ticket_id: UUID,
    body: TicketResultSubmit,
    member: TeamMember = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    try:
        result = submit_ticket_result(
            db,
            team_id=member.team_id,
            actor_user_id=member.user_id,
            ticket_id=ticket_id,
            body=body,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


@router.get('/{ticket_id}/assignment-logs', response_model=list[TicketAssignmentLogRead])
def get_team_ticket_assignment_logs(
    ticket_id: UUID,
    member: TeamMember = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    return get_ticket_assignment_logs(
        db, team_id=member.team_id, actor_user_id=member.user_id,
        ticket_id=ticket_id,
    )


@router.post('/{ticket_id}/review', response_model=TicketRead)
def review_team_ticket(
    ticket_id: UUID,
    body: TicketReview,
    member: TeamMember = Depends(get_current_member),
    db: Session = Depends(get_db),
):
    try:
        result = review_ticket(
            db, team_id=member.team_id, actor_user_id=member.user_id,
            ticket_id=ticket_id, body=body,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


@router.patch('/{ticket_id}/assignment', response_model=TicketRead)
def reassign_team_ticket(
    ticket_id: UUID,
    body: TicketReassign,
    member: TeamMember = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        result = reassign_ticket(
            db, team_id=member.team_id, actor_user_id=member.user_id,
            ticket_id=ticket_id, body=body,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result
