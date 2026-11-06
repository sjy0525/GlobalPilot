import type { TeamMemberOption } from '../api/team'
import type { TicketRead } from '../api/ticket'
import { ApiError } from '../api/http'

export type TicketReadScope = { userId: string; teamId: string; role: 'admin' | 'employee' }
type ReadSnapshot = TicketReadScope & { resource: string; epoch: number }

export function observeTicketReadAccessFailure<T>(request: Promise<T>, onDenied: (failure: ApiError) => void) {
  return request.catch((failure: unknown) => {
    if (failure instanceof ApiError && (failure.status === 401 || failure.status === 403 || failure.status === 404 || failure.code === 'ACCOUNT_DISABLED')) onDenied(failure)
    throw failure
  })
}

export function createTicketReadGuard(scope: TicketReadScope, resource: string, generation = 0) {
  let active = false
  let epoch = generation
  return {
    open() { active = true; ++epoch },
    close() { active = false; ++epoch },
    capture(): ReadSnapshot { return { ...scope, resource, epoch } },
    isCurrent(snapshot: ReadSnapshot, current: TicketReadScope | null, currentResource: string) {
      return active && snapshot.epoch === epoch && snapshot.resource === currentResource && snapshot.userId === current?.userId && snapshot.teamId === current.teamId && snapshot.role === current.role
    },
  }
}

export const ticketStatusLabels: Record<TicketRead['status'], string> = { processing: '处理中', awaiting_review: '待审核', needs_info: '待补充', completed: '已完成' }

export function ticketMatchesReadScope(ticket: TicketRead, teamId: string, ticketId?: string) {
  return ticket.team_id === teamId && (!ticketId || ticket.id === ticketId)
}

export function ticketPerson(userId: string, members: readonly TeamMemberOption[], namesLoaded: boolean) {
  const member = members.find((item) => item.user_id === userId)
  return member
    ? { name: member.nickname?.trim() || member.email.split('@')[0], detail: member.email, historical: false }
    : { name: namesLoaded ? '历史成员' : '成员信息暂不可用', detail: userId.slice(0, 8), historical: namesLoaded }
}

export function formatTicketTime(value: string) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '时间信息暂不可用'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

export function filterTicketRows(tickets: readonly TicketRead[], query: string, status: string, members: readonly TeamMemberOption[]) {
  const needle = query.trim().toLocaleLowerCase()
  const people = new Map(members.map((member) => [member.user_id, [member.nickname, member.email].filter(Boolean).join(' ')]))
  return tickets.filter((ticket) => (status === 'all' || ticket.status === status) && (!needle || [ticket.id, ticket.title, ticket.description, ...[ticket.created_by, ticket.assignee_id, ticket.reviewer_id].flatMap((id) => [id, people.get(id) || ''])].join(' ').toLocaleLowerCase().includes(needle)))
}
