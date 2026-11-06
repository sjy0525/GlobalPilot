import { create } from 'zustand'
import { ApiError } from '../api/http'
import { addTeamMemberByEmail, fetchMyTeams, fetchTeamMembers, fetchTeamMembership, removeTeamMember, teamErrorMessage, updateTeamMemberRole, type TeamMemberOption, type TeamMembership } from '../api/team'
import { useAuthStore } from '../store/authStore'

type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'
export type MemberMutation = 'add' | 'role' | 'remove'
export type MemberMutationResult<T> = { value: T; currentScope: boolean }
type LiveTeamState = {
  userId: string | null
  teams: TeamMembership[]
  currentTeamId: string | null
  membership: TeamMembership | null
  status: LoadStatus
  error: string
  members: TeamMemberOption[]
  membersStatus: LoadStatus
  membersError: string
  memberMutation: MemberMutation | null
  reset: (userId?: string | null) => void
  refreshTeams: (userId: string, force?: boolean) => Promise<void>
  selectTeam: (teamId: string) => Promise<void>
  refreshMembers: () => Promise<void>
  addMember: (email: string) => Promise<MemberMutationResult<TeamMemberOption>>
  changeMemberRole: (userId: string, role: TeamMemberOption['role']) => Promise<MemberMutationResult<TeamMemberOption>>
  removeMember: (userId: string) => Promise<MemberMutationResult<void>>
}

let identityVersion = 0
let membersVersion = 0
let mutationVersion = 0
let identityInFlight: Promise<void> | null = null
let mutationController: AbortController | null = null

function cancelMutation() {
  ++mutationVersion
  mutationController?.abort()
  mutationController = null
}

function currentUserIs(userId: string) {
  const auth = useAuthStore.getState()
  return auth.isAuthenticated && auth.user?.id === userId
}

function savedTeam(userId: string) {
  try { return window.sessionStorage.getItem(`gp-team-selection:${userId}`) } catch { return null }
}

function saveTeam(userId: string, teamId: string) {
  // Remember navigation only. Roles and member state never come from storage.
  try { window.sessionStorage.setItem(`gp-team-selection:${userId}`, teamId) } catch { /* Selection remains available in memory. */ }
}

const emptyState = () => ({
  userId: null,
  teams: [],
  currentTeamId: null,
  membership: null,
  status: 'idle' as LoadStatus,
  error: '',
  members: [],
  membersStatus: 'idle' as LoadStatus,
  membersError: '',
  memberMutation: null as MemberMutation | null,
})

