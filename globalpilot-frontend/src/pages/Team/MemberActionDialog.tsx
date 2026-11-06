import { useRef, useState, type FormEvent, type RefObject } from 'react'
import { Info, LockKeyhole } from 'lucide-react'
import { teamErrorMessage } from '../../api/team'
import { useWorkbench } from '../../workbench/context'
import type { TeamMember } from '../../workbench/team'
import { MemberAvatar, TeamDialog } from './components'

type Props = {
  kind: 'role' | 'remove'
  member: TeamMember
  fallbackFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  onSuccess: (message: string) => void
}

export default function MemberActionDialog({ kind, member, fallbackFocusRef, onClose, onSuccess }: Props) {
  const { teamName, currentMemberId, teamMembers, canManageTeam, memberMutation, teamLoading, membersLoading, updateTeamMemberRole, removeTeamMember } = useWorkbench()
  const [role, setRole] = useState<TeamMember['role']>(member.role)
  const [error, setError] = useState('')
  const submittingRef = useRef(false)
  const busy = memberMutation !== null
  const loading = teamLoading || membersLoading
  const currentTarget = teamMembers.find((item) => item.id === member.id)
  const adminCount = teamMembers.filter((item) => item.role === 'admin').length
  const lastAdmin = currentTarget?.role === 'admin' && adminCount <= 1
  const blocked = lastAdmin && (kind === 'remove' || role === 'employee')
  const isSelf = member.id === currentMemberId
  const missing = !currentTarget && !loading && !busy

  function close() {
    if (!submittingRef.current && !busy) onClose()
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submittingRef.current || busy || loading || !canManageTeam) return
    if (!currentTarget) { setError('该成员已不在当前有效成员列表中，请刷新后重试。'); return }
    if (blocked) { setError('团队至少需要保留一名有效管理员，请先设置其他管理员。'); return }
    if (kind === 'role' && role === currentTarget.role) return
    submittingRef.current = true
    setError('')
    try {
      const result = kind === 'role' ? await updateTeamMemberRole(member.id, role) : await removeTeamMember(member.id)
      if (result.currentScope) onSuccess(kind === 'role' ? `${member.name} 已设为${role === 'admin' ? '管理员' : '员工'}。` : `${member.name} 已移出${teamName}。`)
    } catch (cause) {
      setError(teamErrorMessage(cause, kind))
    } finally {
      submittingRef.current = false
    }
  }

  return <TeamDialog title={kind === 'role' ? '修改成员角色' : '移除团队成员'} description={kind === 'role' ? '设置成员在当前团队中的角色。' : `确认将${member.name}移出${teamName}？`} fallbackFocusRef={fallbackFocusRef} onClose={close} busy={busy}>
    <form onSubmit={submit}>
      <div className="gp-team-dialog__body">
        <div className="gp-team-removal-person"><MemberAvatar member={currentTarget || member} /><div><strong>{member.name}</strong><small>{member.email}</small></div></div>
        {kind === 'role' ? <fieldset className="gp-team-role-choices" disabled={busy || loading}><legend>团队角色</legend>
          <label className={`gp-team-choice${role === 'admin' ? ' is-selected' : ''}`}><input type="radio" name="gp-live-team-role" value="admin" checked={role === 'admin'} onChange={() => { setRole('admin'); setError('') }} /><span><strong>管理员</strong><small>管理当前团队成员</small></span></label>
          <label className={`gp-team-choice${role === 'employee' ? ' is-selected' : ''}`}><input type="radio" name="gp-live-team-role" value="employee" checked={role === 'employee'} disabled={lastAdmin} onChange={() => { setRole('employee'); setError('') }} /><span><strong>员工</strong><small>使用当前团队工作空间</small></span></label>
        </fieldset> : <p className="gp-team-removal-description">仅退出当前团队，账号和历史记录继续保留。</p>}
        {isSelf && (kind === 'remove' || role === 'employee') && <p className="gp-team-dialog__hint"><Info size={13} aria-hidden="true" />{kind === 'remove' ? '这是当前账号，确认后你将退出该团队。' : '确认后你将退出团队管理页面。'}</p>}
        {lastAdmin && <p className="gp-team-dialog__hint"><LockKeyhole size={13} aria-hidden="true" />团队至少保留一名有效管理员，请先设置其他管理员。</p>}
        {(error || missing) && <p className="gp-team-error" role="alert">{error || '该成员已不在当前有效成员列表中，请关闭后刷新。'}</p>}
      </div>
      <div className="gp-team-dialog__footer gp-team-dialog__footer--remove"><button className="gp-team-secondary" type="button" disabled={busy} onClick={close}>取消</button><button className={kind === 'remove' ? 'gp-team-danger' : 'gp-team-primary'} type="submit" disabled={busy || loading || !canManageTeam || missing || blocked || (kind === 'role' && role === currentTarget?.role)}>{busy ? kind === 'role' ? '正在保存…' : '正在移除…' : kind === 'role' ? '确认修改' : '确认移除'}</button></div>
    </form>
  </TeamDialog>
}
