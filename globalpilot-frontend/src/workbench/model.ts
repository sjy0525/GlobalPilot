import { INITIAL_MEMBER_IDS, PREVIEW_ACCOUNTS, type TeamMember } from './team.ts'

export type TicketStatus = 'processing' | 'awaiting_review' | 'needs_info' | 'completed'
export type WorkbenchTicket = {
  id: string; title: string; category: string; status: TicketStatus
  creatorId: string; creatorName: string
  assigneeId: string | null; assigneeName: string | null
  reviewerId: string | null; reviewerName: string | null
  deadline: string; dueToday?: boolean; createdAt: string; updatedAt: string
}
export type OperationLog = {
  id: string; createdAt: string; actorId: string; actorName: string
  action: 'member_added' | 'member_removed' | 'role_changed' | 'ticket_reassigned'
  targetType: 'member' | 'ticket'; targetId: string; targetName: string
  before: string; after: string; reason: string
}
export type Assignment = { assigneeId: string | null; reviewerId: string | null; reason: string }
export type MemberRemovalPlan = Assignment & { mode: 'reassign' | 'queue' }
export type PreviewState = { members: TeamMember[]; tickets: WorkbenchTicket[]; logs: OperationLog[] }
export const ACTION_LABELS: Record<OperationLog['action'], string> = {
  member_added: '添加成员', member_removed: '移除成员', role_changed: '调整身份', ticket_reassigned: '工单改派',
}
export const STATUS_LABELS: Record<TicketStatus, string> = { processing: '处理中', awaiting_review: '待审核', needs_info: '待补充', completed: '已完成' }

export function createInitialPreviewState(now = Date.now()): PreviewState {
  const members = PREVIEW_ACCOUNTS.filter((account) => INITIAL_MEMBER_IDS.includes(account.id)).map((member) => ({ ...member }))
  const person = (id: string | null) => members.find((member) => member.id === id)?.name || null
  const specs: [string, string, string, TicketStatus, string, string | null, string | null, string, number][] = [
    ['GP-20261024-001', '品牌官网首页文案更新', '内容需求', 'processing', 'member-lin', 'member-chen', 'member-xu', '今天 18:00', 1],
    ['GP-20261024-002', '深圳行业峰会宣传物料', '设计需求', 'awaiting_review', 'member-chen', 'member-zhou', 'member-lin', '10月26日', 2],
    ['GP-20261023-003', '产品介绍资料信息补充', '资料维护', 'needs_info', 'member-xu', 'member-chen', 'member-zhou', '10月27日', 9],
    ['GP-20261023-004', '11月社交媒体内容配图', '设计需求', 'processing', 'member-zhou', 'member-xu', 'member-chen', '10月28日', 1],
    ['GP-20261022-005', '客户沙龙活动方案确认', '活动需求', 'awaiting_review', 'member-lin', 'member-zhou', 'member-chen', '今天 17:00', 8],
    ['GP-20261022-006', '新品发布邮件内容制作', '内容需求', 'processing', 'member-chen', 'member-xu', 'member-lin', '10月29日', 2],
    ['GP-20261021-007', '合作伙伴品牌素材交付', '资料申请', 'completed', 'member-chen', 'member-zhou', 'member-xu', '10月23日', 3],
    ['GP-20261020-008', '季度产品手册视觉调整', '设计需求', 'completed', 'member-xu', 'member-zhou', 'member-lin', '10月22日', 4],
    ['GP-20261024-009', '双十一活动落地页设计', '设计需求', 'processing', 'member-lin', null, 'member-xu', '10月30日', 1],
    ['GP-20261024-010', '品牌合作推文确认', '内容需求', 'awaiting_review', 'member-xu', 'member-chen', null, '10月28日', 8],
  ]
  return { members, logs: [], tickets: specs.map(([id, title, category, status, creatorId, assigneeId, reviewerId, deadline, age]) => ({
    id, title, category, status, creatorId, creatorName: person(creatorId)!, assigneeId, assigneeName: person(assigneeId), reviewerId, reviewerName: person(reviewerId), deadline,
    dueToday: deadline.startsWith('今天'), createdAt: new Date(now - (age + 2) * 86400000).toISOString(), updatedAt: new Date(now - age * 86400000).toISOString(),
  })) }
}

