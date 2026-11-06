import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { Info, UserRoundCog, X } from 'lucide-react'
import type { WorkbenchTicket } from '../../workbench/model'
import type { TeamMember } from '../../workbench/team'

export default function AssignmentDialog({ ticket, members, onSave, onClose }: {
  ticket: WorkbenchTicket
  members: TeamMember[]
  onSave: (assignment: { assigneeId: string | null; reviewerId: string | null; reason: string }) => void
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const reasonId = useId()
  const errorId = useId()
  const [assigneeId, setAssigneeId] = useState(ticket.assigneeId || '')
  const [reviewerId, setReviewerId] = useState(ticket.reviewerId || '')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const hasChanges = assigneeId !== (ticket.assigneeId || '') || reviewerId !== (ticket.reviewerId || '')
  const assigneeIsValid = !assigneeId || members.some((member) => member.id === assigneeId)
  const reviewerIsValid = !reviewerId || members.some((member) => member.id === reviewerId)

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    dialog.showModal()
    document.body.style.overflow = 'hidden'
    return () => {
      dialog.close()
      document.body.style.overflow = previousOverflow
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!assigneeIsValid || !reviewerIsValid) { setError('原负责人已离开团队，请重新选择成员或设为待分配。'); return }
    if (!hasChanges) { setError('请先调整处理人或审核人。'); return }
    if (!reason.trim()) { setError('请填写本次调整的原因。'); return }
    try {
      onSave({ assigneeId: assigneeId || null, reviewerId: reviewerId || null, reason: reason.trim() })
      onClose()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '调整未保存，请重试。')
    }
  }

  function personOptions(originalId: string | null, originalName: string | null) {
    return <>
      <option value="">待分配</option>
      {originalId && !members.some((member) => member.id === originalId) && <option value={originalId} disabled>{originalName || '原负责人'} · 已离开团队</option>}
      {members.map((member) => <option key={member.id} value={member.id}>{member.name} · {member.department || '未设置部门'}</option>)}
    </>
  }

  return (
    <dialog ref={dialogRef} className="gp-admin-dialog" aria-labelledby={titleId} onCancel={(event) => { event.preventDefault(); onClose() }} onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <form onSubmit={submit}>
        <header className="gp-admin-dialog__header"><div><span className="gp-admin-dialog__eyebrow"><UserRoundCog size={14} aria-hidden="true" />工单指派</span><h2 id={titleId}>调整处理人与审核人</h2><p>{ticket.title}<span>{ticket.id}</span></p></div><button className="gp-admin-icon-button" type="button" aria-label="关闭指派窗口" onClick={onClose}><X size={18} aria-hidden="true" /></button></header>
        <div className="gp-admin-dialog__body">
          <div className="gp-admin-dialog__notice"><Info size={15} aria-hidden="true" /><p>可指定当前团队成员，或设为待分配。每次调整都会记录操作者、前后变化与原因。</p></div>
          <div className="gp-admin-dialog__people">
            <label className="gp-admin-field"><span>处理人</span><select aria-label="调整处理人" value={assigneeId} onChange={(event) => { setAssigneeId(event.target.value); setError('') }} autoFocus>{personOptions(ticket.assigneeId, ticket.assigneeName)}</select></label>
            <label className="gp-admin-field"><span>审核人</span><select aria-label="调整审核人" value={reviewerId} onChange={(event) => { setReviewerId(event.target.value); setError('') }}>{personOptions(ticket.reviewerId, ticket.reviewerName)}</select></label>
          </div>
          {(!assigneeIsValid || !reviewerIsValid) && <p className="gp-admin-error" role="status">原{!assigneeIsValid && !reviewerIsValid ? '处理人与审核人均' : !assigneeIsValid ? '处理人' : '审核人'}已离开团队，请重新选择成员或设为待分配。</p>}
          <label className="gp-admin-field gp-admin-reason" htmlFor={reasonId}><span>调整原因 <small>必填</small></span><textarea id={reasonId} value={reason} onChange={(event) => { setReason(event.target.value); setError('') }} placeholder="例如：原处理人已离队，由同组成员继续跟进" rows={3} required maxLength={500} aria-describedby={error ? errorId : undefined} aria-invalid={Boolean(error)} /></label>
          {error && <p id={errorId} className="gp-admin-error" role="alert">{error}</p>}
        </div>
        <footer className="gp-admin-dialog__footer"><button className="gp-admin-secondary" type="button" onClick={onClose}>取消</button><button className="gp-admin-primary" type="submit" disabled={!hasChanges || !reason.trim() || !assigneeIsValid || !reviewerIsValid}>保存调整</button></footer>
      </form>
    </dialog>
  )
}
