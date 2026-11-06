from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core.http_errors import AppHTTPException
from app.modules.tickets.models import Ticket, TicketAssignmentAuditLog
from app.modules.tickets.schemas import (
    TicketCreate, TicketRead, TicketResultSubmit, TicketReview, TicketReassign,
    TicketAssignmentLogRead,
)
from app.modules.teams.authorization import (
    get_active_team_member_ids,
    lock_team_and_require_admin,
    lock_team_and_require_member,
)
from app.modules.tickets.authorization import get_visible_ticket, lock_visible_ticket

#创建工单
def create_ticket(
    db: Session,
    team_id: UUID,
    actor_user_id: UUID,
    body: TicketCreate,
) -> TicketRead:
    lock_team_and_require_member(db, team_id=team_id, actor_user_id=actor_user_id)

    assigned_user_ids = {body.assignee_id, body.reviewer_id}
    valid_user_ids = get_active_team_member_ids(
        db, team_id=team_id, user_ids=assigned_user_ids
    )
    if not assigned_user_ids.issubset(valid_user_ids):
        raise AppHTTPException(
            status_code=400,
            detail="处理人和审核人必须是当前团队的有效成员",
            code="INVALID_TICKET_ASSIGNMENT",
        )

    ticket = Ticket(
        team_id=team_id,
        title=body.title,
        description=body.description,
        created_by=actor_user_id,
        assignee_id=body.assignee_id,
        reviewer_id=body.reviewer_id,
        status="processing",
    )

    db.add(ticket)
    db.flush()
    db.refresh(ticket)
    return TicketRead.model_validate(ticket)


def get_ticket_assignment_logs(
    db: Session,
    *,
    team_id: UUID,
    actor_user_id: UUID,
    ticket_id: UUID,
) -> list[TicketAssignmentLogRead]:
    # Keep visibility in the log-reading SQL. A historical assignment alone
    # never grants access, and a visible ticket with no logs still returns [].
    rows = (
        get_visible_ticket(db, team_id, actor_user_id)
        .add_entity(TicketAssignmentAuditLog)
        .outerjoin(
            TicketAssignmentAuditLog,
            and_(
                TicketAssignmentAuditLog.ticket_id == Ticket.id,
                TicketAssignmentAuditLog.team_id == Ticket.team_id,
            ),
        )
        .filter(Ticket.id == ticket_id)
        .order_by(
            TicketAssignmentAuditLog.created_at.asc(),
            TicketAssignmentAuditLog.id.asc(),
        )
        .all()
    )
    if not rows:
        raise AppHTTPException(404, '工单不存在', code='TICKET_DETAIL_NOT_FOUND')
    return [
        TicketAssignmentLogRead.model_validate(log)
        for _, log in rows
        if log is not None
    ]


def submit_ticket_result(
    db: Session,
    *,
    team_id: UUID,
    actor_user_id: UUID,
    ticket_id: UUID,
    body: TicketResultSubmit,
) -> TicketRead:
    ticket = lock_visible_ticket(
        db, team_id=team_id, actor_user_id=actor_user_id, ticket_id=ticket_id,
    )
    if ticket.assignee_id != actor_user_id:
        raise AppHTTPException(
            403, '只有当前处理人可以提交处理结果', code='TICKET_ASSIGNEE_REQUIRED',
        )
    if ticket.status not in ('processing', 'needs_info'):
        raise AppHTTPException(
            409, '只有处理中或待补充的工单可以提交处理结果', code='TICKET_STATUS_CONFLICT',
        )

    ticket.processing_result = body.processing_result
    ticket.status = 'awaiting_review'
    db.flush()
    db.refresh(ticket)
    return TicketRead.model_validate(ticket)