export function getUnassignedSides(ticket: WorkbenchTicket, members: readonly TeamMember[]) {
  const active = (id: string | null) => !!id && members.some((member) => member.id === id)
  return { assignee: ticket.status !== 'completed' && !active(ticket.assigneeId), reviewer: ticket.status !== 'completed' && !active(ticket.reviewerId) }
}
export function isStaleTicket(ticket: WorkbenchTicket, now = Date.now()) {
  return ticket.status !== 'completed' && now - Date.parse(ticket.updatedAt) >= 7 * 86400000
}
export function canApproveTicket(ticket: WorkbenchTicket, actorId: string, members: readonly TeamMember[]) {
  return ticket.status === 'awaiting_review' && ticket.reviewerId === actorId && members.some((member) => member.id === actorId)
}
function requireMember(state: PreviewState, id: string) {
  const member = state.members.find((candidate) => candidate.id === id)
  if (!member) throw new Error('该成员已经不在团队中。')
  return member
}
function requireAdmin(state: PreviewState, id: string) {
  const actor = requireMember(state, id)
  if (actor.role !== 'admin') throw new Error('仅团队管理员可以管理团队。')
  return actor
}
function requireReason(reason: string) {
  if (!reason.trim()) throw new Error('请填写改派或移除原因。')
  return reason.trim()
}
function protectLastAdmin(state: PreviewState, target: TeamMember) {
  if (target.role === 'admin' && state.members.filter((member) => member.role === 'admin').length <= 1) throw new Error('团队必须保留至少一名管理员。')
}
function logEntry(actor: TeamMember, fields: Omit<OperationLog, 'id' | 'createdAt' | 'actorId' | 'actorName'>, now: number): OperationLog {
  return { ...fields, id: globalThis.crypto.randomUUID(), createdAt: new Date(now).toISOString(), actorId: actor.id, actorName: actor.name }
}
function assignmentText(ticket: WorkbenchTicket) {
  return `处理人：${ticket.assigneeName || '未分配'}；审核人：${ticket.reviewerName || '未分配'}`
}
function withAssignment(state: PreviewState, ticket: WorkbenchTicket, assignment: Assignment, excludedId?: string) {
  const lookup = (id: string | null) => {
    if (id === null) return null
    if (id === excludedId) throw new Error('不能将任务改派给即将移除的成员。')
    return requireMember(state, id)
  }
  const assignee = lookup(assignment.assigneeId)
  const reviewer = lookup(assignment.reviewerId)
  return { ...ticket, assigneeId: assignee?.id || null, assigneeName: assignee?.name || null, reviewerId: reviewer?.id || null, reviewerName: reviewer?.name || null }
}
function reassignmentLog(actor: TeamMember, before: WorkbenchTicket, after: WorkbenchTicket, reason: string, now: number) {
  return logEntry(actor, { action: 'ticket_reassigned', targetType: 'ticket', targetId: before.id, targetName: before.title, before: assignmentText(before), after: assignmentText(after), reason }, now)
}

