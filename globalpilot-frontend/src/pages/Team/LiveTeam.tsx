import { useMemo, useRef, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import { CheckCircle2, Info, RefreshCw, Search, SearchX, ShieldCheck, UserRound, UserRoundCog, UserRoundMinus, UserRoundPlus, UsersRound, X } from 'lucide-react'
import { teamErrorMessage } from '../../api/team'
import { useWorkbench } from '../../workbench/context'
import { MemberAvatar, TeamDialog } from './components'
import MemberActionDialog from './MemberActionDialog'
import type { TeamMember } from '../../workbench/team'

type DialogState = { kind: 'add' } | { kind: 'role' | 'remove'; member: TeamMember } | null

export default function LiveTeam() {
  const { basePath, teamName, currentMemberId, canManageTeam, teamMembers, teamLoading, membersLoading, membersError, memberMutation, refreshMembers, addMemberByEmail } = useWorkbench()
  const [query, setQuery] = useState('')
  const [dialog, setDialog] = useState<DialogState>(null)
  const [email, setEmail] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const addButtonRef = useRef<HTMLButtonElement>(null)
  const submittingRef = useRef(false)
  const administratorCount = teamMembers.filter((member) => member.role === 'admin').length
  const loading = teamLoading || membersLoading
  const busy = memberMutation !== null
  const isLastAdmin = (member: TeamMember) => member.role === 'admin' && administratorCount <= 1
  const visibleMembers = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    return teamMembers.filter((member) => !needle || [member.name, member.email, member.role === 'admin' ? '管理员' : '员工'].join(' ').toLocaleLowerCase().includes(needle))
  }, [teamMembers, query])

  function closeDialog() {
    if (submittingRef.current || busy) return
    setDialog(null)
    setError('')
  }

  function openAction(kind: 'role' | 'remove', member: TeamMember) {
    if (busy || loading || !canManageTeam || isLastAdmin(member)) return
    setError('')
    setNotice('')
    setDialog({ kind, member })
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submittingRef.current || busy || teamLoading || !canManageTeam) return
    const normalized = email.trim()
    if (!normalized) { setError('请输入对方的注册邮箱。'); return }
    submittingRef.current = true
    setError('')
    try {
      const result = await addMemberByEmail(normalized)
      if (result.currentScope) {
        setDialog(null)
        setEmail('')
        setNotice(`${result.value.name} 已加入${teamName}，身份为员工。`)
      }
    } catch (cause) {
      setError(teamErrorMessage(cause, true))
    } finally {
      submittingRef.current = false
    }
  }

  if (!canManageTeam) return <Navigate to={basePath} replace />

  return <main className="gp-team">
    <div className="gp-team__inner">
      <header className="gp-team-heading"><div><p className="gp-team-eyebrow">{teamName}</p><h1>团队成员</h1><p className="gp-team-description">使用注册邮箱添加成员，一起推进工作。</p></div><button ref={addButtonRef} type="button" className="gp-team-primary" disabled={loading || busy} onClick={() => { setEmail(''); setError(''); setNotice(''); setDialog({ kind: 'add' }) }}><UserRoundPlus size={16} aria-hidden="true" />添加成员</button></header>
      <div className="gp-team-info"><Info size={15} aria-hidden="true" /><p>新成员默认员工。对方需要先完成账号注册。</p></div>
      <div className="gp-team-notice-slot" aria-live="polite" aria-atomic="true">{notice && <div className="gp-team-notice" role="status"><CheckCircle2 size={16} aria-hidden="true" /><span>{notice}</span><button className="gp-team-icon-button" type="button" aria-label="关闭成功提示" onClick={() => setNotice('')}><X size={15} aria-hidden="true" /></button></div>}</div>
      {!loading && !membersError && <section className="gp-team-stats" aria-label="团队概览"><div className="gp-team-stat"><span><UsersRound size={17} />团队成员</span><strong>{teamMembers.length}<small>人</small></strong></div><div className="gp-team-stat"><span><ShieldCheck size={17} />管理员</span><strong>{administratorCount}<small>人</small></strong></div><div className="gp-team-stat"><span><UserRound size={17} />员工</span><strong>{teamMembers.length - administratorCount}<small>人</small></strong></div></section>}
      <section className="gp-team-panel" aria-labelledby="gp-team-members-heading" aria-busy={loading}>
        <div className="gp-team-panel__header"><div><h2 id="gp-team-members-heading">有效成员</h2><p>显示当前团队内账号及成员状态均有效的成员</p></div><button className="gp-team-secondary" type="button" disabled={loading || busy} onClick={() => { void refreshMembers() }}><RefreshCw size={13} aria-hidden="true" />{loading ? '刷新中…' : '刷新成员'}</button></div>
        <div className="gp-team-toolbar"><div className="gp-team-search"><Search size={16} aria-hidden="true" /><label htmlFor="gp-live-team-search" className="gp-team-sr-only">搜索姓名或邮箱</label><input id="gp-live-team-search" type="search" placeholder="搜索姓名或邮箱" value={query} onChange={(event) => setQuery(event.target.value)} />{query && <button className="gp-team-icon-button" type="button" aria-label="清空成员搜索" onClick={() => setQuery('')}><X size={14} /></button>}</div><span className="gp-team-count" aria-live="polite">{loading ? '正在读取成员…' : membersError ? '读取失败' : `共 ${visibleMembers.length} 人`}</span></div>
        {membersError ? <div className="gp-team-load-error" role="alert"><p>{membersError}</p><button className="gp-team-secondary" type="button" onClick={() => { void refreshMembers() }}>重新加载</button></div> : loading ? <div className="gp-team-empty" role="status"><p>正在读取团队成员…</p></div> : <div className="gp-team-table-wrap"><table className="gp-team-table gp-team-table--live"><thead><tr><th scope="col" className="gp-team-col-person">成员</th><th scope="col" className="gp-team-col-email">邮箱</th><th scope="col" className="gp-team-col-role">身份</th><th scope="col" className="gp-team-col-action">操作</th></tr></thead><tbody>{visibleMembers.map((member) => <tr key={member.id}><td className="gp-team-col-person"><div className="gp-team-person"><MemberAvatar member={member} /><div><div className="gp-team-person__name"><strong>{member.name}</strong>{member.id === currentMemberId && <span className="gp-team-current">当前账号</span>}</div><span className="gp-team-mobile-email">{member.email}</span></div></div></td><td className="gp-team-col-email">{member.email}</td><td className="gp-team-col-role"><span className={`gp-team-role${member.role === 'admin' ? ' is-admin' : ''}`}>{member.role === 'admin' ? '管理员' : '员工'}</span></td><td className="gp-team-col-action"><div className="gp-team-actions"><button type="button" className="gp-team-role-action" disabled={busy || loading || isLastAdmin(member)} title={isLastAdmin(member) ? '团队至少保留一名有效管理员，请先设置其他管理员' : undefined} aria-label={`修改${member.name}的角色`} onClick={() => openAction('role', member)}><UserRoundCog size={13} aria-hidden="true" />修改角色</button><button type="button" className="gp-team-remove" disabled={busy || loading || isLastAdmin(member)} title={isLastAdmin(member) ? '团队至少保留一名有效管理员，请先设置其他管理员' : undefined} aria-label={`移除${member.name}`} onClick={() => openAction('remove', member)}><UserRoundMinus size={13} aria-hidden="true" />移除</button></div></td></tr>)}</tbody></table>{!visibleMembers.length && <div className="gp-team-empty"><SearchX size={27} aria-hidden="true" /><h3>{query.trim() ? '没有找到匹配的成员' : '暂无有效成员'}</h3><p>{query.trim() ? '试试其他姓名或邮箱。' : '添加已注册的账号加入团队。'}</p>{query.trim() && <button className="gp-team-text-button" type="button" onClick={() => setQuery('')}>清空搜索</button>}</div>}</div>}
        <div className="gp-team-panel__footer"><ShieldCheck size={12} aria-hidden="true" /><span>团队至少保留一名有效管理员；移除仅退出当前团队，保留账号和历史记录。</span></div>
      </section>
    </div>
    {dialog?.kind === 'add' && <TeamDialog title="添加团队成员" description="输入对方已注册的邮箱，加入后身份为员工。" fallbackFocusRef={addButtonRef} onClose={closeDialog} busy={busy}>
      <form onSubmit={submit}><div className="gp-team-dialog__body"><div className="gp-team-field gp-team-field--first"><label htmlFor="gp-team-member-email">注册邮箱</label><input id="gp-team-member-email" name="email" type="email" autoComplete="email" placeholder="例如：colleague@company.com" value={email} onChange={(event) => { setEmail(event.target.value); setError('') }} maxLength={255} required autoFocus disabled={busy} aria-invalid={Boolean(error)} aria-describedby={error ? 'gp-team-add-error' : undefined} /></div><p className="gp-team-dialog__hint"><Info size={13} aria-hidden="true" />不会自动发送邀请邮件；尚未注册的账号请先完成注册。</p>{error && <p id="gp-team-add-error" className="gp-team-error" role="alert">{error}</p>}</div><div className="gp-team-dialog__footer"><span className="gp-team-selection">团队身份：员工</span><div><button className="gp-team-secondary" type="button" onClick={closeDialog} disabled={busy}>取消</button><button className="gp-team-primary" type="submit" disabled={busy || teamLoading || !email.trim()}>{busy ? '正在添加…' : '确认添加'}</button></div></div></form>
    </TeamDialog>}
    {dialog && dialog.kind !== 'add' && <MemberActionDialog kind={dialog.kind} member={dialog.member} fallbackFocusRef={addButtonRef} onClose={closeDialog} onSuccess={(message) => { setDialog(null); setError(''); setNotice(message) }} />}
  </main>
}
