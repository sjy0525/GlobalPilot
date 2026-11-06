import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowLeft, ArrowUpRight, CheckCircle2, FilePlus2, RefreshCw, Users } from 'lucide-react'
import { ApiError } from '../../api/http'
import { fetchTeamMembers, type TeamMemberOption } from '../../api/team'
import { createTeamTicket, ticketErrorMessage, type CreatedTicket, type TicketCreateBody } from '../../api/ticket'
import { useAuthStore } from '../../store/authStore'
import { useWorkbench } from '../../workbench/context'
import { useLiveTeamStore } from '../../workbench/liveTeamStore'
import { createTicketScopeGuard, receiptMatchesScope, reconcileTicketAssignments, type TicketScope } from '../../workbench/ticketCreation'
import Link from '../../workbench/WorkbenchLink'
import '../../css/components/ticket-create.css'

const emptyDraft = (): TicketCreateBody => ({ title: '', description: '', assignee_id: '', reviewer_id: '' })
const statusLabels: Record<CreatedTicket['status'], string> = { processing: '处理中', awaiting_review: '待审核', needs_info: '待补充', completed: '已完成' }
type Receipt = { ticket: CreatedTicket; teamName: string; creatorName: string; assignee: string; reviewer: string }

function currentScope(): TicketScope | null {
  const auth = useAuthStore.getState()
  const team = useLiveTeamStore.getState()
  const membership = team.membership
  return auth.isAuthenticated && auth.user?.id === team.userId && membership?.status === 'active' && (membership.role === 'admin' || membership.role === 'employee') && team.currentTeamId === membership.team_id
    ? { userId: auth.user!.id, teamId: membership.team_id }
    : null
}

function memberName(member: TeamMemberOption) {
  return `${member.nickname?.trim() || member.email.split('@')[0]} · ${member.email}`
}

export default function TicketCreate() {
  const { teamId, currentMemberId } = useWorkbench()
  return <TicketCreateForm key={`${currentMemberId}:${teamId}`} />
}

