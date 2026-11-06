import { useMemo, useState } from 'react'
import { Navigate, useLocation } from 'react-router'
import { ArrowRight, ArrowUpRight, CheckCircle2, Clock3, Filter, Inbox, Search, SearchX, UserRoundCog, UsersRound, X } from 'lucide-react'
import { useWorkbench } from '../../workbench/context'
import { getUnassignedSides, isStaleTicket, type WorkbenchTicket } from '../../workbench/model'
import Link from '../../workbench/WorkbenchLink'
import AssignmentDialog from './AssignmentDialog'
import '../../css/components/workbench-admin.css'

const STATUS_LABELS: Record<WorkbenchTicket['status'], string> = { processing: '处理中', awaiting_review: '待审核', needs_info: '待补充', completed: '已完成' }
type IssueFilter = 'all' | 'unassigned' | 'stale'
type MissingFilter = 'all' | 'assignee' | 'reviewer'

export default function TeamTickets({ mode = 'all' }: { mode?: 'all' | 'unassigned' }) {
  const { tickets, teamMembers, currentMemberId, canManageTeam, isPreview, basePath, assignTicket } = useWorkbench()
  const location = useLocation()
  const [query, setQuery] = useState('')
  const [creator, setCreator] = useState('')
  const [assignee, setAssignee] = useState('')
  const [reviewer, setReviewer] = useState('')
  const [status, setStatus] = useState('')
  const [issue, setIssue] = useState<IssueFilter>('all')
  const [missing, setMissing] = useState<MissingFilter>('all')
  const [assigningId, setAssigningId] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const isUnassigned = mode === 'unassigned'
  const membersById = useMemo(() => new Map(teamMembers.map((member) => [member.id, member])), [teamMembers])
  const unassignedTickets = tickets.filter((ticket) => { const sides = getUnassignedSides(ticket, teamMembers); return sides.assignee || sides.reviewer })
  const staleTickets = tickets.filter((ticket) => isStaleTicket(ticket))
  const sourceTickets = isUnassigned ? unassignedTickets : tickets
  const missingAssigneeCount = unassignedTickets.filter((ticket) => getUnassignedSides(ticket, teamMembers).assignee).length
  const missingReviewerCount = unassignedTickets.filter((ticket) => getUnassignedSides(ticket, teamMembers).reviewer).length

  const people = useMemo(() => {
    const values = new Map(teamMembers.map((member) => [member.id, { id: member.id, name: member.name, active: true }]))
    for (const ticket of tickets) {
      for (const [id, name] of [[ticket.creatorId, ticket.creatorName], [ticket.assigneeId, ticket.assigneeName], [ticket.reviewerId, ticket.reviewerName]]) {
        if (id && !values.has(id)) values.set(id, { id, name: name || '历史成员', active: false })
      }
    }
    return [...values.values()]
  }, [tickets, teamMembers])

  const visibleTickets = sourceTickets.filter((ticket) => {
    const sides = getUnassignedSides(ticket, teamMembers)
    if (isUnassigned && missing !== 'all' && !sides[missing]) return false
    if (!isUnassigned && issue === 'unassigned' && !sides.assignee && !sides.reviewer) return false
    if (!isUnassigned && issue === 'stale' && !isStaleTicket(ticket)) return false
    if (creator && ticket.creatorId !== creator) return false
    if (assignee && (assignee === '__unassigned' ? !sides.assignee : ticket.assigneeId !== assignee)) return false
    if (reviewer && (reviewer === '__unassigned' ? !sides.reviewer : ticket.reviewerId !== reviewer)) return false
    if (status && ticket.status !== status) return false
    const needle = query.trim().toLocaleLowerCase()
    const peopleText = [ticket.creatorId, ticket.assigneeId, ticket.reviewerId].flatMap((id) => { const person = id ? membersById.get(id) : undefined; return person ? [person.name, person.department, person.email] : [] })
    return !needle || [ticket.id, ticket.title, ticket.category, ticket.creatorName, ticket.assigneeName, ticket.reviewerName, STATUS_LABELS[ticket.status], ...peopleText].filter(Boolean).join(' ').toLocaleLowerCase().includes(needle)
  })
  const assigningTicket = tickets.find((ticket) => ticket.id === assigningId)
  const hasIssueFilter = isUnassigned ? missing !== 'all' : issue !== 'all'
  const hasFilters = Boolean(query || creator || assignee || reviewer || status || hasIssueFilter)

  function clearFilters() { setQuery(''); setCreator(''); setAssignee(''); setReviewer(''); setStatus(''); setIssue('all'); setMissing('all') }

  function person(id: string | null, name: string | null) {
    const member = id ? membersById.get(id) : undefined
    return <span className="gp-admin-person"><span>{member?.name || name || '待分配'}{id === currentMemberId && <small className="gp-admin-self">我</small>}</span>{id && !member ? <small className="gp-admin-left">已离开团队</small> : member ? <small>{member.department || '未设置部门'}</small> : <small className="gp-admin-left">尚未指定</small>}</span>
  }

  function peopleOptions(includeUnassigned = false) {
    return <>{includeUnassigned && <option value="__unassigned">待分配（含已离队）</option>}{people.map((person) => <option key={person.id} value={person.id}>{person.name}{person.active ? '' : ' · 已离队'}</option>)}</>
  }

  if (!canManageTeam) return <Navigate to={{ pathname: basePath, search: location.search }} replace />

  return (
    <main className="gp-admin">
      <div className="gp-admin__inner">
        <header className="gp-admin-heading"><div><p className="gp-admin-eyebrow">团队管理</p><h1>{isUnassigned ? '待分配工单' : '全部工单'}</h1><p>{isUnassigned ? '补齐处理与审核责任，让每个需求有人跟进。' : '查看团队工单，及时发现分配缺口与积压事项。'}</p></div><Link className="gp-admin-secondary" to={`${basePath}/team/logs`}>操作日志<ArrowUpRight size={14} aria-hidden="true" /></Link></header>

        <section className="gp-admin-summary" aria-label="团队工单概览">
          <button className={`gp-admin-summary-card${(isUnassigned ? missing === 'all' : issue === 'all') ? ' is-selected' : ''}`} type="button" onClick={() => { setIssue('all'); setMissing('all') }}><span><Inbox size={16} aria-hidden="true" />{isUnassigned ? '待分配工单' : '团队工单'}</span><strong>{isUnassigned ? unassignedTickets.length : tickets.length}<small>项</small></strong><p>{isUnassigned ? '至少一项责任尚未落实' : '包含进行中与已完成工单'}</p></button>
          {isUnassigned ? <>
            <button className={`gp-admin-summary-card is-amber${missing === 'assignee' ? ' is-selected' : ''}`} type="button" onClick={() => setMissing(missing === 'assignee' ? 'all' : 'assignee')}><span><UserRoundCog size={16} aria-hidden="true" />缺处理人</span><strong>{missingAssigneeCount}<small>项</small></strong><p>尚未指派或原处理人已离队</p></button>
            <button className={`gp-admin-summary-card is-amber${missing === 'reviewer' ? ' is-selected' : ''}`} type="button" onClick={() => setMissing(missing === 'reviewer' ? 'all' : 'reviewer')}><span><UsersRound size={16} aria-hidden="true" />缺审核人</span><strong>{missingReviewerCount}<small>项</small></strong><p>尚未指派或原审核人已离队</p></button>
          </> : <>
            <button className={`gp-admin-summary-card is-amber${issue === 'unassigned' ? ' is-selected' : ''}`} type="button" onClick={() => setIssue(issue === 'unassigned' ? 'all' : 'unassigned')}><span><UserRoundCog size={16} aria-hidden="true" />待分配</span><strong>{unassignedTickets.length}<small>项</small></strong><p>处理人或审核人需要补齐</p></button>
            <button className={`gp-admin-summary-card is-amber${issue === 'stale' ? ' is-selected' : ''}`} type="button" onClick={() => setIssue(issue === 'stale' ? 'all' : 'stale')}><span><Clock3 size={16} aria-hidden="true" />长期未处理</span><strong>{staleTickets.length}<small>项</small></strong><p>未完成且连续 7 天没有更新</p></button>
          </>}
        </section>

        <section className="gp-admin-panel" aria-labelledby="gp-admin-tickets-title">
          <div className="gp-admin-panel-heading"><div><h2 id="gp-admin-tickets-title">{isUnassigned ? '分配队列' : '团队工单列表'}</h2><p>{isUnassigned ? '已完成工单不会进入待分配队列；两项均缺失时会同时计入。' : '管理分配与跟进进度，审核操作由被指派的审核人完成。'}</p></div><span className="gp-admin-total">共 {sourceTickets.length} 项</span></div>
          <div className="gp-admin-filters">
            <div className="gp-admin-search"><Search size={16} aria-hidden="true" /><label className="gp-admin-sr-only" htmlFor="gp-admin-ticket-search">搜索工单、编号或参与人</label><input id="gp-admin-ticket-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工单、编号或参与人" />{query && <button className="gp-admin-icon-button" type="button" aria-label="清空搜索" onClick={() => setQuery('')}><X size={14} aria-hidden="true" /></button>}</div>
            <div className="gp-admin-filter-row">
              <label className="gp-admin-filter"><span>创建人</span><select value={creator} onChange={(event) => setCreator(event.target.value)}><option value="">全部创建人</option>{peopleOptions()}</select></label>
              <label className="gp-admin-filter"><span>处理人</span><select value={assignee} onChange={(event) => setAssignee(event.target.value)}><option value="">全部处理人</option>{peopleOptions(true)}</select></label>
              <label className="gp-admin-filter"><span>审核人</span><select value={reviewer} onChange={(event) => setReviewer(event.target.value)}><option value="">全部审核人</option>{peopleOptions(true)}</select></label>
              <label className="gp-admin-filter"><span>状态</span><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              {hasFilters && <button className="gp-admin-text-button" type="button" onClick={clearFilters}>重置筛选</button>}
            </div>
          </div>
          {hasIssueFilter && <p className="gp-admin-filter-note"><Filter size={13} aria-hidden="true" />{isUnassigned ? missing === 'assignee' ? '当前只看缺处理人的工单' : '当前只看缺审核人的工单' : issue === 'stale' ? '当前只看连续 7 天未更新的未完成工单' : '当前只看待分配工单'}<button type="button" onClick={() => { setIssue('all'); setMissing('all') }}>清除</button></p>}
          {notice && <div className="gp-admin-notice" role="status"><CheckCircle2 size={15} aria-hidden="true" /><span>{notice}</span><button className="gp-admin-icon-button" type="button" aria-label="关闭成功提示" onClick={() => setNotice('')}><X size={14} aria-hidden="true" /></button></div>}
          <div className="gp-admin-table-wrap">
            <table className="gp-admin-table gp-admin-ticket-table"><thead><tr><th scope="col">工单</th><th scope="col">状态 / 跟进</th><th scope="col">创建人</th><th scope="col">处理人</th><th scope="col">审核人</th><th scope="col">截止 / 最近更新</th><th scope="col"><span className="gp-admin-sr-only">操作</span></th></tr></thead><tbody>
              {visibleTickets.map((ticket) => {
                const sides = getUnassignedSides(ticket, teamMembers)
                return <tr key={ticket.id}>
                  <td><Link className="gp-admin-ticket-title" to={`${basePath}/tickets/${ticket.id}`}>{ticket.title}</Link><p className="gp-admin-ticket-meta"><span>{ticket.id}</span><span>{ticket.category}</span></p></td>
                  <td><span className={`gp-admin-status is-${ticket.status}`}><span />{STATUS_LABELS[ticket.status]}</span><div className="gp-admin-flags">{sides.assignee && <span>缺处理人</span>}{sides.reviewer && <span>缺审核人</span>}{isStaleTicket(ticket) && <span className="is-stale">7 天未更新</span>}</div></td>
                  <td>{person(ticket.creatorId, ticket.creatorName)}</td><td>{person(ticket.assigneeId, ticket.assigneeName)}</td><td>{person(ticket.reviewerId, ticket.reviewerName)}</td>
                  <td><span className={ticket.dueToday && ticket.status !== 'completed' ? 'gp-admin-due' : ''}>{ticket.deadline || '未设置截止日'}</span><small className="gp-admin-updated">{new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(ticket.updatedAt))} 更新</small></td>
                  <td><div className="gp-admin-row-actions"><Link to={`${basePath}/tickets/${ticket.id}`}>查看<ArrowUpRight size={12} aria-hidden="true" /></Link>{ticket.status !== 'completed' && <button type="button" onClick={() => { setAssigningId(ticket.id); setNotice('') }} disabled={!isPreview} title={!isPreview ? '当前仅可查看，工单分配尚未接入业务数据' : undefined} aria-label={`调整${ticket.title}的处理人与审核人`}><UserRoundCog size={13} aria-hidden="true" />{sides.assignee || sides.reviewer ? '指派' : '改派'}</button>}</div></td>
                </tr>
              })}
            </tbody></table>
            {visibleTickets.length === 0 && <div className="gp-admin-empty">{isUnassigned && !hasFilters ? <CheckCircle2 size={30} strokeWidth={1.5} aria-hidden="true" /> : <SearchX size={30} strokeWidth={1.5} aria-hidden="true" />}<h3>{isUnassigned && !hasFilters ? '所有工单均已落实负责人' : '没有找到符合条件的工单'}</h3><p>{isUnassigned && !hasFilters ? '后续出现责任空缺的工单会显示在这里。' : '试试其他关键词，或调整筛选条件。'}</p>{hasFilters && <button className="gp-admin-text-button" type="button" onClick={clearFilters}>清除筛选</button>}</div>}
          </div>
          <footer className="gp-admin-panel-footer"><span aria-live="polite">当前显示 {visibleTickets.length} 项</span><Link to={`${basePath}/team/${isUnassigned ? 'tickets' : 'unassigned'}`}>{isUnassigned ? '查看全部工单' : '进入待分配队列'}<ArrowRight size={13} aria-hidden="true" /></Link></footer>
        </section>
      </div>
      {assigningTicket && isPreview && <AssignmentDialog key={assigningTicket.id} ticket={assigningTicket} members={teamMembers} onClose={() => setAssigningId(null)} onSave={(assignment) => { assignTicket(assigningTicket.id, assignment); setNotice(`已更新“${assigningTicket.title}”的分配，调整原因已记入操作日志。`) }} />}
    </main>
  )
}
