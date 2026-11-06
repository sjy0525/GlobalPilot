from sqlalchemy.orm import Session,Query
from sqlalchemy import or_

from app.core.http_errors import AppHTTPException
from app.modules.tickets.models import Ticket
from app.modules.teams.authorization import lock_team, require_active_team_member

from uuid import UUID

#管理员可以查看全部工单，普通员工只能看自己涉及的工单
def get_visible_ticket(
    db:Session,
    team_id:UUID,
    actor_user_id:UUID
)->Query[Ticket]:
    member=require_active_team_member(
        db,team_id=team_id,user_id=actor_user_id
    )

    query=db.query(Ticket).filter(
        Ticket.team_id==team_id
    )
    if member.role=='admin':
        return query

    return query.filter(
        or_(
            Ticket.created_by==actor_user_id,
            Ticket.assignee_id==actor_user_id,
            Ticket.reviewer_id==actor_user_id
        )
    )


def lock_visible_ticket(
    db: Session,
    *,
    team_id: UUID,
    actor_user_id: UUID,
    ticket_id: UUID,
) -> Ticket:
    # Use the same Team -> Ticket lock order as other team mutations. Recheck
    # membership and refresh the Ticket after waiting for concurrent writers.
    lock_team(db, team_id=team_id)
    ticket = (
        get_visible_ticket(db, team_id, actor_user_id)
        .filter(Ticket.id == ticket_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if ticket is None:
        raise AppHTTPException(404, '工单不存在', code='TICKET_DETAIL_NOT_FOUND')
    return ticket
