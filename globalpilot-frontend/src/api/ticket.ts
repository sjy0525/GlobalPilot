import { ApiError, apiJson } from './http'

export type TicketCreateBody = {
  title: string
  description: string
  assignee_id: string
  reviewer_id: string
}

export type TicketRead = TicketCreateBody & {
  id: string
  team_id: string
  created_by: string
  status: 'processing' | 'awaiting_review' | 'needs_info' | 'completed'
  created_at: string
  updated_at: string
  processing_result: string | null
  review_comment: string | null
}

export type CreatedTicket = TicketRead

export type TicketAssignmentLogRead = {
  id: string
  team_id: string
  ticket_id: string
  actor_user_id: string
  before_assignee_id: string
  after_assignee_id: string
  before_reviewer_id: string
  after_reviewer_id: string
  created_at: string
}

export function fetchTeamTickets(teamId: string, signal?: AbortSignal) {
  return apiJson<TicketRead[]>(`/api/team/${encodeURIComponent(teamId)}/tickets`, { signal })
}

export function fetchTeamTicket(teamId: string, ticketId: string, signal?: AbortSignal) {
  return apiJson<TicketRead>(`/api/team/${encodeURIComponent(teamId)}/tickets/${encodeURIComponent(ticketId)}`, { signal })
}

export function fetchTicketAssignmentLogs(teamId: string, ticketId: string, signal?: AbortSignal) {
  return apiJson<TicketAssignmentLogRead[]>(`${ticketPath(teamId, ticketId)}/assignment-logs`, { signal })
}

export function ticketAssignmentLogsErrorMessage(error: unknown) {
  if (!(error instanceof ApiError)) return '暂时无法读取改派记录，请重新加载。'
  if (error.code === 'TICKET_DETAIL_NOT_FOUND' || error.code === 'TICKET_NOT_FOUND' || error.status === 404) return '工单不存在或你无权访问。'
  if (error.code === 'ACCOUNT_DISABLED') return '当前账号已停用，无法读取改派记录。'
  if (error.status === 401) return '登录已失效，请重新登录。'
  if (error.status === 403) return '团队访问权限已失效，无法读取改派记录。'
  if (error.status === 0) return '无法连接服务器，改派记录尚未读取。请检查网络后重新加载。'
  if (error.status === 422) return '改派记录请求无效，请检查工单地址后重新加载。'
  return '暂时无法读取改派记录，请重新加载。'
}

export function ticketReadErrorMessage(error: unknown, view: 'list' | 'detail' = 'list') {
  if (!(error instanceof ApiError)) return view === 'detail' ? '暂时无法读取工单详情，请稍后重试。' : '暂时无法读取工单列表，请稍后重试。'
  if (error.code === 'TICKET_DETAIL_NOT_FOUND' || error.code === 'TICKET_NOT_FOUND' || (view === 'detail' && error.status === 404)) return '工单不存在或你无权访问。'
  if (error.code === 'ACCOUNT_DISABLED') return '当前账号已停用，无法使用团队工作空间。'
  if (error.status === 401) return '登录已失效，请重新登录。'
  if (error.status === 403) return '团队访问权限已失效，请刷新团队身份。'
  if (error.status === 404) return '团队不存在，或你已不在该团队中。'
  if (error.status === 422) return view === 'detail' ? '工单地址无效，请检查链接后重试。' : '工单请求无效，请刷新后重试。'
  if (error.status === 0) return '无法连接服务器，请检查网络后重试。'
  return view === 'detail' ? '暂时无法读取工单详情，请稍后重试。' : '暂时无法读取工单列表，请稍后重试。'
}

export function buildTicketCreateBody(draft: TicketCreateBody): TicketCreateBody {
  return {
    title: draft.title.trim(),
    description: draft.description.trim(),
    assignee_id: draft.assignee_id,
    reviewer_id: draft.reviewer_id,
  }
}

export function createTeamTicket(teamId: string, draft: TicketCreateBody, signal?: AbortSignal) {
  return apiJson<CreatedTicket>(`/api/team/${encodeURIComponent(teamId)}/tickets`, {
    method: 'POST',
    body: JSON.stringify(buildTicketCreateBody(draft)),
    signal,
  })
}

