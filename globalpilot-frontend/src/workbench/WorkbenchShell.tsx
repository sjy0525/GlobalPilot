import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Navigate, NavLink, Outlet, useLocation, useNavigate, useParams } from 'react-router'
import { ArrowLeft, ArrowUpRight, BookOpen, BriefcaseBusiness, ChevronDown, Compass, FilePlus2, Inbox, LayoutDashboard, LogOut, Menu, ShieldCheck, Users, X, ListFilter, ListTodo, ScrollText } from 'lucide-react'
import { useAuthStore } from '../store/authStore'
import { WorkbenchContext, useWorkbench } from './context'
import { getWorkbenchAccess } from './permissions'
import { PREVIEW_ACCOUNTS, PREVIEW_ADMIN_ID, PREVIEW_TEAM_ID, previewAccountId } from './team'
import { canApproveTicket, type Assignment, type MemberRemovalPlan } from './model'
import { usePreviewTeamStore } from './teamStore'
import { useLiveTeamStore } from './liveTeamStore'
import Link from './WorkbenchLink'
import '../css/components/workbench-shell.css'

function pageTitle(pathname: string, basePath: string) {
  const suffix = pathname.slice(basePath.length).replace(/\/+$/, '')
  if (suffix === '/tickets/new') return '提交工单'
  if (suffix.startsWith('/tickets/')) return '工单详情'
  if (suffix.startsWith('/tickets')) return '工单处理'
  if (suffix.startsWith('/reviews/')) return '审核详情'
  if (suffix.startsWith('/reviews')) return '审核台'
  if (suffix.startsWith('/team/tickets')) return '全部工单'
  if (suffix.startsWith('/team/unassigned')) return '待分配'
  if (suffix.startsWith('/team/logs')) return '操作日志'
  if (suffix.startsWith('/team')) return '团队成员'
  if (suffix.startsWith('/marketing/')) return '营销需求详情'
  if (suffix.startsWith('/marketing')) return '营销中心'
  if (suffix.startsWith('/assets')) return '品牌资料'
  return '工作台'
}

