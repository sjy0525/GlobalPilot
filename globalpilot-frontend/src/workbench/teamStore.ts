import { create } from 'zustand'
import { PREVIEW_ACCOUNTS, PREVIEW_ADMIN_ID } from './team'
import { addTeamMember, changeTeamRole, createInitialPreviewState, reassignTicket, removeTeamMember, type Assignment, type MemberRemovalPlan, type PreviewState } from './model'

// A single snapshot keeps preview membership, assignments and the audit trail atomic.
const STORAGE_KEY = 'gp-workbench-preview-state-v3'
const knownAccount = (id: unknown) => typeof id === 'string' && PREVIEW_ACCOUNTS.some((account) => account.id === id)

function readState(): PreviewState {
  const initial = createInitialPreviewState()
  try {
    const saved = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) || 'null') as PreviewState | null
    if (saved && Array.isArray(saved.members) && saved.members.length > 0 && saved.members.every((member) => member && knownAccount(member.id) && ['admin', 'employee'].includes(member.role)) && saved.members.some((member) => member.role === 'admin') && new Set(saved.members.map((member) => member.id)).size === saved.members.length && Array.isArray(saved.tickets) && saved.tickets.length === initial.tickets.length && saved.tickets.every((ticket) => ticket && initial.tickets.some((item) => item.id === ticket.id) && ['processing', 'awaiting_review', 'needs_info', 'completed'].includes(ticket.status) && [ticket.assigneeId, ticket.reviewerId].every((id) => id === null || knownAccount(id)) && !Number.isNaN(Date.parse(ticket.updatedAt))) && Array.isArray(saved.logs) && saved.logs.every((log) => log && ['member_added', 'member_removed', 'role_changed', 'ticket_reassigned'].includes(log.action) && ['id', 'createdAt', 'actorId', 'actorName', 'targetId', 'targetName', 'before', 'after', 'reason'].every((key) => typeof log[key as keyof typeof log] === 'string'))) {
      return { members: saved.members.map((member) => ({ ...PREVIEW_ACCOUNTS.find((account) => account.id === member.id)!, role: member.role })), tickets: saved.tickets, logs: saved.logs }
    }
    // Retain assignments from the earlier preview without inventing historical logs.
    const previousMembers: unknown = JSON.parse(window.sessionStorage.getItem('gp-workbench-preview-team-v2') || 'null')
    if (Array.isArray(previousMembers)) initial.members = PREVIEW_ACCOUNTS.filter((account) => account.id === PREVIEW_ADMIN_ID || previousMembers.includes(account.id)).map((member) => ({ ...member }))
    const assignments: unknown = JSON.parse(window.sessionStorage.getItem('gp-workbench-preview-ticket-assignments-v1') || 'null')
    if (Array.isArray(assignments)) for (const assignment of assignments) {
      const ticket = initial.tickets.find((item) => item.id === assignment?.id)
      if (!ticket || !knownAccount(assignment.assigneeId) || !knownAccount(assignment.reviewerId)) continue
      const assignee = PREVIEW_ACCOUNTS.find((member) => member.id === assignment.assigneeId)!
      const reviewer = PREVIEW_ACCOUNTS.find((member) => member.id === assignment.reviewerId)!
      Object.assign(ticket, { assigneeId: assignee.id, assigneeName: assignee.name, reviewerId: reviewer.id, reviewerName: reviewer.name })
    }
  } catch { /* Obsolete or unavailable storage falls back to the examples. */ }
  return initial
}

type PreviewTeamState = PreviewState & {
  addMember: (actorId: string, targetId: string) => void
  changeRole: (actorId: string, targetId: string, role: 'admin' | 'employee', reason: string) => void
  removeMember: (actorId: string, targetId: string, plan: MemberRemovalPlan) => void
  assignTicket: (actorId: string, id: string, assignment: Assignment) => void
}
function persist(state: PreviewState) {
  try { window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state)) } catch { /* Preserve current in-memory preview if storage is unavailable. */ }
  return state
}
export const usePreviewTeamStore = create<PreviewTeamState>((set) => ({
  ...readState(),
  addMember: (actorId, targetId) => set((state) => persist(addTeamMember(state, actorId, targetId))),
  changeRole: (actorId, targetId, role, reason) => set((state) => persist(changeTeamRole(state, actorId, targetId, role, reason))),
  removeMember: (actorId, targetId, plan) => set((state) => persist(removeTeamMember(state, actorId, targetId, plan))),
  assignTicket: (actorId, id, assignment) => set((state) => persist(reassignTicket(state, actorId, id, assignment))),
}))
