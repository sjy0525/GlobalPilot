export type TeamMember = {
  id: string
  name: string
  email: string
  department: string
  role: 'admin' | 'employee'
}

export const PREVIEW_TEAM_ID = 'team-brand-growth'
export const PREVIEW_ADMIN_ID = 'member-lin'
export const PREVIEW_EMPLOYEE_ID = 'member-chen'

export const PREVIEW_ACCOUNTS: TeamMember[] = [
  { id: PREVIEW_ADMIN_ID, name: '林晓', email: 'lin.xiao@example.com', department: '市场部', role: 'admin' },
  { id: PREVIEW_EMPLOYEE_ID, name: '陈越', email: 'chen.yue@example.com', department: '市场部', role: 'employee' },
  { id: 'member-xu', name: '许清', email: 'xu.qing@example.com', department: '产品部', role: 'employee' },
  { id: 'member-zhou', name: '周宁', email: 'zhou.ning@example.com', department: '品牌部', role: 'employee' },
  { id: 'member-song', name: '宋然', email: 'song.ran@example.com', department: '销售部', role: 'employee' },
  { id: 'member-gu', name: '顾南', email: 'gu.nan@example.com', department: '运营部', role: 'employee' },
]

export const INITIAL_MEMBER_IDS = [PREVIEW_ADMIN_ID, PREVIEW_EMPLOYEE_ID, 'member-xu', 'member-zhou']

export function previewAccountId(value: string | null, view: string | null) {
  return PREVIEW_ACCOUNTS.find((account) => account.id === value)?.id || (view === 'admin' ? PREVIEW_ADMIN_ID : PREVIEW_EMPLOYEE_ID)
}