function TicketCreateForm() {
  const { basePath, teamId, teamName, currentMemberId, displayName, teamLoading } = useWorkbench()
  const scope = { userId: currentMemberId, teamId: teamId || '' }
  const [guard] = useState(() => createTicketScopeGuard(scope))
  const [draft, setDraft] = useState(emptyDraft)
  const draftRef = useRef(draft)
  const [members, setMembers] = useState<TeamMemberOption[]>([])
  const [membersStatus, setMembersStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [membersError, setMembersError] = useState('')
  const [assignmentNotice, setAssignmentNotice] = useState('')
  const [permissionBlocked, setPermissionBlocked] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const submitController = useRef<AbortController | null>(null)
  const membersVersion = useRef(0)
  const [submitError, setSubmitError] = useState('')
  const [uncertainResult, setUncertainResult] = useState(false)
  const [receipt, setReceipt] = useState<Receipt | null>(null)

  const loadMembers = useCallback(async () => {
    const snapshot = guard.capture()
    if (!guard.isCurrent(snapshot, currentScope()) || submittingRef.current || !teamId) return
    const version = ++membersVersion.current
    setMembersStatus('loading')
    setMembers([])
    setMembersError('')
    try {
      const available = await fetchTeamMembers(teamId)
      if (!guard.isCurrent(snapshot, currentScope()) || version !== membersVersion.current) return
      const reconciled = reconcileTicketAssignments(draftRef.current, available)
      draftRef.current = reconciled.draft
      setDraft(reconciled.draft)
      setAssignmentNotice(reconciled.notice)
      setMembers(available)
      setMembersStatus('ready')
      setPermissionBlocked(false)
    } catch (error) {
      if (!guard.isCurrent(snapshot, currentScope()) || version !== membersVersion.current) return
      setMembers([])
      setMembersStatus('error')
      setMembersError(ticketErrorMessage(error, true))
      if (error instanceof ApiError && (error.status === 403 || error.code === 'ACCOUNT_DISABLED')) {
        setPermissionBlocked(true)
        await useLiveTeamStore.getState().refreshTeams(currentMemberId, true)
      }
    }
  }, [guard, teamId, currentMemberId])

  useEffect(() => {
    guard.open()
    const invalidate = () => {
      if (!guard.isCurrent(guard.capture(), currentScope())) {
        guard.close()
        ++membersVersion.current
        submitController.current?.abort()
      }
    }
    const unsubscribeAuth = useAuthStore.subscribe(invalidate)
    const unsubscribeTeam = useLiveTeamStore.subscribe(invalidate)
    const refreshWhenVisible = () => { if (document.visibilityState !== 'hidden') void loadMembers() }
    void Promise.resolve().then(loadMembers)
    window.addEventListener('focus', refreshWhenVisible)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      guard.close()
      submitController.current?.abort()
      unsubscribeAuth()
      unsubscribeTeam()
      window.removeEventListener('focus', refreshWhenVisible)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [guard, loadMembers])

  function updateDraft(patch: Partial<TicketCreateBody>) {
    const next = { ...draftRef.current, ...patch }
    draftRef.current = next
    setDraft(next)
    setSubmitError('')
  }

  const validAssignee = members.some((member) => member.user_id === draft.assignee_id)
  const validReviewer = members.some((member) => member.user_id === draft.reviewer_id)
  const canSubmit = Boolean(draft.title.trim()) && validAssignee && validReviewer && membersStatus === 'ready' && !teamLoading && !permissionBlocked && !submitting && !uncertainResult

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const snapshot = guard.capture()
    if (submittingRef.current || !canSubmit || !teamId || !guard.isCurrent(snapshot, currentScope())) return
    submittingRef.current = true
    setSubmitting(true)
    setSubmitError('')
    const submittedDraft = { ...draftRef.current }
    const people = new Map(members.map((member) => [member.user_id, memberName(member)]))
    const submittedTeamName = teamName
    const submittedCreatorName = displayName
    const controller = new AbortController()
    submitController.current = controller
    let refreshAssignments = false
    try {
      const ticket = await createTeamTicket(teamId, submittedDraft, controller.signal)
      if (!guard.isCurrent(snapshot, currentScope())) return
      if (!receiptMatchesScope(ticket, snapshot)) {
        setSubmitError('返回的工单信息与当前团队不一致，请联系管理员核对。')
        return
      }
      setReceipt({ ticket, teamName: submittedTeamName, creatorName: submittedCreatorName, assignee: people.get(ticket.assignee_id) || ticket.assignee_id, reviewer: people.get(ticket.reviewer_id) || ticket.reviewer_id })
    } catch (error) {
      if (!guard.isCurrent(snapshot, currentScope())) return
      setSubmitError(ticketErrorMessage(error))
      if (error instanceof ApiError && error.status === 0) setUncertainResult(true)
      if (error instanceof ApiError && error.code === 'INVALID_TICKET_ASSIGNMENT') refreshAssignments = true
      if (error instanceof ApiError && (error.status === 403 || error.code === 'ACCOUNT_DISABLED')) {
        setPermissionBlocked(true)
        setMembers([])
        await useLiveTeamStore.getState().refreshTeams(currentMemberId, true)
      }
    } finally {
      submittingRef.current = false
      if (submitController.current === controller) submitController.current = null
      if (guard.isCurrent(snapshot, currentScope())) {
        setSubmitting(false)
        if (refreshAssignments) void loadMembers()
      }
    }
  }

  function createAnother() {
    const next = emptyDraft()
    draftRef.current = next
    setDraft(next)
    setSubmitError('')
    setAssignmentNotice('')
    setReceipt(null)
    void loadMembers()
  }

  return <main className="gp-ticket-create"><div className="gp-ticket-create__inner">
    <Link className="gp-ticket-back" to={basePath}><ArrowLeft size={14} />返回工作台</Link>
    {receipt ? <section className="gp-ticket-receipt" aria-labelledby="gp-ticket-success-title" role="status">
      <CheckCircle2 className="gp-ticket-success-icon" size={34} aria-hidden="true" /><p className="gp-ticket-eyebrow">{receipt.teamName}</p><h1 id="gp-ticket-success-title">工单已创建</h1><h2>{receipt.ticket.title}</h2>
      <dl><div><dt>工单编号</dt><dd><code>{receipt.ticket.id}</code></dd></div><div><dt>团队</dt><dd>{receipt.teamName}</dd></div><div><dt>当前状态</dt><dd><span className="gp-ticket-status">{statusLabels[receipt.ticket.status]}</span></dd></div><div><dt>创建人</dt><dd>{receipt.creatorName}</dd></div><div><dt>处理人</dt><dd>{receipt.assignee}</dd></div><div><dt>审核人</dt><dd>{receipt.reviewer}</dd></div>{receipt.ticket.description && <div><dt>工单说明</dt><dd className="gp-ticket-description">{receipt.ticket.description}</dd></div>}</dl>
      <div className="gp-ticket-actions"><Link className="gp-ticket-primary" to={`${basePath}/tickets/${encodeURIComponent(receipt.ticket.id)}`}>查看详情<ArrowUpRight size={15} /></Link><button className="gp-ticket-secondary" type="button" onClick={createAnother}><FilePlus2 size={16} />继续创建</button><Link className="gp-ticket-secondary" to={basePath}>返回工作台</Link></div>
    </section> : <><header className="gp-ticket-heading"><p className="gp-ticket-eyebrow">{teamName}</p><h1>新建工单</h1><p>填写需求，并指定团队内的处理人和审核人。</p></header>
      <form className="gp-ticket-form" onSubmit={submit} aria-busy={submitting}>
        <div className="gp-ticket-field"><label htmlFor="gp-ticket-title">工单标题<span>必填</span></label><input id="gp-ticket-title" name="title" value={draft.title} onChange={(event) => updateDraft({ title: event.target.value })} placeholder="例如：制作新品上线宣传材料" required autoFocus disabled={submitting} /></div>
        <div className="gp-ticket-field"><label htmlFor="gp-ticket-description">工单说明<span>选填</span></label><textarea id="gp-ticket-description" name="description" value={draft.description} onChange={(event) => updateDraft({ description: event.target.value })} rows={6} placeholder="说明具体需求、期望结果和补充信息" disabled={submitting} /></div>
        <div className="gp-ticket-people-heading"><h2><Users size={16} aria-hidden="true" />责任人</h2><button className="gp-ticket-refresh" type="button" disabled={membersStatus === 'loading' || submitting} onClick={() => { void loadMembers() }}><RefreshCw size={13} />{membersStatus === 'loading' ? '正在加载…' : '刷新成员'}</button></div>
        {membersError && <div className="gp-ticket-error" role="alert"><p>{membersError}</p><button type="button" onClick={() => { void loadMembers() }}>重新加载成员</button></div>}
        <div className="gp-ticket-people"><div className="gp-ticket-field"><label htmlFor="gp-ticket-assignee">处理人<span>必选</span></label><select id="gp-ticket-assignee" name="assignee_id" value={draft.assignee_id} onChange={(event) => updateDraft({ assignee_id: event.target.value })} required disabled={submitting || membersStatus !== 'ready' || permissionBlocked}><option value="">{membersStatus === 'loading' ? '正在读取有效成员…' : '请选择处理人'}</option>{members.map((member) => <option key={member.user_id} value={member.user_id}>{memberName(member)}{member.user_id === currentMemberId ? '（我）' : ''}</option>)}</select></div><div className="gp-ticket-field"><label htmlFor="gp-ticket-reviewer">审核人<span>必选</span></label><select id="gp-ticket-reviewer" name="reviewer_id" value={draft.reviewer_id} onChange={(event) => updateDraft({ reviewer_id: event.target.value })} required disabled={submitting || membersStatus !== 'ready' || permissionBlocked}><option value="">{membersStatus === 'loading' ? '正在读取有效成员…' : '请选择审核人'}</option>{members.map((member) => <option key={member.user_id} value={member.user_id}>{memberName(member)}{member.user_id === currentMemberId ? '（我）' : ''}</option>)}</select></div></div>
        <p className="gp-ticket-hint">处理人和审核人可以是同一人，也可以选择自己。</p>
        {membersStatus === 'ready' && members.length === 0 && <p className="gp-ticket-error" role="alert">当前没有可指派的有效成员，请联系团队管理员。</p>}
        {assignmentNotice && <p className="gp-ticket-warning" role="status">{assignmentNotice}</p>}
        {submitError && <p className="gp-ticket-error" role="alert">{submitError}</p>}
        <footer className="gp-ticket-form-footer"><span>提交至 {teamName}</span><button className="gp-ticket-primary" type="submit" disabled={!canSubmit}>{submitting ? '正在创建…' : uncertainResult ? '创建结果待确认' : '创建工单'}</button></footer>
      </form>
    </>}
  </div></main>
}