def review_ticket(
    db: Session,
    *,
    team_id: UUID,
    actor_user_id: UUID,
    ticket_id: UUID,
    body: TicketReview,
) -> TicketRead:
    ticket = lock_visible_ticket(
        db, team_id=team_id, actor_user_id=actor_user_id, ticket_id=ticket_id,
    )
    if ticket.reviewer_id != actor_user_id:
        raise AppHTTPException(
            403, '只有当前审核人可以审核工单', code='TICKET_REVIEWER_REQUIRED',
        )
    if ticket.status != 'awaiting_review':
        raise AppHTTPException(
            409, '只有待审核的工单可以审核', code='TICKET_STATUS_CONFLICT',
        )
    if ticket.updated_at != body.expected_updated_at:
        raise AppHTTPException(
            409, '工单已更新，请重新查看后再审核', code='TICKET_REVIEW_OUTDATED',
        )

    ticket.review_comment = body.comment or None
    ticket.status = 'completed' if body.decision == 'approve' else 'needs_info'
    db.flush()
    db.refresh(ticket)
    return TicketRead.model_validate(ticket)


def reassign_ticket(
    db: Session,
    *,
    team_id: UUID,
    actor_user_id: UUID,
    ticket_id: UUID,
    body: TicketReassign,
) -> TicketRead:
    lock_team_and_require_admin(db, team_id=team_id, actor_user_id=actor_user_id)
    ticket = (
        db.query(Ticket)
        .filter(Ticket.team_id == team_id, Ticket.id == ticket_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if ticket is None:
        raise AppHTTPException(404, '工单不存在', code='TICKET_DETAIL_NOT_FOUND')
    if ticket.status == 'completed':
        raise AppHTTPException(
            409, '已完成的工单不能改派', code='TICKET_STATUS_CONFLICT',
        )
    if ticket.updated_at != body.expected_updated_at:
        raise AppHTTPException(
            409, '工单已更新，请重新查看后再改派', code='TICKET_ASSIGNMENT_OUTDATED',
        )

    assigned_user_ids = {body.assignee_id, body.reviewer_id}
    valid_user_ids = get_active_team_member_ids(
        db, team_id=team_id, user_ids=assigned_user_ids,
    )
    if not assigned_user_ids.issubset(valid_user_ids):
        raise AppHTTPException(
            400, '处理人和审核人必须是当前团队的有效成员',
            code='INVALID_TICKET_ASSIGNMENT',
        )

    before_assignee_id = ticket.assignee_id
    before_reviewer_id = ticket.reviewer_id
    if (before_assignee_id, before_reviewer_id) == (body.assignee_id, body.reviewer_id):
        return TicketRead.model_validate(ticket)

    ticket.assignee_id = body.assignee_id
    ticket.reviewer_id = body.reviewer_id
    db.add(TicketAssignmentAuditLog(
        team_id=team_id,
        ticket_id=ticket.id,
        actor_user_id=actor_user_id,
        before_assignee_id=before_assignee_id,
        after_assignee_id=ticket.assignee_id,
        before_reviewer_id=before_reviewer_id,
        after_reviewer_id=ticket.reviewer_id,
    ))
    db.flush()
    db.refresh(ticket)
    return TicketRead.model_validate(ticket)


#获取工单列表
def get_ticket_list(
    db:Session,
    team_id:UUID,
    actor_user_id:UUID
)->list[TicketRead]:
    query=get_visible_ticket(db,team_id,actor_user_id)
    tickets=query.order_by(
        Ticket.created_at.desc(),
        Ticket.id.desc()
    ).all()

    return [
        TicketRead.model_validate(ticket)
        for ticket in tickets
    ]

#查询具体的工单详情
def get_ticket_detail(
    db:Session,
    team_id:UUID,
    actor_user_id:UUID,
    ticket_id:UUID
)->TicketRead:
    query=get_visible_ticket(db,team_id,actor_user_id)
    ticket=query.filter(ticket_id==Ticket.id).one_or_none()
    if ticket is None:
        raise AppHTTPException(
            status_code=404,
            detail='工单不存在',
            code='TICKET_DETAIL_NOT_FOUND'
        )
    return TicketRead.model_validate(ticket)
