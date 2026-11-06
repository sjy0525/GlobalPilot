import { ArrowRight, History, RefreshCw } from 'lucide-react'
import type { TeamMemberOption } from '../../api/team'
import type { TicketAssignmentLogsResult } from '../../workbench/ticketAssignmentLogs'
import { formatTicketTime } from '../../workbench/ticketReading'
import { TicketPerson } from '../TicketList/components'

type Props = {
  result: TicketAssignmentLogsResult | null
  loading: boolean
  busy: boolean
  members: TeamMemberOption[]
  namesLoaded: boolean
  currentMemberId: string
  onRetry: () => void
}

export default function TicketAssignmentLogs({ result, loading, busy, members, namesLoaded, currentMemberId, onRetry }: Props) {
  const person = (userId: string) => <TicketPerson userId={userId} members={members} namesLoaded={namesLoaded} currentMemberId={currentMemberId} />
  return <section className="gp-ticket-assignment-logs" aria-label="改派记录" aria-busy={loading}>
    <header><div><h2><History size={17} aria-hidden="true" />改派记录</h2><p>记录处理人和审核人的历次指派变更。</p></div></header>
    {loading ? <p className="gp-ticket-assignment-logs__state" role="status">正在读取改派记录…</p> : !result ? <p className="gp-ticket-assignment-logs__state" role="status">工单确认完成后读取改派记录。</p> : result.status === 'error' ? <div className="gp-ticket-assignment-logs__error" role="alert"><p>{result.message}</p><button className="gp-ticket-read-secondary" type="button" disabled={busy} onClick={onRetry}><RefreshCw size={13} />重新加载改派记录</button></div> : result.rows.length === 0 ? <p className="gp-ticket-assignment-logs__state">暂无改派记录。</p> : <ol className="gp-ticket-assignment-log-list">{result.rows.map((log) => <li key={log.id}>
      <header><time dateTime={log.created_at}>{formatTicketTime(log.created_at)}</time><div className="gp-ticket-assignment-log-actor"><span>操作人</span>{person(log.actor_user_id)}</div></header>
      <dl><div><dt>处理人</dt><dd><span><small>改派前</small>{person(log.before_assignee_id)}</span><ArrowRight size={14} aria-hidden="true" /><span><small>改派后</small>{person(log.after_assignee_id)}</span>{log.before_assignee_id === log.after_assignee_id && <em>未变化</em>}</dd></div><div><dt>审核人</dt><dd><span><small>改派前</small>{person(log.before_reviewer_id)}</span><ArrowRight size={14} aria-hidden="true" /><span><small>改派后</small>{person(log.after_reviewer_id)}</span>{log.before_reviewer_id === log.after_reviewer_id && <em>未变化</em>}</dd></div></dl>
    </li>)}</ol>}
  </section>
}