export default function WorkbenchShell({ preview = false }: { preview?: boolean }) {
  const user = useAuthStore((state) => state.user)
  const ready = useAuthStore((state) => state.ready)
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated)
  const location = useLocation()
  const navigate = useNavigate()
  const params = new URLSearchParams(location.search)
  const previewMemberId = previewAccountId(params.get('member'), params.get('view'))
  const previewState = usePreviewTeamStore()
  const liveState = useLiveTeamStore()
  const [mobileMenu, setMobileMenu] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)
  const [logoutError, setLogoutError] = useState('')
  const isPreview = preview && import.meta.env.DEV
  const basePath = isPreview ? '/preview/workbench' : '/workbench'
  const liveMembership = liveState.userId === user?.id ? liveState.membership : null
  const access = getWorkbenchAccess(isPreview ? { role: previewState.members.find((member) => member.id === previewMemberId)?.role, teamId: previewState.members.some((member) => member.id === previewMemberId) ? PREVIEW_TEAM_ID : null } : { role: liveMembership?.role, teamId: liveMembership?.team_id, status: liveMembership?.status })
  const currentMemberId = isPreview ? previewMemberId : user?.id || ''
  const displayName = isPreview ? PREVIEW_ACCOUNTS.find((account) => account.id === previewMemberId)?.name || '同事' : user?.name || '同事'
  const teamMembers = isPreview ? previewState.members : liveState.userId === user?.id ? liveState.members.map((member) => ({ id: member.user_id, name: member.nickname?.trim() || member.email.split('@')[0], email: member.email, department: '', role: member.role })) : []
  const availablePeople = isPreview ? PREVIEW_ACCOUNTS.filter((account) => !previewState.members.some((member) => member.id === account.id)) : []
  const teamId = isPreview ? PREVIEW_TEAM_ID : liveMembership?.team_id ?? null
  const teamName = isPreview ? '品牌与增长团队' : liveMembership?.team_name || '团队工作空间'
  const teamLoading = !isPreview && liveState.status === 'loading'
  const liveAccessRestricted = !isPreview && liveState.status === 'error' && !liveMembership
  const locationTitle = !isPreview && location.pathname.replace(/\/+$/, '') === `${basePath}/tickets` ? access.canManageTeam ? '团队全部工单' : '我的工单' : pageTitle(location.pathname, basePath)
  const title = access.canAccessWorkbench ? locationTitle : liveAccessRestricted ? '团队访问' : '加入团队'
  const identityLabel = liveAccessRestricted ? '团队访问受限' : !access.canAccessWorkbench ? '尚未加入团队' : access.canManageTeam ? '管理员' : '员工'

  useEffect(() => {
    if (!isPreview) void useAuthStore.getState().restoreSession()
  }, [isPreview])

  useEffect(() => {
    if (isPreview || !ready || !isAuthenticated || !user?.id) return
    const userId = user.id
    const refresh = () => {
      if (document.visibilityState !== 'hidden') void useLiveTeamStore.getState().refreshTeams(userId)
    }
    refresh()
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [isPreview, ready, isAuthenticated, user?.id])

  useEffect(() => {
    const originalTitle = document.title
    document.title = `${title} · GlobalPilot`
    return () => { document.title = originalTitle }
  }, [title])

  useEffect(() => {
    if (!mobileMenu) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileMenu(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [mobileMenu])

  if (!isPreview && !ready) return <div className="wb-loading" role="status">正在打开工作台…</div>
  if (!isPreview && !isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />
  }
  if (!isPreview && (!liveMembership && (liveState.userId !== user?.id || liveState.status === 'idle' || teamLoading))) return <div className="wb-loading" role="status">正在确认团队身份…</div>

  const navItems = access.canAccessWorkbench ? [
    { label: '工作台', icon: LayoutDashboard, to: basePath, end: true },
    ...(!isPreview ? [{ label: access.canManageTeam ? '团队全部工单' : '我的工单', icon: Inbox, to: `${basePath}/tickets`, end: true }] : []),
    ...(!isPreview ? [{ label: '新建工单', icon: FilePlus2, to: `${basePath}/tickets/new`, end: true }] : []),
    ...(isPreview ? [
    { label: '工单处理', icon: Inbox, to: `${basePath}/tickets`, end: false },
    ...(access.canReview ? [{ label: '审核台', icon: ShieldCheck, to: `${basePath}/reviews`, end: false }] : []),
    { label: '营销中心', icon: BriefcaseBusiness, to: `${basePath}/marketing`, end: false },
    { label: '品牌资料', icon: BookOpen, to: `${basePath}/assets`, end: false },
    ] : []),
  ] : []

  function changeAccount(memberId: string) {
    setMobileMenu(false)
    void navigate(`${basePath}?member=${memberId}`)
  }
  const navPath = (path: string) => isPreview ? `${path}?member=${previewMemberId}` : path
  const requirePreview = () => {
    if (!isPreview) throw new Error('业务数据尚未接入，请在前端预览中体验。')
  }
  const adminItems = [
    { label: '团队成员', icon: Users, to: `${basePath}/team`, end: true },
    ...(isPreview ? [
    { label: '全部工单', icon: ListFilter, to: `${basePath}/team/tickets`, end: false },
    { label: '待分配', icon: ListTodo, to: `${basePath}/team/unassigned`, end: false },
    { label: '操作日志', icon: ScrollText, to: `${basePath}/team/logs`, end: false },
    ] : []),
  ]

  return (
    <WorkbenchContext.Provider value={{ basePath, displayName, currentMemberId, ...access, isPreview, teamId, teamName, teamLoading, membersLoading: !isPreview && liveState.membersStatus === 'loading', membersError: isPreview ? '' : liveState.membersError, memberMutation: isPreview ? null : liveState.memberMutation, refreshMembers: () => isPreview || !user?.id ? Promise.resolve() : useLiveTeamStore.getState().refreshTeams(user.id), addMemberByEmail: async (email) => { const result = await useLiveTeamStore.getState().addMember(email); const member = result.value; return { ...result, value: { id: member.user_id, name: member.nickname?.trim() || member.email.split('@')[0], email: member.email, department: '', role: member.role } } }, updateTeamMemberRole: async (memberId, role) => { const result = await useLiveTeamStore.getState().changeMemberRole(memberId, role); const member = result.value; return { ...result, value: { id: member.user_id, name: member.nickname?.trim() || member.email.split('@')[0], email: member.email, department: '', role: member.role } } }, removeTeamMember: (memberId) => useLiveTeamStore.getState().removeMember(memberId), teamMembers, availablePeople, tickets: isPreview ? previewState.tickets : [], logs: isPreview ? previewState.logs : [], addMember: (memberId) => { requirePreview(); previewState.addMember(currentMemberId, memberId) }, removeMember: (memberId, plan: MemberRemovalPlan) => { requirePreview(); previewState.removeMember(currentMemberId, memberId, plan) }, changeMemberRole: (memberId, role, reason) => { requirePreview(); previewState.changeRole(currentMemberId, memberId, role, reason) }, assignTicket: (id, assignment: Assignment) => { requirePreview(); previewState.assignTicket(currentMemberId, id, assignment) } }}>
      <div className="wb-app" lang="zh-CN">
        {mobileMenu && <button className="wb-sidebar-scrim" aria-label="关闭导航" onClick={() => setMobileMenu(false)} />}
        <aside className={`wb-sidebar${mobileMenu ? ' is-open' : ''}`} aria-label="工作台导航">
          <Link className="wb-brand" to={basePath} onClick={() => setMobileMenu(false)}>
            <span className="wb-brand-icon"><Compass size={23} strokeWidth={1.8} /></span>
            <span>GlobalPilot<span className="wb-brand-caption">企业协作空间</span></span>
          </Link>
          <div className="wb-space"><span className="wb-space-avatar">{teamName.slice(0, 1)}</span>{!isPreview && liveState.teams.length > 1 ? <label className="wb-team-switcher"><span className="wb-sr-only">当前团队</span><select aria-label="当前团队" value={liveState.currentTeamId || ''} onChange={(event) => { setMobileMenu(false); void liveState.selectTeam(event.target.value) }}>{liveState.teams.map((team) => <option key={team.team_id} value={team.team_id}>{team.team_name}</option>)}</select><small>切换团队工作空间</small></label> : <span><strong>{teamName}</strong><small>团队工作空间</small></span>}<span className="wb-space-dot" /></div>
          <p className="wb-nav-label">工作空间</p>
          <nav className="wb-nav">
            {navItems.map(({ label, icon: Icon, to, end }) => (
              <NavLink key={label} to={navPath(to)} end={end} onClick={() => setMobileMenu(false)} className={({ isActive }) => `wb-nav-item${isActive ? ' is-active' : ''}`}>
                <Icon size={18} strokeWidth={1.7} /><span>{label}</span>
              </NavLink>
            ))}
          </nav>
          {access.canManageTeam && <div className="wb-admin-nav"><p className="wb-nav-label">团队管理</p>{adminItems.map(({ label, icon: Icon, to, end }) => <NavLink key={label} to={navPath(to)} end={end} onClick={() => setMobileMenu(false)} className={({ isActive }) => `wb-nav-item${isActive ? ' is-active' : ''}`}><Icon size={18} strokeWidth={1.7} /><span>{label}</span></NavLink>)}</div>}

          <div className="wb-sidebar-bottom">
            <div className="wb-space-note"><span className="wb-space-note-line" /><strong>让协作，有条不紊。</strong><p>从一个需求，到一次交付。</p></div>
            <Link className="wb-return" to="/"><ArrowLeft size={15} />返回品牌中心<ArrowUpRight size={14} /></Link>
            <div className="wb-user"><span className="wb-user-avatar">{displayName.slice(0, 1)}</span><span><strong>{displayName}</strong><small>{identityLabel}</small></span>{isPreview ? <span className="wb-user-online" title="当前使用中" /> : <button className="wb-logout" type="button" aria-label="退出登录" title="退出登录" disabled={loggingOut} onClick={() => { if (loggingOut) return; setLoggingOut(true); setLogoutError(''); void useAuthStore.getState().logout().then(() => navigate('/login', { replace: true })).catch(() => setLogoutError('退出失败，请重试。')).finally(() => setLoggingOut(false)) }}><LogOut size={16} /></button>}</div>
            {logoutError && <p className="wb-logout-error" role="alert">{logoutError}</p>}
          </div>
        </aside>
        <div className="wb-main">
          <header className="wb-topbar">
            <div className="wb-breadcrumb"><button className="wb-mobile-toggle" aria-label={mobileMenu ? '关闭导航' : '打开导航'} aria-expanded={mobileMenu} onClick={() => setMobileMenu(!mobileMenu)}>{mobileMenu ? <X size={20} /> : <Menu size={20} />}</button><span>工作空间</span><span className="wb-breadcrumb-divider">/</span><strong>{title}</strong></div>
            <div className="wb-topbar-right"><span className="wb-topbar-status"><span />协作空间</span><span className="wb-topbar-avatar">{displayName.slice(0, 1)}</span></div>
          </header>
          {isPreview && <div className="wb-preview-bar"><div><span className="wb-preview-dot" /><strong>前端预览</strong><span className="wb-preview-description">示例团队与工单 · 操作保存在当前浏览器会话</span></div><label className="wb-preview-picker"><span>账号</span><span className="wb-preview-select"><select aria-label="预览账号" value={previewMemberId} onChange={(event) => changeAccount(event.target.value)}>{PREVIEW_ACCOUNTS.map((account) => { const member = teamMembers.find((item) => item.id === account.id); return <option key={account.id} value={account.id}>{account.name} · {member ? member.role === 'admin' ? '管理员' : '员工' : '未加入团队'}</option> })}</select><ChevronDown size={13} /></span></label></div>}

          <div className="wb-content">{access.canAccessWorkbench ? <Outlet key={`${currentMemberId}:${teamId}`} /> : <main className="wb-membership-empty"><span className="wb-membership-icon"><Users size={29} strokeWidth={1.5} /></span><p className="wb-membership-eyebrow">团队工作空间</p><h1>{!isPreview && liveState.error ? '暂时无法打开团队' : '先加入团队，开启协作'}</h1><p>{!isPreview && liveState.error ? liveState.error : <>{displayName}，你的账号目前尚未加入有效团队。<br />请联系管理员使用你的注册邮箱添加成员。</>}</p>{isPreview ? <button className="wb-membership-action" onClick={() => changeAccount(previewState.members.find((member) => member.role === 'admin')?.id || PREVIEW_ADMIN_ID)}>切换管理员，添加成员<ArrowUpRight size={15} /></button> : <button className="wb-membership-action" onClick={() => { if (user?.id) void liveState.refreshTeams(user.id) }}>刷新团队<ArrowUpRight size={15} /></button>}</main>}</div>
        </div>
      </div>
    </WorkbenchContext.Provider>
  )
}

export function WorkbenchBlankPage() {
  const { isPreview, basePath } = useWorkbench()
  return isPreview ? <main className="wb-blank-page" aria-label="预留页面" /> : <main className="wb-membership-empty"><span className="wb-membership-icon"><BriefcaseBusiness size={28} /></span><h1>此功能正在接入</h1><p>当前可创建、查看工单。处理、审核等功能正在接入。</p><Link className="wb-membership-action" to={basePath}>返回工作台<ArrowUpRight size={15} /></Link></main>
}

export function WorkbenchPermissionGate({ permission, children }: { permission: 'review' | 'team'; children?: ReactNode }) {
  const { canReview, canManageTeam, basePath, isPreview } = useWorkbench()
  const location = useLocation()
  const allowed = permission === 'review' ? canReview : canManageTeam
  return allowed ? (!isPreview && location.pathname !== `${basePath}/team` ? <WorkbenchBlankPage /> : children || <WorkbenchBlankPage />) : <Navigate to={{ pathname: basePath, search: location.search }} replace />
}

/** A team admin has no bypass: details are reserved for the assigned reviewer. */
export function WorkbenchReviewGate() {
  const { id } = useParams()
  const { tickets, currentMemberId, teamMembers, basePath } = useWorkbench()
  const location = useLocation()
  const ticket = tickets.find((item) => item.id === id)
  return ticket && canApproveTicket(ticket, currentMemberId, teamMembers)
    ? <WorkbenchBlankPage />
    : <Navigate to={{ pathname: `${basePath}/reviews`, search: location.search }} replace />
}
