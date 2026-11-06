import { useEffect, useId, useRef, type ReactNode, type RefObject } from 'react'
import { X } from 'lucide-react'
import type { TeamMember } from '../../workbench/team'

export function MemberAvatar({ member }: { member: TeamMember }) {
  return <span className={`gp-team-avatar${member.role === 'admin' ? ' is-admin' : ''}`} aria-hidden="true">{member.name.slice(0, 1)}</span>
}

export function TeamDialog({ title, description, onClose, fallbackFocusRef, wide = false, busy = false, children }: {
  title: string
  description: string
  onClose: () => void
  fallbackFocusRef: RefObject<HTMLButtonElement | null>
  wide?: boolean
  busy?: boolean
  children: ReactNode
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const descriptionId = useId()

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const fallbackFocus = fallbackFocusRef.current
    const previousOverflow = document.body.style.overflow
    dialog.showModal()
    document.body.style.overflow = 'hidden'
    return () => {
      dialog.close()
      document.body.style.overflow = previousOverflow
      if (previousFocus?.isConnected) previousFocus.focus()
      else fallbackFocus?.focus()
    }
  }, [fallbackFocusRef])

  return <dialog ref={dialogRef} className={`gp-team-dialog${wide ? ' gp-team-dialog--wide' : ''}`} aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={busy} onCancel={(event) => { event.preventDefault(); if (!busy) onClose() }}>
    <div className="gp-team-dialog__header"><div><h2 id={titleId}>{title}</h2><p id={descriptionId}>{description}</p></div><button type="button" className="gp-team-icon-button" onClick={onClose} disabled={busy} aria-label="关闭对话框"><X size={18} aria-hidden="true" /></button></div>
    {children}
  </dialog>
}
