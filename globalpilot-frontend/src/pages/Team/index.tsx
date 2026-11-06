import { useMemo, useRef, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import Link from '../../workbench/WorkbenchLink'
import { ArrowUpRight, Check, CheckCircle2, Info, LockKeyhole, Search, SearchX, ShieldCheck, UserRound, UserRoundCog, UserRoundMinus, UserRoundPlus, UsersRound, X } from 'lucide-react'
import { useWorkbench } from '../../workbench/context'
import type { MemberRemovalPlan, WorkbenchTicket } from '../../workbench/model'
import type { TeamMember } from '../../workbench/team'
import { MemberAvatar, TeamDialog } from './components'
import LiveTeam from './LiveTeam'
import '../../css/components/workbench-team.css'

type TeamDialogState = { kind: 'add' } | { kind: 'remove' | 'role'; member: TeamMember } | null

const TICKET_STATUS_LABELS: Record<string, string> = { processing: '处理中', awaiting_review: '待审核', needs_info: '待补充', completed: '已完成' }

function PendingTasks({ title, tickets }: { title: string; tickets: WorkbenchTicket[] }) {
  return <section className="gp-team-pending" aria-label={title}>
    <h3>{title}<span>{tickets.length} 项</span></h3>
    {tickets.length ? <ul>{tickets.map((ticket) => <li key={ticket.id}><div><strong>{ticket.title}</strong><small>{ticket.id}</small></div><span>{TICKET_STATUS_LABELS[ticket.status] || ticket.status}</span></li>)}</ul> : <p>暂无未完成任务</p>}
  </section>
}

export default function Team() {
  const { isPreview, teamId } = useWorkbench()
  return isPreview ? <PreviewTeam /> : <LiveTeam key={teamId} />
}

function PreviewTeam() {
  const { basePath, currentMemberId, canManageTeam, isPreview, teamMembers, availablePeople, tickets, addMember, changeMemberRole, removeMember } = useWorkbench()
  const [query, setQuery] = useState('')
  const [dialog, setDialog] = useState<TeamDialogState>(null)
  const [peopleQuery, setPeopleQuery] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const [nextRole, setNextRole] = useState<TeamMember['role']>('employee')
  const [roleReason, setRoleReason] = useState('')
  const [removalMode, setRemovalMode] = useState<MemberRemovalPlan['mode']>('queue')
  const [replacementAssignee, setReplacementAssignee] = useState('')
  const [replacementReviewer, setReplacementReviewer] = useState('')
  const [removalReason, setRemovalReason] = useState('成员离队')
  const [dialogError, setDialogError] = useState('')
  const [notice, setNotice] = useState('')
  const addButtonRef = useRef<HTMLButtonElement>(null)
  const canEdit = canManageTeam && isPreview
  const administratorCount = teamMembers.filter((member) => member.role === 'admin').length

  const visibleMembers = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    return teamMembers.filter((member) => !needle || [member.name, member.email, member.department, member.role === 'admin' ? '管理员' : '员工'].join(' ').toLocaleLowerCase().includes(needle))
  }, [teamMembers, query])

  const eligiblePeople = useMemo(() => {
    const memberIds = new Set(teamMembers.map((member) => member.id))
    return availablePeople.filter((person) => !memberIds.has(person.id))
  }, [availablePeople, teamMembers])

  const visiblePeople = useMemo(() => {
    const needle = peopleQuery.trim().toLocaleLowerCase()
    return eligiblePeople.filter((person) => !needle || [person.name, person.email, person.department].join(' ').toLocaleLowerCase().includes(needle))
  }, [eligiblePeople, peopleQuery])

  const selectedPerson = eligiblePeople.find((person) => person.id === selectedId)
  const removeTarget = dialog?.kind === 'remove' ? teamMembers.find((member) => member.id === dialog.member.id) : undefined
  const roleTarget = dialog?.kind === 'role' ? teamMembers.find((member) => member.id === dialog.member.id) : undefined
  const processingTasks = removeTarget ? tickets.filter((ticket) => ticket.status !== 'completed' && ticket.assigneeId === removeTarget.id) : []
  const reviewTasks = removeTarget ? tickets.filter((ticket) => ticket.status !== 'completed' && ticket.reviewerId === removeTarget.id) : []
  const hasOpenTasks = processingTasks.length > 0 || reviewTasks.length > 0
  const replacements = teamMembers.filter((member) => member.id !== removeTarget?.id)
  const lastAdminRemoval = removeTarget?.role === 'admin' && administratorCount <= 1
  const lastAdminDemotion = roleTarget?.role === 'admin' && administratorCount <= 1 && nextRole === 'employee'
  const missingReplacement = removalMode === 'reassign' && hasOpenTasks && (
    (processingTasks.length > 0 && !replacements.some((member) => member.id === replacementAssignee)) ||
    (reviewTasks.length > 0 && !replacements.some((member) => member.id === replacementReviewer))
  )

  function closeDialog() {
    setDialog(null)
    setDialogError('')
  }

  function openAddDialog() {
    if (!canEdit) return
    setPeopleQuery('')
    setSelectedId('')
    setDialogError('')
    setNotice('')
    setDialog({ kind: 'add' })
  }

  function openRemoveDialog(member: TeamMember) {
    if (!canEdit || (member.role === 'admin' && administratorCount <= 1)) return
    setRemovalMode('queue')
    setReplacementAssignee('')
    setReplacementReviewer('')
    setRemovalReason('成员离队')
    setDialogError('')
    setNotice('')
    setDialog({ kind: 'remove', member })
  }

  function openRoleDialog(member: TeamMember) {
    if (!canEdit || (member.role === 'admin' && administratorCount <= 1)) return
    setNextRole(member.role)
    setRoleReason('')
    setDialogError('')
    setNotice('')
    setDialog({ kind: 'role', member })
  }

  function submitAdd(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canEdit) { setDialogError('当前页面仅可查看，请在前端预览中管理成员。'); return }
    if (!selectedPerson) { setDialogError('请选择一个尚未加入团队的账号。'); return }
    try {
      addMember(selectedPerson.id)
      closeDialog()
      setNotice(`${selectedPerson.name} 已加入团队，可处理工单并被指派审核。`)
    } catch (error) {
      setDialogError(error instanceof Error ? error.message : '添加成员失败，请重试。')
    }
  }

  function submitRemove(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canEdit) { setDialogError('当前页面仅可查看，请在前端预览中管理成员。'); return }
    if (!removeTarget) { setDialogError('该成员已不在团队中，请关闭对话框后查看。'); return }
    if (lastAdminRemoval) { setDialogError('团队至少需要保留一位管理员，请先将其他成员设为管理员。'); return }
    if (missingReplacement) { setDialogError('请为未完成的处理和审核任务选择替代成员。'); return }
    if (hasOpenTasks && removalMode === 'reassign' && !removalReason.trim()) { setDialogError('请填写交接原因。'); return }
    try {
      const plan: MemberRemovalPlan = {
        mode: hasOpenTasks ? removalMode : 'queue',
        assigneeId: hasOpenTasks && removalMode === 'reassign' && processingTasks.length > 0 ? replacementAssignee : null,
        reviewerId: hasOpenTasks && removalMode === 'reassign' && reviewTasks.length > 0 ? replacementReviewer : null,
        reason: removalReason.trim() || '成员离队',
      }
      removeMember(removeTarget.id, plan)
      closeDialog()
      setNotice(`${removeTarget.name} 已从团队移除。${hasOpenTasks ? removalMode === 'queue' ? '相关未完成任务已移入待分配。' : '相关未完成任务已交接。' : ''}`)
    } catch (error) {
      setDialogError(error instanceof Error ? error.message : '移除成员失败，请重试。')
    }
  }

  function submitRole(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!canEdit) { setDialogError('当前页面仅可查看，请在前端预览中管理成员。'); return }
    if (!roleTarget) { setDialogError('该成员已不在团队中，请关闭对话框后查看。'); return }
    if (lastAdminDemotion) { setDialogError('团队至少需要保留一位管理员，请先将其他成员设为管理员。'); return }
    if (nextRole === roleTarget.role) { setDialogError('身份未发生变化。'); return }
    try {
      changeMemberRole(roleTarget.id, nextRole, roleReason.trim())
      closeDialog()
      setNotice(`${roleTarget.name} 已设为${nextRole === 'admin' ? '管理员' : '员工'}。`)
    } catch (error) {
      setDialogError(error instanceof Error ? error.message : '调整身份失败，请重试。')
    }
  }

  if (!canManageTeam) return <Navigate to={basePath} replace />

  return (
    <main className="gp-team">
      <div className="gp-team__inner">
        <header className="gp-team-heading">
          <div><p className="gp-team-eyebrow">团队协作</p><h1>团队成员</h1><p className="gp-team-description">管理团队成员，一起推进需求与交付。</p></div>
          <button ref={addButtonRef} type="button" className="gp-team-primary" onClick={openAddDialog} disabled={!canEdit} title={!canEdit ? '成员管理目前仅在前端预览中开放' : undefined}><UserRoundPlus size={16} aria-hidden="true" />添加成员</button>
        </header>

        <nav className="gp-team-page-links" aria-label="团队管理相关页面"><span aria-current="page">团队成员</span><Link to={`${basePath}/team/tickets`}>全部工单<ArrowUpRight size={12} aria-hidden="true" /></Link><Link to={`${basePath}/team/unassigned`}>待分配<ArrowUpRight size={12} aria-hidden="true" /></Link><Link to={`${basePath}/team/logs`}>操作日志<ArrowUpRight size={12} aria-hidden="true" /></Link></nav>

        <div className="gp-team-info"><Info size={15} aria-hidden="true" /><p>加入后即可处理工单并被指派审核。{!isPreview && <span>成员管理目前仅在前端预览中开放。</span>}</p></div>

        <div className="gp-team-notice-slot" aria-live="polite" aria-atomic="true">
          {notice && <div className="gp-team-notice" role="status"><CheckCircle2 size={16} aria-hidden="true" /><span>{notice}</span><button type="button" className="gp-team-icon-button" aria-label="关闭成功提示" onClick={() => setNotice('')}><X size={15} aria-hidden="true" /></button></div>}
        </div>

        <section className="gp-team-stats" aria-label="团队概览">
          <div className="gp-team-stat"><span><UsersRound size={17} aria-hidden="true" />团队成员</span><strong>{teamMembers.length}<small>人</small></strong></div>
          <div className="gp-team-stat"><span><ShieldCheck size={17} aria-hidden="true" />管理员</span><strong>{administratorCount}<small>人</small></strong></div>
          <div className="gp-team-stat"><span><UserRound size={17} aria-hidden="true" />员工</span><strong>{teamMembers.length - administratorCount}<small>人</small></strong></div>
        </section>

        <section className="gp-team-panel" aria-labelledby="gp-team-members-heading">
          <div className="gp-team-panel__header"><div><h2 id="gp-team-members-heading">团队成员</h2><p>查看成员信息与团队身份</p></div><span className="gp-team-count">{teamMembers.length} 位成员</span></div>
          <div className="gp-team-toolbar"><div className="gp-team-search"><Search size={16} aria-hidden="true" /><label htmlFor="gp-team-search" className="gp-team-sr-only">搜索姓名、邮箱或部门</label><input id="gp-team-search" type="search" placeholder="搜索姓名、邮箱或部门" value={query} onChange={(event) => setQuery(event.target.value)} />{query && <button type="button" className="gp-team-icon-button" aria-label="清空成员搜索" onClick={() => setQuery('')}><X size={14} aria-hidden="true" /></button>}</div><span className="gp-team-count" aria-live="polite">共 {visibleMembers.length} 人</span></div>
          <div className="gp-team-table-wrap">
            <table className="gp-team-table">
              <thead><tr><th scope="col" className="gp-team-col-person">成员</th><th scope="col" className="gp-team-col-email">邮箱</th><th scope="col" className="gp-team-col-department">所属部门</th><th scope="col" className="gp-team-col-role">身份</th><th scope="col" className="gp-team-col-action">操作</th></tr></thead>
              <tbody>{visibleMembers.map((member) => {
                const isCurrent = member.id === currentMemberId
                const isLastAdmin = member.role === 'admin' && administratorCount <= 1
                const disabledReason = !canEdit ? '成员管理目前仅在前端预览中开放' : isLastAdmin ? '团队至少保留一位管理员，请先将其他成员设为管理员' : undefined
                return <tr key={member.id}>
                  <td className="gp-team-col-person"><div className="gp-team-person"><MemberAvatar member={member} /><div><div className="gp-team-person__name"><strong>{member.name}</strong>{isCurrent && <span className="gp-team-current">当前账号</span>}</div><span className="gp-team-mobile-email">{member.email}</span><span className="gp-team-mobile-department">{member.department || '未设置部门'}</span></div></div></td>
                  <td className="gp-team-col-email">{member.email}</td><td className="gp-team-col-department">{member.department || '未设置'}</td>
                  <td className="gp-team-col-role"><span className={`gp-team-role${member.role === 'admin' ? ' is-admin' : ''}`}>{member.role === 'admin' ? '管理员' : '员工'}</span></td>
                  <td className="gp-team-col-action"><div className="gp-team-actions"><button type="button" className="gp-team-role-action" onClick={() => openRoleDialog(member)} disabled={!canEdit || isLastAdmin} aria-label={`调整${member.name}的身份`} title={disabledReason}><UserRoundCog size={13} aria-hidden="true" />调整身份</button><button type="button" className="gp-team-remove" onClick={() => openRemoveDialog(member)} disabled={!canEdit || isLastAdmin} aria-label={`移除${member.name}`} title={disabledReason}><UserRoundMinus size={13} aria-hidden="true" />移除</button></div></td>
                </tr>
              })}</tbody>
            </table>
            {visibleMembers.length === 0 && <div className="gp-team-empty"><SearchX size={28} strokeWidth={1.5} aria-hidden="true" /><h3>{query.trim() ? '没有找到匹配的成员' : '团队中还没有成员'}</h3><p>{query.trim() ? '试试其他姓名、邮箱或部门。' : '添加成员，一起处理团队工单。'}</p>{query.trim() && <button type="button" className="gp-team-text-button" onClick={() => setQuery('')}>清空搜索</button>}</div>}
          </div>
          <div className="gp-team-panel__footer"><LockKeyhole size={12} aria-hidden="true" /><span>团队至少保留一位管理员；成员离队前需交接未完成任务。</span></div>
        </section>
      </div>

      {dialog?.kind === 'add' && <TeamDialog title="添加团队成员" description="选择一个尚未加入团队的账号。" onClose={closeDialog} fallbackFocusRef={addButtonRef}>
        <form onSubmit={submitAdd}>
          <div className="gp-team-dialog__body">
            <div className="gp-team-search gp-team-search--dialog"><Search size={16} aria-hidden="true" /><label htmlFor="gp-team-people-search" className="gp-team-sr-only">搜索可添加账号</label><input id="gp-team-people-search" type="search" placeholder="搜索姓名、邮箱或部门" value={peopleQuery} onChange={(event) => setPeopleQuery(event.target.value)} autoFocus />{peopleQuery && <button type="button" className="gp-team-icon-button" aria-label="清空账号搜索" onClick={() => setPeopleQuery('')}><X size={14} aria-hidden="true" /></button>}</div>
            <div className="gp-team-picker-heading"><span>可添加的账号</span><span aria-live="polite">{visiblePeople.length} 人</span></div>
            <fieldset className="gp-team-picker"><legend className="gp-team-sr-only">选择要添加的成员</legend>{visiblePeople.map((person) => <label key={person.id} className={`gp-team-option${selectedId === person.id ? ' is-selected' : ''}`}>
              <input type="radio" name="gp-team-new-member" value={person.id} checked={selectedId === person.id} onChange={() => { setSelectedId(person.id); setDialogError('') }} />
              <MemberAvatar member={person} /><span className="gp-team-option__copy"><strong>{person.name}<span>{person.department || '未设置部门'}</span></strong><small>{person.email}</small></span><span className="gp-team-option__check" aria-hidden="true">{selectedId === person.id && <Check size={12} />}</span>
            </label>)}</fieldset>
            {visiblePeople.length === 0 && <div className="gp-team-empty gp-team-empty--dialog"><UsersRound size={26} strokeWidth={1.5} aria-hidden="true" /><h3>{eligiblePeople.length === 0 ? '暂无可添加的账号' : '没有找到匹配的账号'}</h3><p>{eligiblePeople.length === 0 ? '当前可用账号均已加入团队。' : '试试其他姓名、邮箱或部门。'}</p>{eligiblePeople.length > 0 && <button type="button" className="gp-team-text-button" onClick={() => setPeopleQuery('')}>清空搜索</button>}</div>}
            <p className="gp-team-dialog__hint"><Info size={13} aria-hidden="true" />加入后即可处理工单并被指派审核</p>
            {dialogError && <p className="gp-team-error" role="alert">{dialogError}</p>}
          </div>
          <div className="gp-team-dialog__footer"><span className="gp-team-selection">{selectedPerson ? `已选择 ${selectedPerson.name}` : '请选择一位成员'}</span><div><button type="button" className="gp-team-secondary" onClick={closeDialog}>取消</button><button type="submit" className="gp-team-primary" disabled={!selectedPerson || !canEdit}>确认添加</button></div></div>
        </form>
      </TeamDialog>}

      {dialog?.kind === 'role' && <TeamDialog title="调整成员身份" description="调整成员在当前团队中的身份。" onClose={closeDialog} fallbackFocusRef={addButtonRef}>
        <form onSubmit={submitRole}>
          <div className="gp-team-dialog__body">
            <div className="gp-team-removal-person"><MemberAvatar member={roleTarget || dialog.member} /><div><strong>{(roleTarget || dialog.member).name}</strong><small>{(roleTarget || dialog.member).email}</small></div></div>
            <fieldset className="gp-team-role-choices"><legend>团队身份</legend>
              <label className={`gp-team-choice${nextRole === 'admin' ? ' is-selected' : ''}`}><input type="radio" name="gp-team-role" value="admin" checked={nextRole === 'admin'} onChange={() => { setNextRole('admin'); setDialogError('') }} /><span><strong>管理员</strong><small>管理团队成员与团队工单</small></span></label>
              <label className={`gp-team-choice${nextRole === 'employee' ? ' is-selected' : ''}`}><input type="radio" name="gp-team-role" value="employee" checked={nextRole === 'employee'} disabled={roleTarget?.role === 'admin' && administratorCount <= 1} onChange={() => { setNextRole('employee'); setDialogError('') }} /><span><strong>员工</strong><small>处理工单，参与被指派的审核</small></span></label>
            </fieldset>
            <div className="gp-team-field"><label htmlFor="gp-team-role-reason">调整备注<span>选填</span></label><input id="gp-team-role-reason" value={roleReason} onChange={(event) => setRoleReason(event.target.value)} maxLength={500} placeholder="例如：团队负责人调整" /></div>
            {roleTarget?.id === currentMemberId && nextRole === 'employee' && <p className="gp-team-dialog__hint"><Info size={13} aria-hidden="true" />调整为员工后，你将退出团队管理页面。</p>}
            {roleTarget?.role === 'admin' && administratorCount <= 1 && <p className="gp-team-dialog__hint"><LockKeyhole size={13} aria-hidden="true" />团队至少保留一位管理员，请先将其他成员设为管理员。</p>}
            {!roleTarget && <p className="gp-team-error" role="alert">该成员已不在团队中，请关闭对话框后查看。</p>}
            {dialogError && <p className="gp-team-error" role="alert">{dialogError}</p>}
          </div>
          <div className="gp-team-dialog__footer gp-team-dialog__footer--remove"><button type="button" className="gp-team-secondary" onClick={closeDialog}>取消</button><button type="submit" className="gp-team-primary" disabled={!canEdit || !roleTarget || nextRole === roleTarget.role || lastAdminDemotion}>确认调整</button></div>
        </form>
      </TeamDialog>}

      {dialog?.kind === 'remove' && <TeamDialog title="移除团队成员" description={`查看${(removeTarget || dialog.member).name}的未完成任务，确认交接方式后移除。`} onClose={closeDialog} fallbackFocusRef={addButtonRef} wide>
        <form onSubmit={submitRemove}>
          <div className="gp-team-dialog__body">
            <div className="gp-team-removal-person"><MemberAvatar member={removeTarget || dialog.member} /><div><strong>{(removeTarget || dialog.member).name}</strong><small>{(removeTarget || dialog.member).email}</small></div></div>
            <div className="gp-team-removal-tasks"><PendingTasks title="未完成的处理任务" tickets={processingTasks} /><PendingTasks title="未完成的审核任务" tickets={reviewTasks} /></div>
            {hasOpenTasks ? <>
              <fieldset className="gp-team-removal-choices"><legend>未完成任务如何交接</legend>
                <label className={`gp-team-choice${removalMode === 'queue' ? ' is-selected' : ''}`}><input type="radio" name="gp-team-removal-mode" value="queue" checked={removalMode === 'queue'} onChange={() => { setRemovalMode('queue'); setDialogError('') }} /><span><strong>移入待分配</strong><small>清空该成员的处理、审核指派，稍后重新分配</small></span></label>
                <label className={`gp-team-choice${removalMode === 'reassign' ? ' is-selected' : ''}`}><input type="radio" name="gp-team-removal-mode" value="reassign" checked={removalMode === 'reassign'} onChange={() => { setRemovalMode('reassign'); setDialogError('') }} /><span><strong>交接给其他成员</strong><small>选择替代处理人和审核人，交接后移除</small></span></label>
              </fieldset>
              {removalMode === 'reassign' && <div className="gp-team-replacements">
                {processingTasks.length > 0 && <div className="gp-team-field"><label htmlFor="gp-team-replacement-assignee">替代处理人<span>承接 {processingTasks.length} 项</span></label><select id="gp-team-replacement-assignee" value={replacementAssignee} onChange={(event) => { setReplacementAssignee(event.target.value); setDialogError('') }} required><option value="">请选择团队成员</option>{replacements.map((member) => <option key={member.id} value={member.id}>{member.name} · {member.department || '未设置部门'}</option>)}</select></div>}
                {reviewTasks.length > 0 && <div className="gp-team-field"><label htmlFor="gp-team-replacement-reviewer">替代审核人<span>承接 {reviewTasks.length} 项</span></label><select id="gp-team-replacement-reviewer" value={replacementReviewer} onChange={(event) => { setReplacementReviewer(event.target.value); setDialogError('') }} required><option value="">请选择团队成员</option>{replacements.map((member) => <option key={member.id} value={member.id}>{member.name} · {member.department || '未设置部门'}</option>)}</select></div>}
                {replacements.length === 0 && <p className="gp-team-error" role="alert">暂无可交接的成员，请选择移入待分配。</p>}
              </div>}
            </> : <p className="gp-team-no-tasks"><CheckCircle2 size={14} aria-hidden="true" />没有需要交接的未完成任务，可以直接移除。</p>}
            <div className="gp-team-field"><label htmlFor="gp-team-removal-reason">{hasOpenTasks && removalMode === 'reassign' ? '交接原因' : '移除原因'}<span>{hasOpenTasks && removalMode === 'reassign' ? '必填' : '可修改'}</span></label><input id="gp-team-removal-reason" value={removalReason} onChange={(event) => { setRemovalReason(event.target.value); setDialogError('') }} maxLength={500} placeholder="说明本次成员变动的原因" required={hasOpenTasks && removalMode === 'reassign'} /></div>
            <p className="gp-team-removal-description">移除后将无法进入当前团队工作台，历史工单保留姓名。需要时可重新添加。</p>
            {removeTarget?.id === currentMemberId && <p className="gp-team-dialog__hint"><Info size={13} aria-hidden="true" />这是当前账号，确认后你将退出团队工作台。</p>}
            {lastAdminRemoval && <p className="gp-team-error" role="alert">团队至少需要保留一位管理员，当前成员不可移除。</p>}
            {!removeTarget && <p className="gp-team-error" role="alert">该成员已不在团队中，请关闭对话框后查看。</p>}
            {dialogError && <p id="gp-team-remove-error" className="gp-team-error" role="alert">{dialogError}</p>}
          </div>
          <div className="gp-team-dialog__footer gp-team-dialog__footer--remove"><button type="button" className="gp-team-secondary" onClick={closeDialog}>取消</button><button type="submit" className="gp-team-danger" disabled={!canEdit || !removeTarget || lastAdminRemoval || missingReplacement || (hasOpenTasks && removalMode === 'reassign' && !removalReason.trim())}>{hasOpenTasks ? removalMode === 'queue' ? '移入待分配并移除' : '交接并移除' : '确认移除'}</button></div>
        </form>
      </TeamDialog>}
    </main>
  )
}
