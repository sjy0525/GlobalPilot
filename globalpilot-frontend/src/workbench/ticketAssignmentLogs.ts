import { ApiError } from '../api/http'
import { fetchTeamTicket, fetchTicketAssignmentLogs, ticketAssignmentLogsErrorMessage, type TicketAssignmentLogRead, type TicketRead } from '../api/ticket'

export type TicketAssignmentLogsResult =
  | { status: 'ready'; rows: TicketAssignmentLogRead[] }
  | { status: 'error'; message: string }

export type TicketDetailWithLogs = {
  ticket: TicketRead
  assignmentLogs: TicketAssignmentLogsResult
}

const logFields = ['id', 'team_id', 'ticket_id', 'actor_user_id', 'before_assignee_id', 'after_assignee_id', 'before_reviewer_id', 'after_reviewer_id', 'created_at'] as const

export function assignmentLogsMatchScope(rows: unknown, teamId: string, ticketId: string): rows is TicketAssignmentLogRead[] {
  return Array.isArray(rows) && rows.every((row: unknown) => {
    if (!row || typeof row !== 'object') return false
    const record = row as Record<string, unknown>
    return logFields.every((field) => typeof record[field] === 'string' && record[field] !== '') && record.team_id === teamId && record.ticket_id === ticketId
  })
}

export function isAssignmentLogAccessFailure(error: unknown) {
  return error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404 || error.code === 'ACCOUNT_DISABLED')
}

// Detail, history and member names share the caller's one guarded read cycle.
// A history outage is visible locally; an access denial invalidates all data.
export async function fetchTicketDetailWithLogs(teamId: string, ticketId: string, signal?: AbortSignal): Promise<TicketDetailWithLogs> {
  const logRequest: Promise<TicketAssignmentLogsResult> = fetchTicketAssignmentLogs(teamId, ticketId, signal).then(
    (rows) => assignmentLogsMatchScope(rows, teamId, ticketId)
      ? { status: 'ready', rows }
      : { status: 'error', message: '返回的改派记录与当前工单不一致，已停止显示。请重新加载。' },
    (error: unknown) => {
      if (isAssignmentLogAccessFailure(error)) throw error
      return { status: 'error', message: ticketAssignmentLogsErrorMessage(error) }
    },
  )
  const [ticket, assignmentLogs] = await Promise.all([fetchTeamTicket(teamId, ticketId, signal), logRequest])
  return { ticket, assignmentLogs }
}
