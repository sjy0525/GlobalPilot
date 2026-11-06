import { useCallback, useRef, useState } from 'react'
import { useParams } from 'react-router'
import { ArrowLeft, FilePlus2, RefreshCw } from 'lucide-react'
import { fetchTicketDetailWithLogs, type TicketDetailWithLogs } from '../../workbench/ticketAssignmentLogs'
import { useWorkbench } from '../../workbench/context'
import { formatTicketTime, ticketMatchesReadScope } from '../../workbench/ticketReading'
import { useTicketRead } from '../../workbench/useTicketRead'
import Link from '../../workbench/WorkbenchLink'
import { TicketPerson, TicketReadError, TicketStatus } from '../TicketList/components'
import TicketActions from './TicketActions'
import TicketAssignmentLogs from './TicketAssignmentLogs'
import '../../css/components/ticket-read.css'
import '../../css/components/ticket-actions.css'
import '../../css/components/ticket-assignment-logs.css'

export default function TicketDetail() {
  const { id = '' } = useParams()
  const { teamId, currentMemberId, canManageTeam } = useWorkbench()
  return <TicketDetailContent key={`${currentMemberId}:${teamId}:${canManageTeam ? 'admin' : 'employee'}:${id}`} ticketId={id} />
}

function TicketDetailContent({ ticketId }: { ticketId: string }) {
  const { teamId, teamName, currentMemberId, canManageTeam, basePath } = useWorkbench()
  const mutationBusy = useRef(false)
  const [busy, setBusy] = useState(false)
  const onBusy = useCallback((value: boolean) => { mutationBusy.current = value; setBusy(value) }, [])
  const canRefresh = useCallback(() => !mutationBusy.current, [])
  const read = useCallback((signal: AbortSignal) => fetchTicketDetailWithLogs(teamId || '', ticketId, signal), [teamId, ticketId])
  const validate = useCallback((data: TicketDetailWithLogs) => ticketMatchesReadScope(data.ticket, teamId || '', ticketId), [teamId, ticketId])
  const result = useTicketRead({ scope: { userId: currentMemberId, teamId: teamId || '', role: canManageTeam ? 'admin' : 'employee' }, resource: ticketId, view: 'detail', read, validate, canRefresh })
  const ticket = result.data?.ticket ?? null
  const loading = result.status === 'loading'

  return <main className="gp-ticket-read"><div className="gp-ticket-read__inner gp-ticket-read__inner--detail">
    <Link className="gp-ticket-read-back" to={`${basePath}/tickets`}><ArrowLeft size={14} />返回工单列表</Link>
    <header className="gp-ticket-read-heading"><div><p className="gp-ticket-read-eyebrow">{teamName}</p><h1>工单详情</h1></div><div className="gp-ticket-read-actions"><button className="gp-ticket-read-secondary" type="button" disabled={loading || busy} onClick={() => { void result.refresh() }}><RefreshCw size={14} />{loading ? '读取中…' : '刷新工单'}</button><Link className="gp-ticket-read-primary" to={`${basePath}/tickets/new`}><FilePlus2 size={16} />新建工单</Link></div></header>
    <section className="gp-ticket-read-panel gp-ticket-detail-panel" aria-label="工单详情" aria-busy={loading}>
      {loading ? <div className="gp-ticket-read-empty" role="status"><RefreshCw size={26} aria-hidden="true" /><p>正在读取工单详情…</p></div> : result.status === 'error' ? <TicketReadError title="无法打开工单" message={result.error} onRetry={() => { void result.refresh() }} /> : ticket && <>
        <header className="gp-ticket-detail-title"><div><h2>{ticket.title}</h2><code>{ticket.id}</code></div><TicketStatus status={ticket.status} /></header>
        {result.namesError && <p className="gp-ticket-read-warning" role="status">{result.namesError}</p>}
        <dl className="gp-ticket-detail-facts"><div><dt>所属团队</dt><dd>{teamName}</dd></div><div><dt>创建时间</dt><dd>{formatTicketTime(ticket.created_at)}</dd></div><div><dt>更新时间</dt><dd>{formatTicketTime(ticket.updated_at)}</dd></div></dl>
        <div className="gp-ticket-detail-people">{[{ label: '创建人', id: ticket.created_by }, { label: '处理人', id: ticket.assignee_id }, { label: '审核人', id: ticket.reviewer_id }].map((person) => <section key={person.label}><h3>{person.label}</h3><TicketPerson userId={person.id} members={result.members} namesLoaded={result.namesLoaded} currentMemberId={currentMemberId} /></section>)}</div>
        <section className="gp-ticket-detail-description"><h3>工单说明</h3><p>{ticket.description || '暂无补充说明。'}</p></section>
        <section className="gp-ticket-detail-description"><h3>处理结果</h3><p>{ticket.processing_result ?? '尚未提交处理结果。'}</p></section>
        <section className="gp-ticket-detail-description"><h3>最近审核意见</h3><p>{ticket.review_comment ?? '暂无审核意见。'}</p></section>
      </>}
    </section>
    <TicketActions key={result.generation} scope={{ userId: currentMemberId, teamId: teamId || '', role: canManageTeam ? 'admin' : 'employee', ticketId }} ticket={ticket} members={result.members} namesLoaded={result.namesLoaded} revision={result.revision} onBusy={onBusy} onRefresh={result.refresh} onClearData={result.clearData} />
    {(loading || ticket) && <TicketAssignmentLogs result={result.data?.assignmentLogs ?? null} loading={loading} busy={busy} members={result.members} namesLoaded={result.namesLoaded} currentMemberId={currentMemberId} onRetry={() => { void result.refresh() }} />}
  </div></main>
}
