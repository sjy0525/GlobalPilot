import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowRightLeft, CheckCircle2, RefreshCw, Send, ShieldCheck } from 'lucide-react'
import { ApiError } from '../../api/http'
import { fetchTeamMembers, type TeamMemberOption } from '../../api/team'
import { reassignTeamTicket, reviewTeamTicket, submitTeamTicket, ticketActionErrorMessage, type TicketRead } from '../../api/ticket'
import { useAuthStore } from '../../store/authStore'
import { useLiveTeamStore } from '../../workbench/liveTeamStore'
import { actionSnapshotChanged, createTicketActionGuard, reconcileActionAssignments, ticketActionFailure, ticketActionPermissions, ticketActionReceiptMatches, type TicketActionKind, type TicketActionScope } from '../../workbench/ticketActions'

type Props = {
  scope: TicketActionScope
  ticket: TicketRead | null
  members: TeamMemberOption[]
  namesLoaded: boolean
  revision: number
  onBusy: (busy: boolean) => void
  onRefresh: () => Promise<boolean>
  onClearData: () => void
}
type Draft = { result: string; decision: 'approve' | 'return'; comment: string; assignee_id: string; reviewer_id: string; expected_updated_at: string }
const emptyDraft = (): Draft => ({ result: '', decision: 'approve', comment: '', assignee_id: '', reviewer_id: '', expected_updated_at: '' })
const actionLabels = { submit: '提交处理结果', review: '审核工单', assignment: '改派工单' }
const successLabels = { submit: '处理结果已提交，工单进入待审核。', review: '审核已完成。', assignment: '工单指派已保存。' }

function currentScope(ticketId: string): TicketActionScope | null {
  const auth = useAuthStore.getState()
  const team = useLiveTeamStore.getState()
  const member = team.membership
  return auth.isAuthenticated && auth.user?.id === team.userId && member?.status === 'active' && team.currentTeamId === member.team_id
    ? { userId: auth.user!.id, teamId: member.team_id, role: member.role, ticketId }
    : null
}

function personLabel(person: TeamMemberOption, userId: string) {
  return `${person.nickname?.trim() || person.email.split('@')[0]} · ${person.email}${person.user_id === userId ? '（我）' : ''}`
}

