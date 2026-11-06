import type { CreatedTicket, TicketCreateBody } from '../api/ticket'

export type TicketScope = { userId: string; teamId: string }
type ScopeSnapshot = TicketScope & { epoch: number }

export function createTicketScopeGuard(scope: TicketScope) {
  let epoch = 0
  let active = false
  return {
    open() { active = true; ++epoch },
    close() { active = false; ++epoch },
    capture(): ScopeSnapshot { return { ...scope, epoch } },
    isCurrent(snapshot: ScopeSnapshot, current: TicketScope | null) {
      return active && snapshot.epoch === epoch && snapshot.userId === current?.userId && snapshot.teamId === current.teamId
    },
  }
}

export function receiptMatchesScope(ticket: CreatedTicket, scope: TicketScope) {
  return ticket.team_id === scope.teamId && ticket.created_by === scope.userId
}

export function reconcileTicketAssignments(draft: TicketCreateBody, members: readonly { user_id: string }[]) {
  const validIds = new Set(members.map((member) => member.user_id))
  const assigneeInvalid = Boolean(draft.assignee_id) && !validIds.has(draft.assignee_id)
  const reviewerInvalid = Boolean(draft.reviewer_id) && !validIds.has(draft.reviewer_id)
  return {
    draft: { ...draft, assignee_id: assigneeInvalid ? '' : draft.assignee_id, reviewer_id: reviewerInvalid ? '' : draft.reviewer_id },
    notice: [assigneeInvalid ? '原处理人已不在有效成员中，请重新选择。' : '', reviewerInvalid ? '原审核人已不在有效成员中，请重新选择。' : ''].filter(Boolean).join(''),
  }
}