export const useLiveTeamStore = create<LiveTeamState>((set, get) => {
  const identityMatches = (userId: string, version: number) => currentUserIs(userId) && get().userId === userId && identityVersion === version
  const memberScopeMatches = (userId: string, teamId: string, version: number) => currentUserIs(userId) && get().userId === userId && get().currentTeamId === teamId && get().membership?.role === 'admin' && membersVersion === version

  async function readMembership(userId: string, teamId: string, version: number, teams: TeamMembership[]) {
    const identity = await fetchTeamMembership(teamId)
    if (!identityMatches(userId, version)) return
    if (identity.user_id !== userId || identity.team_id !== teamId || identity.status !== 'active') {
      throw new ApiError('Membership is no longer active.', 403)
    }
    const membership: TeamMembership = { ...identity, team_name: teams.find((team) => team.team_id === teamId)?.team_name || get().membership?.team_name || '团队工作空间' }
    const sameTeam = get().membership?.team_id === teamId
    if (!sameTeam || membership.role !== 'admin') cancelMutation()
    set({
      teams: teams.map((team) => team.team_id === teamId ? membership : team),
      currentTeamId: teamId,
      membership,
      status: 'ready',
      error: '',
      ...(!sameTeam || membership.role !== 'admin' ? { members: [], membersStatus: 'idle' as LoadStatus, membersError: '', memberMutation: null } : {}),
    })
    saveTeam(userId, teamId)
    if (membership.role === 'admin') await get().refreshMembers()
  }

  async function mutateMember<T>(kind: MemberMutation, write: (teamId: string, signal: AbortSignal) => Promise<T>): Promise<MemberMutationResult<T>> {
    const { userId, membership, memberMutation, status } = get()
    if (!userId || !currentUserIs(userId) || !membership || membership.role !== 'admin' || status !== 'ready') throw new Error('请等待团队管理员身份确认后再操作。')
    if (memberMutation) throw new Error('正在更新团队成员，请勿重复提交。')
    const teamId = membership.team_id
    const version = ++mutationVersion
    const controller = new AbortController()
    mutationController = controller
    const matches = () => currentUserIs(userId) && get().userId === userId && get().currentTeamId === teamId && mutationVersion === version
    set({ memberMutation: kind })
    try {
      const value = await write(teamId, controller.signal)
      // A successful response remains successful even if our own membership
      // changes during the following authoritative refresh.
      if (matches()) await get().refreshTeams(userId, true)
      return { value, currentScope: matches() && get().membership?.role === 'admin' }
    } catch (error) {
      if (matches() && error instanceof ApiError && (error.status === 403 || error.code === 'TEAM_MEMBER_NOT_FOUND' || error.code === 'LAST_ADMIN_REQUIRED')) {
        // A disabled target is distinct from a disabled administrator.
        await get().refreshTeams(userId, true)
      }
      throw error
    } finally {
      if (mutationVersion === version) set({ memberMutation: null })
      if (mutationController === controller) mutationController = null
    }
  }

  return {
    ...emptyState(),
    reset: (userId = null) => {
      ++identityVersion
      ++membersVersion
      cancelMutation()
      identityInFlight = null
      set({ ...emptyState(), userId })
    },
    refreshTeams: (userId, force = false) => {
      if (!currentUserIs(userId)) return Promise.resolve()
      if (get().userId !== userId) get().reset(userId)
      if (identityInFlight && !force) return identityInFlight
      if (force) {
        identityInFlight = null
        ++membersVersion
        set({ members: [], membersStatus: 'idle', membersError: '' })
      }
      const version = ++identityVersion
      set({ status: 'loading', error: '' })
      const task = (async () => {
        try {
          const teams = (await fetchMyTeams()).filter((team) => team.user_id === userId && team.status === 'active')
          if (!identityMatches(userId, version)) return
          const wantedTeam = get().currentTeamId || savedTeam(userId)
          const selected = teams.find((team) => team.team_id === wantedTeam) || teams[0]
          if (!selected) {
            ++membersVersion
            cancelMutation()
            set({ teams: [], membership: null, currentTeamId: null, members: [], membersStatus: 'idle', membersError: '', memberMutation: null, status: 'ready' })
            return
          }
          if (get().currentTeamId !== selected.team_id) {
            ++membersVersion
            cancelMutation()
            set({ teams, currentTeamId: selected.team_id, membership: null, members: [], membersStatus: 'idle', membersError: '', memberMutation: null })
          }
          await readMembership(userId, selected.team_id, version, teams)
        } catch (error) {
          if (!identityMatches(userId, version)) return
          ++membersVersion
          cancelMutation()
          set({ membership: null, teams: [], currentTeamId: null, members: [], membersStatus: 'idle', membersError: '', memberMutation: null, status: 'error', error: teamErrorMessage(error) })
        }
      })()
      identityInFlight = task
      void task.finally(() => { if (identityInFlight === task) identityInFlight = null })
      return task
    },
    selectTeam: async (teamId) => {
      const { userId, teams } = get()
      if (!userId || !currentUserIs(userId) || !teams.some((team) => team.team_id === teamId)) return
      if (get().membership?.team_id === teamId && get().status === 'ready') return
      const version = ++identityVersion
      ++membersVersion
      cancelMutation()
      identityInFlight = null
      set({ currentTeamId: teamId, membership: null, members: [], membersStatus: 'idle', membersError: '', memberMutation: null, status: 'loading', error: '' })
      saveTeam(userId, teamId)
      try {
        await readMembership(userId, teamId, version, teams)
      } catch (error) {
        if (identityMatches(userId, version)) set({ membership: null, members: [], status: 'error', error: teamErrorMessage(error) })
      }
    },
    refreshMembers: async () => {
      const { userId, membership } = get()
      if (!userId || !currentUserIs(userId) || !membership || membership.role !== 'admin') return
      const teamId = membership.team_id
      const version = ++membersVersion
      set({ members: [], membersStatus: 'loading', membersError: '' })
      try {
        const members = await fetchTeamMembers(teamId)
        if (memberScopeMatches(userId, teamId, version)) set({ members, membersStatus: 'ready' })
      } catch (error) {
        if (!memberScopeMatches(userId, teamId, version)) return
        set({ members: [], membersStatus: 'error', membersError: teamErrorMessage(error) })
        // A rejected read cannot retain a previously visible member list.
        // Refresh identity separately; do not recursively retry this same read.
        if (error instanceof ApiError && error.status === 403) {
          const latestIdentity = await fetchTeamMembership(teamId).catch(() => null)
          if (!memberScopeMatches(userId, teamId, version)) return
          if (!latestIdentity || latestIdentity.user_id !== userId || latestIdentity.team_id !== teamId || latestIdentity.status !== 'active') {
            cancelMutation()
            set({ membership: null, members: [], memberMutation: null, status: 'error', error: '团队访问权限已失效，请刷新团队。' })
          } else if (latestIdentity.role !== 'admin') {
            const latest: TeamMembership = { ...latestIdentity, team_name: get().membership?.team_name || get().teams.find((team) => team.team_id === teamId)?.team_name || '团队工作空间' }
            cancelMutation()
            set({ membership: latest, members: [], memberMutation: null, membersStatus: 'idle', membersError: '', teams: get().teams.map((team) => team.team_id === teamId ? latest : team) })
          }
        }
      }
    },
    addMember: (email) => mutateMember('add', (teamId, signal) => addTeamMemberByEmail(teamId, email, signal)),
    changeMemberRole: (userId, role) => mutateMember('role', (teamId, signal) => updateTeamMemberRole(teamId, userId, role, signal)),
    removeMember: (userId) => mutateMember('remove', (teamId, signal) => removeTeamMember(teamId, userId, signal)),
  }
})

useAuthStore.subscribe((auth, previous) => {
  const userId = auth.isAuthenticated ? auth.user?.id ?? null : null
  const previousId = previous.isAuthenticated ? previous.user?.id ?? null : null
  if (userId !== previousId) useLiveTeamStore.getState().reset(userId)
})