// This component stays mounted while a detail GET clears its data. Its draft
// belongs to the keyed account/team/role/ticket scope, not to a GET response.
export default function TicketActions({ scope, ticket, members, namesLoaded, revision, onBusy, onRefresh, onClearData }: Props) {
  const [guard] = useState(() => createTicketActionGuard(scope))
  const [mode, setMode] = useState<TicketActionKind | null>(null)
  const [draft, setDraft] = useState(emptyDraft)
  const draftRef = useRef(draft)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [conflict, setConflict] = useState(false)
  const [uncertainAt, setUncertainAt] = useState<number | null>(null)
  const [people, setPeople] = useState<TeamMemberOption[]>([])
  const [peopleStatus, setPeopleStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [peopleError, setPeopleError] = useState('')
  const mutationController = useRef<AbortController | null>(null)
  const peopleController = useRef<AbortController | null>(null)
  const peopleVersion = useRef(0)
  const permissions = ticketActionPermissions(ticket, scope.userId, scope.role)
  const changed = actionSnapshotChanged(draft.expected_updated_at, ticket)
  const needsConfirmation = changed || conflict
  const uncertain = uncertainAt !== null

  const setBusy = useCallback((value: boolean) => {
    setPending(value)
    onBusy(value)
  }, [onBusy])

  useEffect(() => {
    guard.open()
    const invalidate = () => {
      if (guard.isCurrent(guard.capture(), currentScope(scope.ticketId))) return
      // Abort synchronously when auth changes. The shared HTTP layer can retry
      // a 401, so merely ignoring a late receipt would not isolate the write.
      guard.close()
      mutationController.current?.abort()
      peopleController.current?.abort()
      ++peopleVersion.current
      draftRef.current = emptyDraft()
      setDraft(emptyDraft())
      setMode(null)
      setPeople([])
      setError('')
      setNotice('')
      setUncertainAt(null)
      setBusy(false)
    }
    const unsubscribeAuth = useAuthStore.subscribe(invalidate)
    const unsubscribeTeam = useLiveTeamStore.subscribe(invalidate)
    return () => {
      guard.close()
      mutationController.current?.abort()
      peopleController.current?.abort()
      unsubscribeAuth()
      unsubscribeTeam()
      onBusy(false)
    }
  }, [guard, scope.ticketId, onBusy, setBusy])

  function update(patch: Partial<Draft>) {
    const next = { ...draftRef.current, ...patch }
    draftRef.current = next
    setDraft(next)
    setError('')
  }

  async function loadPeople() {
    const snapshot = guard.capture()
    if (!guard.isCurrent(snapshot, currentScope(scope.ticketId)) || pending) return
    const version = ++peopleVersion.current
    peopleController.current?.abort()
    const controller = new AbortController()
    peopleController.current = controller
    setPeopleStatus('loading')
    setPeopleError('')
    try {
      const available = await fetchTeamMembers(scope.teamId, controller.signal)
      if (!guard.isCurrent(snapshot, currentScope(scope.ticketId)) || version !== peopleVersion.current) return
      const reconciled = reconcileActionAssignments(draftRef.current, available)
      update({ assignee_id: reconciled.assignee_id, reviewer_id: reconciled.reviewer_id })
      setPeople(available)
      setPeopleStatus('ready')
      if (reconciled.notice) setPeopleError(reconciled.notice)
    } catch (cause) {
      if (!guard.isCurrent(snapshot, currentScope(scope.ticketId)) || version !== peopleVersion.current) return
      setPeople([])
      setPeopleStatus('error')
      setPeopleError('无法读取有效成员，请重新加载后再选择。')
      if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
        onClearData()
        setMode(null)
        draftRef.current = emptyDraft()
        setDraft(emptyDraft())
        setPeopleError(ticketActionErrorMessage(cause))
        await useLiveTeamStore.getState().refreshTeams(scope.userId, true)
        if (guard.isCurrent(snapshot, currentScope(scope.ticketId))) await onRefresh()
      }
    }
  }

  function open(kind: TicketActionKind) {
    if (!ticket || pending || uncertain || !permissions[kind]) return
    const next: Draft = {
      result: ticket.processing_result || '', decision: 'approve', comment: '',
      assignee_id: ticket.assignee_id, reviewer_id: ticket.reviewer_id,
      // Preserve the original server timestamp, including microseconds.
      expected_updated_at: ticket.updated_at,
    }
    draftRef.current = next
    setDraft(next)
    setMode(kind)
    setError('')
    setNotice('')
    setConflict(false)
    if (kind === 'assignment') {
      setPeople(namesLoaded ? members : [])
      setPeopleStatus(namesLoaded ? 'ready' : 'idle')
      setPeopleError('')
      void loadPeople()
    }
  }

  function close() {
    if (pending || uncertain) return
    setMode(null)
    draftRef.current = emptyDraft()
    setDraft(emptyDraft())
    setError('')
    setConflict(false)
  }

  function confirmLatest() {
    if (!ticket || pending || uncertain) return
    update({ expected_updated_at: ticket.updated_at })
    setConflict(false)
    setNotice('已确认当前显示的工单内容，填写的草稿已保留。')
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const snapshot = guard.capture()
    if (!mode || !ticket || pending || uncertain || needsConfirmation || !permissions[mode] || !guard.isCurrent(snapshot, currentScope(scope.ticketId))) return
    const body = { ...draftRef.current }
    if (mode === 'submit' && !body.result.trim()) { setError('请填写处理结果。'); return }
    if (mode === 'review' && body.decision === 'return' && !body.comment.trim()) { setError('退回工单时必须填写审核意见。'); return }
    if (mode === 'assignment' && (peopleStatus !== 'ready' || !people.some((person) => person.user_id === body.assignee_id) || !people.some((person) => person.user_id === body.reviewer_id))) { setError('请选择团队内有效的处理人和审核人。'); return }
    if (!guard.begin()) return
    const kind = mode
    const controller = new AbortController()
    mutationController.current = controller
    peopleController.current?.abort()
    ++peopleVersion.current
    setBusy(true)
    setError('')
    setNotice('')
    let refresh: boolean
    let refreshCandidates = false
    let refreshIdentity = false
    let succeeded = false
    try {
      const receipt = kind === 'submit'
        ? await submitTeamTicket(scope.teamId, scope.ticketId, body.result, controller.signal)
        : kind === 'review'
          ? await reviewTeamTicket(scope.teamId, scope.ticketId, { decision: body.decision, comment: body.comment, expected_updated_at: body.expected_updated_at }, controller.signal)
          : await reassignTeamTicket(scope.teamId, scope.ticketId, { assignee_id: body.assignee_id, reviewer_id: body.reviewer_id, expected_updated_at: body.expected_updated_at }, controller.signal)
      if (!guard.isCurrent(snapshot, currentScope(scope.ticketId))) return
      if (!receipt || !ticketActionReceiptMatches(receipt, scope)) {
        setError('收到的操作回执与当前工单不一致，请先刷新核对，勿重复提交。')
        setUncertainAt(revision)
        return
      }
      succeeded = true
      setNotice(kind === 'review' ? body.decision === 'return' ? '工单已退回，等待处理人补充。' : '审核已通过，工单已完成。' : successLabels[kind])
      setMode(null)
      draftRef.current = emptyDraft()
      setDraft(emptyDraft())
      refresh = true
    } catch (cause) {
      if (!guard.isCurrent(snapshot, currentScope(scope.ticketId))) return
      const failure = ticketActionFailure(cause)
      setError(ticketActionErrorMessage(cause))
      setConflict(failure.conflict)
      if (failure.unknown) setUncertainAt(revision)
      refresh = failure.refreshTicket
      refreshCandidates = failure.refreshCandidates
      refreshIdentity = failure.refreshIdentity
      if (failure.clearSensitive) {
        onClearData()
        setMode(null)
        draftRef.current = emptyDraft()
        setDraft(emptyDraft())
        setPeople([])
      }
    } finally {
      if (guard.isCurrent(snapshot, currentScope(scope.ticketId))) {
        guard.finish()
        mutationController.current = null
        setBusy(false)
      }
    }
    if (!guard.isCurrent(snapshot, currentScope(scope.ticketId))) return
    if (refreshIdentity) {
      await useLiveTeamStore.getState().refreshTeams(scope.userId, true)
      refresh = true
    }
    if (!guard.isCurrent(snapshot, currentScope(scope.ticketId))) return
    if (refreshCandidates) await loadPeople()
    if (refresh) {
      const loaded = await onRefresh()
      if (succeeded && !loaded && guard.isCurrent(snapshot, currentScope(scope.ticketId))) setNotice(`${successLabels[kind]}详情暂时无法刷新，请稍后读取；无需重复提交。`)
    }
  }

  const disabled = pending || !ticket || uncertain
  const unchangedAssignment = mode === 'assignment' && ticket && draft.assignee_id === ticket.assignee_id && draft.reviewer_id === ticket.reviewer_id
  const hasActions = permissions.submit || permissions.review || permissions.assignment
  if (!mode && !notice && !error && !hasActions && !uncertain) return null

  return <section className="gp-ticket-actions-panel" aria-label="工单操作" aria-busy={pending}>
    <header><div><h2>工单操作</h2><p>按当前指派和工单状态操作。</p></div>{!mode && !uncertain && <div className="gp-ticket-action-buttons">
      {permissions.submit && <button type="button" className="gp-ticket-read-primary" onClick={() => open('submit')}><Send size={14} />提交处理结果</button>}
      {permissions.review && <button type="button" className="gp-ticket-read-primary" onClick={() => open('review')}><ShieldCheck size={14} />审核工单</button>}
      {permissions.assignment && <button type="button" className="gp-ticket-read-secondary" onClick={() => open('assignment')}><ArrowRightLeft size={14} />改派工单</button>}
    </div>}</header>
    {notice && <p className="gp-ticket-action-success" role="status"><CheckCircle2 size={16} aria-hidden="true" />{notice}</p>}
    {error && <p className="gp-ticket-action-error" role="alert">{error}</p>}
    {uncertain && <div className="gp-ticket-action-warning" role="status"><strong>操作结果待确认</strong><p>请求可能已被服务器执行。请先刷新并核对工单；确认前不要重复提交。</p><button type="button" className="gp-ticket-read-secondary" disabled={pending || !ticket || revision <= uncertainAt!} onClick={() => { setUncertainAt(null); setMode(null); setError(''); draftRef.current = emptyDraft(); setDraft(emptyDraft()); setNotice('已手动确认最新工单。本次操作不会自动重发。') }}>我已核对最新工单，关闭本次操作</button></div>}
    {mode && !uncertain && <form onSubmit={submit}>
      <h3>{actionLabels[mode]}</h3>
      {!ticket && <p className="gp-ticket-action-warning" role="status">正在确认最新工单。草稿已保留，确认完成前暂不能提交。</p>}
      {ticket && !permissions[mode] && <p className="gp-ticket-action-warning" role="status">当前指派或状态已变化，无法继续此操作。草稿已保留，可关闭后查看工单。</p>}
      {ticket && needsConfirmation && <div className="gp-ticket-action-warning" role="status"><p>工单内容或状态已更新。请查看上方最新详情，再确认是否继续。草稿已保留。</p><button type="button" className="gp-ticket-read-secondary" disabled={disabled || !permissions[mode]} onClick={confirmLatest}>确认已查看最新工单，继续编辑</button></div>}
      {mode === 'submit' && <div className="gp-ticket-action-field"><label htmlFor="gp-ticket-result">处理结果<span>必填</span></label><textarea id="gp-ticket-result" rows={7} value={draft.result} disabled={disabled} onChange={(event) => update({ result: event.target.value })} placeholder="说明处理结果、完成情况和需要审核的内容" required /></div>}
      {mode === 'review' && <><fieldset className="gp-ticket-review-options" disabled={disabled}><legend>审核决定</legend><label><input type="radio" name="gp-ticket-review-decision" value="approve" checked={draft.decision === 'approve'} onChange={() => update({ decision: 'approve' })} />通过，完成工单</label><label><input type="radio" name="gp-ticket-review-decision" value="return" checked={draft.decision === 'return'} onChange={() => update({ decision: 'return' })} />退回，补充处理</label></fieldset><div className="gp-ticket-action-field"><label htmlFor="gp-ticket-review-comment">审核意见<span>{draft.decision === 'return' ? '退回时必填' : '选填'}</span></label><textarea id="gp-ticket-review-comment" rows={5} value={draft.comment} disabled={disabled} onChange={(event) => update({ comment: event.target.value })} placeholder={draft.decision === 'return' ? '请说明需要补充或修改的内容' : '填写本次审核意见'} required={draft.decision === 'return'} /></div></>}
      {mode === 'assignment' && <><div className="gp-ticket-action-people-heading"><p>选择团队内有效成员，可指定同一人或自己。</p><button type="button" className="gp-ticket-read-secondary" disabled={pending || peopleStatus === 'loading'} onClick={() => { void loadPeople() }}><RefreshCw size={13} />{peopleStatus === 'loading' ? '读取中…' : '刷新可选成员'}</button></div>{peopleError && <p className="gp-ticket-action-warning" role="status">{peopleError}</p>}<div className="gp-ticket-action-people">{(['assignee_id', 'reviewer_id'] as const).map((field) => <div className="gp-ticket-action-field" key={field}><label htmlFor={`gp-ticket-action-${field}`}>{field === 'assignee_id' ? '处理人' : '审核人'}<span>必选</span></label><select id={`gp-ticket-action-${field}`} value={draft[field]} disabled={disabled || peopleStatus !== 'ready'} required onChange={(event) => update({ [field]: event.target.value })}><option value="">{peopleStatus === 'loading' ? '正在读取有效成员…' : '请选择有效成员'}</option>{people.map((person) => <option key={person.user_id} value={person.user_id}>{personLabel(person, scope.userId)}</option>)}</select></div>)}</div>{unchangedAssignment && <p className="gp-ticket-action-hint">指派未变化，无需保存。</p>}</>}
      <footer><span>提交前请确认上方工单信息。</span><div><button type="button" className="gp-ticket-read-secondary" disabled={pending} onClick={close}>取消</button><button type="submit" className="gp-ticket-read-primary" disabled={disabled || needsConfirmation || !permissions[mode] || Boolean(unchangedAssignment) || (mode === 'assignment' && peopleStatus !== 'ready') || (mode === 'submit' && !draft.result.trim()) || (mode === 'review' && draft.decision === 'return' && !draft.comment.trim())}>{pending ? '正在提交…' : mode === 'review' ? draft.decision === 'approve' ? '确认通过' : '确认退回' : mode === 'assignment' ? '保存指派' : '提交处理结果'}</button></div></footer>
    </form>}
  </section>
}