export function reassignTicket(state: PreviewState, actorId: string, id: string, assignment: Assignment, now = Date.now()): PreviewState {
  const actor = requireMember(state, actorId)
  const ticket = state.tickets.find((candidate) => candidate.id === id)
  if (!ticket) throw new Error('工单不存在。')
  if (actor.role !== 'admin' && ticket.creatorId !== actorId) throw new Error('仅创建人或管理员可以改派工单。')
  if (ticket.status === 'completed') throw new Error('已完成的工单不能改派。')
  const reason = requireReason(assignment.reason)
  const updated = withAssignment(state, ticket, assignment)
  if (updated.assigneeId === ticket.assigneeId && updated.reviewerId === ticket.reviewerId) throw new Error('请先调整处理人或审核人。')
  updated.updatedAt = new Date(now).toISOString()
  return { ...state, tickets: state.tickets.map((item) => item.id === id ? updated : item), logs: [reassignmentLog(actor, ticket, updated, reason, now), ...state.logs] }
}
export function addTeamMember(state: PreviewState, actorId: string, targetId: string, now = Date.now()): PreviewState {
  const actor = requireAdmin(state, actorId)
  if (state.members.some((member) => member.id === targetId)) throw new Error('该成员已经在团队中。')
  const account = PREVIEW_ACCOUNTS.find((person) => person.id === targetId)
  if (!account) throw new Error('未找到这个账号。')
  const target: TeamMember = { ...account, role: 'employee' }
  return { ...state, members: [...state.members, target], logs: [logEntry(actor, { action: 'member_added', targetType: 'member', targetId, targetName: target.name, before: '未加入团队', after: '员工', reason: '管理员添加团队成员' }, now), ...state.logs] }
}
export function changeTeamRole(state: PreviewState, actorId: string, targetId: string, role: TeamMember['role'], reason: string, now = Date.now()): PreviewState {
  const actor = requireAdmin(state, actorId)
  const target = requireMember(state, targetId)
  if (role !== 'admin' && role !== 'employee') throw new Error('身份只能是管理员或员工。')
  if (target.role === role) throw new Error('成员身份没有变化。')
  if (role === 'employee') protectLastAdmin(state, target)
  return { ...state, members: state.members.map((member) => member.id === targetId ? { ...member, role } : member), logs: [logEntry(actor, { action: 'role_changed', targetType: 'member', targetId, targetName: target.name, before: target.role === 'admin' ? '管理员' : '员工', after: role === 'admin' ? '管理员' : '员工', reason: reason.trim() || '团队身份调整' }, now), ...state.logs] }
}
export function removeTeamMember(state: PreviewState, actorId: string, targetId: string, plan: MemberRemovalPlan, now = Date.now()): PreviewState {
  const actor = requireAdmin(state, actorId)
  const target = requireMember(state, targetId)
  protectLastAdmin(state, target)
  const reason = requireReason(plan.reason)
  if (plan.mode !== 'queue' && plan.mode !== 'reassign') throw new Error('请选择任务交接方式。')
  const changes: OperationLog[] = []
  const tickets = state.tickets.map((ticket) => {
    if (ticket.status === 'completed' || (ticket.assigneeId !== targetId && ticket.reviewerId !== targetId)) return ticket
    const processing = ticket.assigneeId === targetId
    const reviewing = ticket.reviewerId === targetId
    if (plan.mode === 'reassign' && ((processing && !plan.assigneeId) || (reviewing && !plan.reviewerId))) throw new Error('请为未完成任务指定接手成员。')
    const updated = withAssignment(state, ticket, {
      assigneeId: processing ? (plan.mode === 'queue' ? null : plan.assigneeId) : ticket.assigneeId,
      reviewerId: reviewing ? (plan.mode === 'queue' ? null : plan.reviewerId) : ticket.reviewerId,
      reason,
    }, targetId)
    updated.updatedAt = new Date(now).toISOString()
    changes.push(reassignmentLog(actor, ticket, updated, `移除${target.name}：${reason}`, now))
    return updated
  })
  return { members: state.members.filter((member) => member.id !== targetId), tickets, logs: [logEntry(actor, { action: 'member_removed', targetType: 'member', targetId, targetName: target.name, before: target.role === 'admin' ? '管理员' : '员工', after: '已移出团队', reason: `${reason}；${plan.mode === 'queue' ? '未完成任务移入待分配' : '未完成任务已交接'}` }, now), ...changes, ...state.logs] }
}
