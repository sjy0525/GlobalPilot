import { AlertCircle, RefreshCw } from 'lucide-react'
import type { TeamMemberOption } from '../../api/team'
import type { TicketRead } from '../../api/ticket'
import { ticketPerson, ticketStatusLabels } from '../../workbench/ticketReading'

export function TicketPerson({ userId, members, namesLoaded, currentMemberId }: { userId: string; members: TeamMemberOption[]; namesLoaded: boolean; currentMemberId: string }) {
  const person = ticketPerson(userId, members, namesLoaded)
  return <span className={`gp-ticket-person${person.historical ? ' is-historical' : ''}`}><span>{person.name}{userId === currentMemberId && <small className="gp-ticket-self">我</small>}</span><small>{person.detail}</small></span>
}

export function TicketStatus({ status }: { status: TicketRead['status'] }) {
  return <span className={`gp-ticket-read-status is-${status}`}>{ticketStatusLabels[status]}</span>
}

export function TicketReadError({ title, message, onRetry }: { title: string; message: string; onRetry: () => void }) {
  return <div className="gp-ticket-read-empty gp-ticket-read-error" role="alert"><AlertCircle size={30} aria-hidden="true" /><h2>{title}</h2><p>{message}</p><button type="button" className="gp-ticket-read-secondary" onClick={onRetry}><RefreshCw size={14} />重新加载</button></div>
}
