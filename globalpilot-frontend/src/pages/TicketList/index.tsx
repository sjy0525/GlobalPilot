import { useCallback, useMemo, useState } from 'react'
import { ArrowUpRight, FilePlus2, Inbox, RefreshCw, Search, X } from 'lucide-react'
import { fetchTeamTickets, type TicketRead } from '../../api/ticket'
import { useWorkbench } from '../../workbench/context'
import { filterTicketRows, formatTicketTime, ticketMatchesReadScope, ticketStatusLabels } from '../../workbench/ticketReading'
import { useTicketRead } from '../../workbench/useTicketRead'
import Link from '../../workbench/WorkbenchLink'
import { TicketPerson, TicketReadError, TicketStatus } from './components'
import '../../css/components/ticket-read.css'

export default function TicketList() {
  const { teamId, currentMemberId, canManageTeam } = useWorkbench()
  return <TicketListContent key={`${currentMemberId}:${teamId}:${canManageTeam ? 'admin' : 'employee'}`} />
}

function TicketListContent() {
  const { teamId, teamName, currentMemberId, canManageTeam, basePath } = useWorkbench()
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const read = useCallback((signal: AbortSignal) => fetchTeamTickets(teamId || '', signal), [teamId])
  const validate = useCallback((tickets: TicketRead[]) => Array.isArray(tickets) && tickets.every((ticket) => ticketMatchesReadScope(ticket, teamId || '')), [teamId])
  const result = useTicketRead({ scope: { userId: currentMemberId, teamId: teamId || '', role: canManageTeam ? 'admin' : 'employee' }, resource: 'list', view: 'list', read, validate })
  const loading = result.status === 'loading'
  const visibleTickets = useMemo(() => filterTicketRows(result.data || [], query, statusFilter, result.members), [result.data, query, statusFilter, result.members])
  const title = canManageTeam ? '团队全部工单' : '我的工单'
  const clearFilters = () => { setQuery(''); setStatusFilter('all') }

  return <main className="gp-ticket-read"><div className="gp-ticket-read__inner">
    <header className="gp-ticket-read-heading"><div><p className="gp-ticket-read-eyebrow">{teamName}</p><h1>{title}</h1><p>{canManageTeam ? '查看当前团队的全部工单。' : '查看由你创建、处理或审核的工单。'}</p></div><div className="gp-ticket-read-actions"><button className="gp-ticket-read-secondary" type="button" disabled={loading} onClick={() => { void result.refresh() }}><RefreshCw size={14} />{loading ? '读取中…' : '刷新工单'}</button><Link className="gp-ticket-read-primary" to={`${basePath}/tickets/new`}><FilePlus2 size={16} />新建工单</Link></div></header>
    <section className="gp-ticket-read-panel" aria-label={title} aria-busy={loading}>
      {loading ? <div className="gp-ticket-read-empty" role="status"><RefreshCw size={26} aria-hidden="true" /><p>正在读取工单…</p></div> : result.status === 'error' ? <TicketReadError title="暂时无法读取工单" message={result.error} onRetry={() => { void result.refresh() }} /> : <>
        <div className="gp-ticket-read-toolbar"><div className="gp-ticket-read-search"><Search size={15} aria-hidden="true" /><label className="gp-ticket-read-sr-only" htmlFor="gp-ticket-list-search">搜索标题、编号或参与人</label><input id="gp-ticket-list-search" type="search" placeholder="搜索标题、编号或参与人" value={query} onChange={(event) => setQuery(event.target.value)} />{query && <button type="button" aria-label="清空工单搜索" onClick={() => setQuery('')}><X size={14} /></button>}</div><label className="gp-ticket-read-filter"><span className="gp-ticket-read-sr-only">筛选工单状态</span><select aria-label="筛选工单状态" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">全部状态</option>{Object.entries(ticketStatusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><span className="gp-ticket-read-count" aria-live="polite">共 {visibleTickets.length} 项</span></div>
        {result.namesError && <p className="gp-ticket-read-warning" role="status">{result.namesError}</p>}
        {visibleTickets.length ? <div className="gp-ticket-read-table-wrap"><table className="gp-ticket-read-table"><thead><tr><th scope="col">工单</th><th scope="col">状态</th><th scope="col">创建人</th><th scope="col">处理人</th><th scope="col">审核人</th><th scope="col">更新时间</th><th scope="col"><span className="gp-ticket-read-sr-only">查看</span></th></tr></thead><tbody>{visibleTickets.map((ticket) => <tr key={ticket.id}><td className="gp-ticket-read-title"><Link to={`${basePath}/tickets/${encodeURIComponent(ticket.id)}`}>{ticket.title}</Link><small>{ticket.id}</small></td><td><TicketStatus status={ticket.status} /></td><td><TicketPerson userId={ticket.created_by} members={result.members} namesLoaded={result.namesLoaded} currentMemberId={currentMemberId} /></td><td><TicketPerson userId={ticket.assignee_id} members={result.members} namesLoaded={result.namesLoaded} currentMemberId={currentMemberId} /></td><td><TicketPerson userId={ticket.reviewer_id} members={result.members} namesLoaded={result.namesLoaded} currentMemberId={currentMemberId} /></td><td className="gp-ticket-read-time">{formatTicketTime(ticket.updated_at)}</td><td><Link className="gp-ticket-read-view" to={`${basePath}/tickets/${encodeURIComponent(ticket.id)}`} aria-label={`查看工单：${ticket.title}`}>查看<ArrowUpRight size={12} /></Link></td></tr>)}</tbody></table></div> : <div className="gp-ticket-read-empty"><Inbox size={30} aria-hidden="true" /><h2>{result.data?.length ? '没有匹配的工单' : canManageTeam ? '当前团队暂无工单' : '暂无与你相关的工单'}</h2><p>{result.data?.length ? '试试其他关键词或状态。' : '创建第一张工单，安排团队处理与审核。'}</p>{result.data?.length ? <button className="gp-ticket-read-secondary" type="button" onClick={clearFilters}>清除筛选</button> : <Link className="gp-ticket-read-primary" to={`${basePath}/tickets/new`}><FilePlus2 size={15} />新建工单</Link>}</div>}
      </>}
    </section>
  </div></main>
}
