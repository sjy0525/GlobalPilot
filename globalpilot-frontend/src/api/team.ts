import { ApiError, apiJson } from './http'

export type TeamMembership = {
  team_id: string
  team_name: string
  user_id: string
  role: 'admin' | 'employee'
  status: 'active'
}

export type TeamMemberOption = {
  user_id: string
  email: string
  nickname: string | null
  role: 'admin' | 'employee'
}

export type TeamMembershipIdentity = Omit<TeamMembership, 'team_name'>

export function fetchMyTeams() {
  return apiJson<TeamMembership[]>('/api/team')
}

export function fetchTeamMembership(teamId: string) {
  return apiJson<TeamMembershipIdentity>(`/api/team/${encodeURIComponent(teamId)}/me`)
}

export function fetchTeamMembers(teamId: string, signal?: AbortSignal) {
  return apiJson<TeamMemberOption[]>(`/api/team/${encodeURIComponent(teamId)}/members/available`, { signal })
}

export function addTeamMemberByEmail(teamId: string, email: string, signal?: AbortSignal) {
  return apiJson<TeamMemberOption>(`/api/team/${encodeURIComponent(teamId)}/members`, {
    method: 'POST',
    body: JSON.stringify({ email: email.trim() }),
    signal,
  })
}

export function updateTeamMemberRole(teamId: string, userId: string, role: TeamMemberOption['role'], signal?: AbortSignal) {
  return apiJson<TeamMemberOption>(`/api/team/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}/role`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
    signal,
  })
}

export async function removeTeamMember(teamId: string, userId: string, signal?: AbortSignal) {
  await apiJson<void>(`/api/team/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    signal,
  })
}

export function teamErrorMessage(error: unknown, operation: boolean | 'add' | 'role' | 'remove' = false) {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : '请求失败，请稍后重试。'
  const adding = operation === true || operation === 'add'
  if (error.code === 'TEAM_MEMBER_EXISTS') return '该账号已是团队成员，无需重复添加。'
  if (error.code === 'LAST_ADMIN_REQUIRED') return '团队至少需要保留一名有效管理员，请先设置其他管理员。'
  if (error.code === 'TEAM_MEMBER_NOT_FOUND') return '该成员已不在当前团队中，请刷新成员列表。'
  if (error.code === 'MEMBER_HAS_OPEN_TICKETS') return '该成员仍有未完成的处理或审核工单，请先完成任务交接后再移除。'
  if (error.code === 'USER_NOT_FOUND') return '未找到这个注册账号，请先让对方完成注册。'
  if (error.code === 'ACCOUNT_DISABLED') return adding ? '该账号已停用，无法添加。' : operation === 'role' ? '该账号已停用，无法修改角色。' : '当前账号已停用，无法访问团队。'
  if (error.status === 401) return '登录已失效，请重新登录。'
  if (error.status === 403) return '你已没有当前团队的管理权限，请刷新后重试。'
  if (error.status === 404) return '团队不存在，或你已不在该团队中。'
  if (error.status === 422) return adding ? '请填写有效的注册邮箱。' : operation === 'role' ? '请选择管理员或员工。' : '成员信息有误，请刷新后重试。'
  if (error.status === 0) return '无法连接服务器，请检查网络后重试。'
  return '暂时无法完成请求，请稍后重试。'
}
