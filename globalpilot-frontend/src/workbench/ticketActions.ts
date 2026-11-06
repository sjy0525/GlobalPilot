import { ApiError } from '../api/http'
import type { TicketRead } from '../api/ticket'
import type { TicketReadScope } from './ticketReading'

export type TicketActionKind = 'submit' | 'review' | 'assignment'
export type TicketActionScope = TicketReadScope & { ticketId: string }

export function ticketActionPermissions(ticket: TicketRead | null, userId: string, role: TicketReadScope['role']) {
  return {
    submit: Boolean(ticket && ticket.assignee_id === userId && (ticket.status === 'processing' || ticket.status === 'needs_info')),
    review: Boolean(ticket && ticket.reviewer_id === userId && ticket.status === 'awaiting_review'),
    assignment: Boolean(ticket && role === 'admin' && ticket.status !== 'completed'),
  }
}

export function createTicketActionGuard(scope: TicketActionScope) {
  let active = false
  let generation = 0
  let busy = false
  return {
    open() { active = true; ++generation },
    close() { active = false; busy = false; ++generation },
    capture() { return { ...scope, generation } },
    isCurrent(snapshot: TicketActionScope & { generation: number }, current: TicketActionScope | null) {
      return active && snapshot.generation === generation && snapshot.userId === current?.userId && snapshot.teamId === current.teamId && snapshot.role === current.role && snapshot.ticketId === current.ticketId
    },
    begin() { if (!active || busy) return false; busy = true; return true },
    finish() { busy = false },
  }
}

export function ticketActionFailure(error: unknown) {
  const code = error instanceof ApiError ? error.code : undefined
  const status = error instanceof ApiError ? error.status : 0
  const assignmentChanged = code === 'TICKET_ASSIGNEE_REQUIRED' || code === 'TICKET_REVIEWER_REQUIRED'
  return {
    unknown: status === 0 || status >= 500,
    refreshTicket: status === 409 || assignmentChanged || status === 404,
    refreshIdentity: status === 401 || (status === 403 && !assignmentChanged),
    clearSensitive: status === 401 || (status === 403 && !assignmentChanged && code !== 'ADMIN_REQUIRED') || status === 404,
    refreshCandidates: code === 'INVALID_TICKET_ASSIGNMENT',
    conflict: status === 409 || assignmentChanged,
  }
}

export function reconcileActionAssignments(draft: { assignee_id: string; reviewer_id: string }, people: readonly { user_id: string }[]) {
  const ids = new Set(people.map((person) => person.user_id))
  const assignee = draft.assignee_id && !ids.has(draft.assignee_id)
  const reviewer = draft.reviewer_id && !ids.has(draft.reviewer_id)
  return {
    assignee_id: assignee ? '' : draft.assignee_id,
    reviewer_id: reviewer ? '' : draft.reviewer_id,
    notice: assignee || reviewer ? '失效的指派已清除，请重新选择。其他填写内容已保留。' : '',
  }
}

export function actionSnapshotChanged(expected: string, ticket: TicketRead | null) {
  return Boolean(ticket && expected && expected !== ticket.updated_at)
}

export function ticketActionReceiptMatches(ticket: TicketRead, scope: TicketActionScope) {
  return ticket.team_id === scope.teamId && ticket.id === scope.ticketId
}
