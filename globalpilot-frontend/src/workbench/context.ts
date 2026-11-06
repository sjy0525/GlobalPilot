import { createContext, useContext } from 'react'
import type { TeamMember } from './team'
import type { Assignment, MemberRemovalPlan, OperationLog, WorkbenchTicket } from './model'
import type { MemberMutation, MemberMutationResult } from './liveTeamStore'

export type WorkbenchContextValue = {
  basePath: string
  displayName: string
  currentMemberId: string
  canAccessWorkbench: boolean
  canReview: boolean
  canManageTeam: boolean
  isPreview: boolean
  teamId: string | null
  teamName: string
  teamLoading: boolean
  membersLoading: boolean
  membersError: string
  memberMutation: MemberMutation | null
  refreshMembers: () => Promise<void>
  addMemberByEmail: (email: string) => Promise<MemberMutationResult<TeamMember>>
  updateTeamMemberRole: (memberId: string, role: TeamMember['role']) => Promise<MemberMutationResult<TeamMember>>
  removeTeamMember: (memberId: string) => Promise<MemberMutationResult<void>>
  teamMembers: TeamMember[]
  availablePeople: TeamMember[]
  tickets: WorkbenchTicket[]
  logs: OperationLog[]
  addMember: (memberId: string) => void
  removeMember: (memberId: string, plan: MemberRemovalPlan) => void
  changeMemberRole: (memberId: string, role: TeamMember['role'], reason: string) => void
  assignTicket: (id: string, assignment: Assignment) => void
}

export const WorkbenchContext = createContext<WorkbenchContextValue | null>(null)

export function useWorkbench() {
  const context = useContext(WorkbenchContext)
  if (!context) throw new Error('Workbench pages require WorkbenchShell.')
  return context
}