export type TicketReviewBody = {
  decision: 'approve' | 'return'
  comment: string
  expected_updated_at: string
}

export type TicketAssignmentBody = {
  assignee_id: string
  reviewer_id: string
  expected_updated_at: string
}

function ticketPath(teamId: string, ticketId: string) {
  return `/api/team/${encodeURIComponent(teamId)}/tickets/${encodeURIComponent(ticketId)}`
}

export function submitTeamTicket(teamId: string, ticketId: string, processingResult: string, signal?: AbortSignal) {
  return apiJson<TicketRead>(`${ticketPath(teamId, ticketId)}/submit`, {
    method: 'POST', body: JSON.stringify({ processing_result: processingResult.trim() }), signal,
  })
}

export function reviewTeamTicket(teamId: string, ticketId: string, draft: TicketReviewBody, signal?: AbortSignal) {
  return apiJson<TicketRead>(`${ticketPath(teamId, ticketId)}/review`, {
    method: 'POST',
    body: JSON.stringify({ decision: draft.decision, comment: draft.comment.trim(), expected_updated_at: draft.expected_updated_at }),
    signal,
  })
}

export function reassignTeamTicket(teamId: string, ticketId: string, draft: TicketAssignmentBody, signal?: AbortSignal) {
  return apiJson<TicketRead>(`${ticketPath(teamId, ticketId)}/assignment`, {
    method: 'PATCH',
    body: JSON.stringify({ assignee_id: draft.assignee_id, reviewer_id: draft.reviewer_id, expected_updated_at: draft.expected_updated_at }),
    signal,
  })
}

export function ticketActionErrorMessage(error: unknown) {
  if (!(error instanceof ApiError)) return '暂时无法确认操作结果，请先刷新工单核对，勿重复提交。'
  if (error.code === 'TICKET_ASSIGNEE_REQUIRED') return '当前处理人已变化，只有当前处理人可以提交结果。请刷新工单。'
  if (error.code === 'TICKET_REVIEWER_REQUIRED') return '当前审核人已变化，只有当前审核人可以审核。请刷新工单。'
  if (error.code === 'TICKET_REVIEW_OUTDATED' || error.code === 'TICKET_ASSIGNMENT_OUTDATED') return '工单已更新，本次操作未执行。请查看最新内容，确认后再操作。'
  if (error.code === 'TICKET_STATUS_CONFLICT') return '工单状态已变化，本次操作未执行。请刷新并确认最新状态。'
  if (error.code === 'INVALID_TICKET_ASSIGNMENT') return '处理人或审核人已失效，请重新选择有效成员。草稿已保留。'
  if (error.code === 'ADMIN_REQUIRED') return '你已没有团队管理权限，无法改派工单。'
  if (error.code === 'ACCOUNT_DISABLED') return '当前账号已停用，无法使用团队工作空间。'
  if (error.status === 401) return '登录已失效，请重新登录。'
  if (error.status === 403) return '团队访问权限已失效，请刷新团队身份。'
  if (error.status === 404) return '工单不存在或你无权访问。'
  if (error.status === 422) return '请填写有效内容；退回工单时必须填写审核意见。'
  if (error.status === 0 || error.status >= 500) return '暂时无法确认操作结果，请先刷新工单核对，勿重复提交。'
  return '操作未完成，请刷新工单后重试。'
}

export function ticketErrorMessage(error: unknown, members = false) {
  if (!(error instanceof ApiError)) return '暂时无法完成请求，请稍后重试。'
  if (error.code === 'INVALID_TICKET_ASSIGNMENT') return '处理人或审核人已失效，请刷新成员后重新选择。'
  if (error.code === 'ACCOUNT_DISABLED') return '当前账号已停用，无法使用团队工作空间。'
  if (error.status === 401) return '登录已失效，请重新登录。'
  if (error.status === 403) return '团队访问权限已失效，请刷新团队身份。'
  if (error.status === 404) return '团队不存在，或你已不在该团队中。'
  if (error.status === 422) return '请填写工单标题，并选择有效的处理人和审核人。'
  if (error.status === 0) return members ? '无法连接服务器，请检查网络后重试。' : '连接中断，暂时无法确认创建结果，请勿重复提交。'
  return members ? '暂时无法读取团队成员，请重试。' : '暂时无法创建工单，请稍后重试。'
}
