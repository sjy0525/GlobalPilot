import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import Link from '../../workbench/WorkbenchLink'
import {
  ArrowRight, ArrowUpRight, CalendarDays, ChevronRight, ClipboardCheck,
  Clock3, FileText, History, Inbox, Megaphone, Plus, Search, SearchX,
  UserRoundCog, UsersRound, X, type LucideIcon,
} from 'lucide-react'
import { useWorkbench } from '../../workbench/context'
import type { WorkbenchTicket } from '../../workbench/model'
import '../../css/components/workbench-dashboard.css'

type TicketStatus = WorkbenchTicket['status']
type TicketFilter = 'all' | TicketStatus
type RelationFilter = 'created' | 'assigned' | 'reviewing' | 'involved'
type TeamMember = ReturnType<typeof useWorkbench>['teamMembers'][number]
const STATUS_LABELS: Record<TicketStatus, string> = {
  processing: '处理中', awaiting_review: '待审核', needs_info: '待补充', completed: '已完成',
}
const FILTERS: { value: TicketFilter; label: string }[] = [
  { value: 'all', label: '全部' }, { value: 'processing', label: '处理中' },
  { value: 'awaiting_review', label: '待审核' }, { value: 'needs_info', label: '待补充' },
  { value: 'completed', label: '已完成' },
]
const RELATIONS: { value: RelationFilter; label: string }[] = [
  { value: 'created', label: '我创建的' }, { value: 'assigned', label: '待我处理' },
  { value: 'reviewing', label: '待我审核' }, { value: 'involved', label: '我参与的' },
]
const MARKETING_REQUESTS = [
  { id: 'MK-202610-001', title: 'Q4 品牌内容计划', type: '内容营销', status: '进行中', time: '2 小时前更新' },
  { id: 'MK-202610-002', title: '新品上市传播方案', type: '品牌活动', status: '待审核', time: '今天 09:30 更新' },
  { id: 'MK-202610-003', title: '海外市场调研', type: '市场研究', status: '待完善', time: '昨天更新' },
]

function matchesRelation(ticket: WorkbenchTicket, relation: RelationFilter, memberId: string) {
  if (relation === 'created') return ticket.creatorId === memberId
  if (relation === 'assigned') return ticket.assigneeId === memberId && (ticket.status === 'processing' || ticket.status === 'needs_info')
  if (relation === 'reviewing') return ticket.reviewerId === memberId && ticket.status === 'awaiting_review'
  return [ticket.creatorId, ticket.assigneeId, ticket.reviewerId].includes(memberId)
}

function OverviewCard({ label, value, hint, icon: Icon, onClick, review = false }: {
  label: string; value: number; hint: string; icon: LucideIcon; onClick: () => void; review?: boolean
}) {
  return (
    <button type="button" onClick={onClick} className={`gp-wbd-stat${review ? ' gp-wbd-stat--review' : ''}`}>
      <span className="gp-wbd-stat__top"><span>{label}</span><span className="gp-wbd-stat__icon"><Icon size={17} strokeWidth={1.8} aria-hidden="true" /></span></span>
      <strong className="gp-wbd-stat__value">{value}<span>项</span></strong>
      <span className="gp-wbd-stat__hint">{hint}<ChevronRight size={14} aria-hidden="true" /></span>
    </button>
  )
}

function PersonLabel({ memberId, name, member, isCurrent, compact = false }: {
  memberId: string | null; name: string | null; member: TeamMember | undefined; isCurrent: boolean; compact?: boolean
}) {
  if (memberId === null) return <span className="gp-wbd-person gp-wbd-person--unassigned">未分配</span>
  return (
    <span className={`gp-wbd-person${compact ? ' is-compact' : ''}`}>
      <span className="gp-wbd-person__name">{member?.name ?? name ?? '历史成员'}{isCurrent && <span className="gp-wbd-person__self">我</span>}</span>
      {!member ? <span className="gp-wbd-person__left">已离开团队</span> : !compact && <span className="gp-wbd-department">{member.department}</span>}
    </span>
  )
}

function AssignmentDialog({ ticket, teamMembers, onSave, onClose }: {
  ticket: WorkbenchTicket; teamMembers: TeamMember[]
  onSave: (assigneeId: string | null, reviewerId: string | null, reason: string) => void
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [assigneeId, setAssigneeId] = useState(ticket.assigneeId)
  const [reviewerId, setReviewerId] = useState(ticket.reviewerId)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const assigneeIsValid = assigneeId === null || teamMembers.some((member) => member.id === assigneeId)
  const reviewerIsValid = reviewerId === null || teamMembers.some((member) => member.id === reviewerId)
  const changed = assigneeId !== ticket.assigneeId || reviewerId !== ticket.reviewerId
  const canSave = assigneeIsValid && reviewerIsValid && changed && reason.trim().length > 0

  useEffect(() => {
    const dialog = dialogRef.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!canSave) return
    try {
      onSave(assigneeId, reviewerId, reason.trim())
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法保存指派，请重试。')
    }
  }

  return (
    <dialog ref={dialogRef} className="gp-wbd-dialog" aria-labelledby="gp-wbd-assignment-title" aria-describedby="gp-wbd-assignment-description" onCancel={(event) => { event.preventDefault(); onClose() }} onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <form onSubmit={submit}>
        <header className="gp-wbd-dialog__header"><div><h2 id="gp-wbd-assignment-title">指派工单</h2><p id="gp-wbd-assignment-description">{ticket.title}</p></div><button type="button" className="gp-wbd-dialog__close" aria-label="关闭指派窗口" onClick={onClose}><X size={18} aria-hidden="true" /></button></header>
        <div className="gp-wbd-dialog__body">
          <p className="gp-wbd-dialog__intro">选择当前团队成员，或暂不指派，交由团队后续分配。</p>
          <label className="gp-wbd-assignment-field"><span>处理人</span><select value={assigneeId ?? ''} onChange={(event) => { setAssigneeId(event.target.value || null); setError(null) }} autoFocus><option value="">暂不指派（待分配）</option>{!assigneeIsValid && <option value={assigneeId ?? ''} disabled>{ticket.assigneeName ?? '原处理人'}（已离开团队）</option>}{teamMembers.map((member) => <option key={member.id} value={member.id}>{member.name} · {member.department}</option>)}</select></label>
          <label className="gp-wbd-assignment-field"><span>审核人</span><select value={reviewerId ?? ''} onChange={(event) => { setReviewerId(event.target.value || null); setError(null) }}><option value="">暂不指派（待分配）</option>{!reviewerIsValid && <option value={reviewerId ?? ''} disabled>{ticket.reviewerName ?? '原审核人'}（已离开团队）</option>}{teamMembers.map((member) => <option key={member.id} value={member.id}>{member.name} · {member.department}</option>)}</select></label>
          {(!assigneeIsValid || !reviewerIsValid) && <p className="gp-wbd-assignment-warning" role="status">责任人已离开团队，请选择当前成员或暂不指派。</p>}
          <label className="gp-wbd-assignment-field"><span>变更原因 <span className="gp-wbd-required">必填</span></span><textarea value={reason} onChange={(event) => { setReason(event.target.value); setError(null) }} placeholder="说明本次指派或调整责任人的原因" maxLength={500} rows={3} required aria-invalid={error ? true : undefined} aria-describedby={error ? 'gp-wbd-assignment-error' : undefined} /></label>
          {error && <p id="gp-wbd-assignment-error" className="gp-wbd-assignment-error" role="alert">{error}</p>}
        </div>
        <footer className="gp-wbd-dialog__footer"><button className="gp-wbd-secondary" type="button" onClick={onClose}>取消</button><button className="gp-wbd-primary" type="submit" disabled={!canSave}>保存指派</button></footer>
      </form>
    </dialog>
  )
}

export default function Workbench() {
  const { isPreview, basePath, displayName, teamName, canManageTeam } = useWorkbench()
  if (isPreview) return <PreviewWorkbench />
  return <main className="gp-wbd"><div className="gp-wbd__inner"><header className="gp-wbd-heading"><div className="gp-wbd-heading__copy"><p className="gp-wbd-heading__eyebrow">{teamName}</p><h1>{displayName}，你好</h1><p className="gp-wbd-heading__description">当前团队身份：{canManageTeam ? '管理员' : '员工'}。</p></div><div className="gp-wbd-heading__actions"><Link className="gp-wbd-secondary" to={`${basePath}/tickets`}><Inbox size={17} />查看工单</Link><Link className="gp-wbd-primary" to={`${basePath}/tickets/new`}><Plus size={17} />新建工单</Link>{canManageTeam && <Link className="gp-wbd-secondary" to={`${basePath}/team`}><UsersRound size={17} />团队成员</Link>}</div></header><section className="gp-wbd-panel"><div className="gp-wbd-panel__header"><div><h2>团队工作空间</h2><p>{canManageTeam ? '可在团队成员页面查看有效成员，并添加已注册的同事。' : '你已加入团队，成员管理由团队管理员负责。'}</p></div></div><div className="gp-wbd-empty"><Inbox size={28} aria-hidden="true" /><h3>创建一张工单，安排团队协作</h3><p>提交需求、查看进展，并按创建、处理或审核关系跟进工单。</p></div></section></div></main>
}

function PreviewWorkbench() {
  const { basePath, displayName, currentMemberId, teamMembers, tickets, assignTicket, canManageTeam } = useWorkbench()
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<TicketFilter>('all')
  const [relation, setRelation] = useState<RelationFilter>('involved')
  const [assigningTicketId, setAssigningTicketId] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState('')
  const ticketsPath = `${basePath}/tickets`
  const today = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())
  const membersById = useMemo(() => new Map(teamMembers.map((member) => [member.id, member])), [teamMembers])
  const relationCount = (value: RelationFilter) => tickets.filter((ticket) => matchesRelation(ticket, value, currentMemberId)).length
  const searchedTickets = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    return tickets.filter((ticket) => {
      if (!matchesRelation(ticket, relation, currentMemberId)) return false
      const names = [ticket.creatorName, ticket.assigneeName, ticket.reviewerName]
      const activePeople = [ticket.creatorId, ticket.assigneeId, ticket.reviewerId].flatMap((id) => {
        if (id === null) return ['未分配']
        const member = membersById.get(id)
        return member ? [member.name, member.department, member.email] : ['已离开团队']
      })
      return !needle || [ticket.id, ticket.title, ticket.category, ...names, ...activePeople].join(' ').toLocaleLowerCase().includes(needle)
    })
  }, [tickets, relation, currentMemberId, query, membersById])
  const visibleTickets = searchedTickets.filter((ticket) => filter === 'all' || ticket.status === filter)
  const statusCount = (status: TicketStatus) => searchedTickets.filter((ticket) => ticket.status === status).length
  const canAssign = (ticket: WorkbenchTicket) => ticket.status !== 'completed' && (canManageTeam || ticket.creatorId === currentMemberId)
  const assigningTicket = tickets.find((ticket) => ticket.id === assigningTicketId && canAssign(ticket))
  const chooseRelation = (value: RelationFilter) => { setRelation(value); setFilter('all'); setQuery('') }
  const clearFilters = () => { setQuery(''); setFilter('all'); setRelation('involved') }

  const saveAssignment = (assigneeId: string | null, reviewerId: string | null, reason: string) => {
    if (!assigningTicket) throw new Error('工单已更新，请重新打开指派窗口。')
    assignTicket(assigningTicket.id, { assigneeId, reviewerId, reason })
    setAssigningTicketId(null)
    setAnnouncement(`已更新“${assigningTicket.title}”的指派：处理人 ${assigneeId ? membersById.get(assigneeId)?.name : '未分配'}，审核人 ${reviewerId ? membersById.get(reviewerId)?.name : '未分配'}。`)
  }

  return (
    <main className="gp-wbd">
      <div className="gp-wbd__inner">
        <header className="gp-wbd-heading">
          <div className="gp-wbd-heading__copy"><p className="gp-wbd-heading__eyebrow">工作台</p><h1>{displayName}，你好</h1><p className="gp-wbd-heading__description">从这里查看工单进展，安排今天的工作。</p></div>
          <div className="gp-wbd-heading__actions"><span className="gp-wbd-date"><CalendarDays size={15} aria-hidden="true" />{today}</span><Link className="gp-wbd-primary" to={`${ticketsPath}/new`}><Plus size={17} aria-hidden="true" />新建工单</Link></div>
        </header>

        <section className="gp-wbd-overview" aria-label="我的工单概览">
          <OverviewCard label="我创建的" value={relationCount('created')} hint="查看我提交的工单" icon={FileText} onClick={() => chooseRelation('created')} />
          <OverviewCard label="待我处理" value={relationCount('assigned')} hint="跟进处理与补充事项" icon={Clock3} onClick={() => chooseRelation('assigned')} />
          <OverviewCard label="待我审核" value={relationCount('reviewing')} hint="查看指派给我的审核" icon={ClipboardCheck} onClick={() => chooseRelation('reviewing')} review />
          <OverviewCard label="我参与的" value={relationCount('involved')} hint="查看所有相关工单" icon={Inbox} onClick={() => chooseRelation('involved')} />
        </section>

        <div className="gp-wbd-grid">
          <section className="gp-wbd-panel gp-wbd-tickets" aria-labelledby="gp-wbd-tickets-heading">
            <div className="gp-wbd-panel__header"><div><h2 id="gp-wbd-tickets-heading">我的工单</h2><p>按创建、处理与审核关系查看工单</p></div><Link className="gp-wbd-text-link" to={ticketsPath}>全部工单<ArrowRight size={14} aria-hidden="true" /></Link></div>
            <div className="gp-wbd-relations" role="group" aria-label="按我与工单的关系筛选">
              {RELATIONS.map((item) => <button key={item.value} type="button" aria-pressed={relation === item.value} aria-controls="gp-wbd-ticket-results" className={`gp-wbd-relation${relation === item.value ? ' is-active' : ''}`} onClick={() => setRelation(item.value)}>{item.label}<span>{relationCount(item.value)}</span></button>)}
            </div>
            <div className="gp-wbd-toolbar">
              <div className="gp-wbd-search"><Search size={16} aria-hidden="true" /><label htmlFor="gp-wbd-ticket-search" className="gp-wbd-sr-only">搜索工单、编号或创建人、处理人、审核人</label><input id="gp-wbd-ticket-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索工单、编号或参与人" />{query && <button type="button" className="gp-wbd-search__clear" onClick={() => setQuery('')} aria-label="清空搜索"><X size={14} aria-hidden="true" /></button>}</div>
              <span className="gp-wbd-result-count" aria-live="polite">共 {visibleTickets.length} 项</span>
            </div>
            <div className="gp-wbd-tabs" role="group" aria-label="按工单状态筛选">
              {FILTERS.map((item) => <button key={item.value} type="button" aria-pressed={filter === item.value} aria-controls="gp-wbd-ticket-results" className={`gp-wbd-tab${filter === item.value ? ' is-active' : ''}`} onClick={() => setFilter(item.value)}>{item.label}<span>{item.value === 'all' ? searchedTickets.length : statusCount(item.value)}</span></button>)}
            </div>
            {announcement && <p className="gp-wbd-announcement" role="status">{announcement}</p>}
            <div id="gp-wbd-ticket-results" className="gp-wbd-table-wrap">
              <table className="gp-wbd-table">
                <thead><tr><th scope="col" className="gp-wbd-col-title">工单信息</th><th scope="col" className="gp-wbd-col-status">状态</th><th scope="col" className="gp-wbd-col-deadline">截止时间</th><th scope="col" className="gp-wbd-col-person">创建人</th><th scope="col" className="gp-wbd-col-person">处理人</th><th scope="col" className="gp-wbd-col-person">审核人</th><th scope="col" className="gp-wbd-col-action"><span className="gp-wbd-sr-only">操作</span></th></tr></thead>
                <tbody>
                  {visibleTickets.map((ticket) => {
                    const people = [
                      { label: '创建人', id: ticket.creatorId, name: ticket.creatorName },
                      { label: '处理人', id: ticket.assigneeId, name: ticket.assigneeName },
                      { label: '审核人', id: ticket.reviewerId, name: ticket.reviewerName },
                    ]
                    return <tr key={ticket.id}>
                      <td className="gp-wbd-col-title"><Link className="gp-wbd-ticket-title" to={`${ticketsPath}/${ticket.id}`}>{ticket.title}</Link><p className="gp-wbd-ticket-meta"><span>{ticket.id}</span><span className="gp-wbd-ticket-category">{ticket.category}</span></p><dl className="gp-wbd-mobile-people">{people.map((person) => <div key={person.label}><dt>{person.label}</dt><dd><PersonLabel memberId={person.id} name={person.name} member={person.id ? membersById.get(person.id) : undefined} isCurrent={person.id === currentMemberId} compact /></dd></div>)}</dl></td>
                      <td className="gp-wbd-col-status"><span className={`gp-wbd-status is-${ticket.status}`}><span aria-hidden="true" />{STATUS_LABELS[ticket.status]}</span></td>
                      <td className={`gp-wbd-col-deadline${ticket.dueToday && ticket.status !== 'completed' ? ' is-today' : ''}`}>{ticket.deadline}</td>
                      {people.map((person) => <td className="gp-wbd-col-person" key={person.label}><PersonLabel memberId={person.id} name={person.name} member={person.id ? membersById.get(person.id) : undefined} isCurrent={person.id === currentMemberId} /></td>)}
                      <td className="gp-wbd-col-action"><div className="gp-wbd-row-actions"><Link className="gp-wbd-view" to={`${ticketsPath}/${ticket.id}`} aria-label={`查看${ticket.title}`}>查看<ArrowUpRight size={12} aria-hidden="true" /></Link>{canAssign(ticket) && <button type="button" className="gp-wbd-assign" onClick={() => setAssigningTicketId(ticket.id)} aria-label={`指派${ticket.title}的处理人与审核人`}><UserRoundCog size={12} aria-hidden="true" />指派</button>}</div></td>
                    </tr>
                  })}
                </tbody>
              </table>
              {visibleTickets.length === 0 && <div className="gp-wbd-empty"><SearchX size={27} strokeWidth={1.5} aria-hidden="true" /><h3>没有找到符合条件的工单</h3><p>试试其他关键词，或调整关系与状态筛选。</p><button type="button" className="gp-wbd-text-link" onClick={clearFilters}>清除筛选</button></div>}
            </div>
            <div className="gp-wbd-table-footer"><span>{RELATIONS.find((item) => item.value === relation)?.label} · {FILTERS.find((item) => item.value === filter)?.label} · {visibleTickets.length} 项</span><Link to={ticketsPath}>进入工单中心<ArrowRight size={13} aria-hidden="true" /></Link></div>
          </section>

          <aside className="gp-wbd-aside" aria-label="快捷入口与营销需求">
            <section className="gp-wbd-panel gp-wbd-shortcuts" aria-labelledby="gp-wbd-shortcuts-heading">
              <div className="gp-wbd-panel__header"><h2 id="gp-wbd-shortcuts-heading">快捷入口</h2></div>
              <nav className="gp-wbd-shortcut-list" aria-label="常用功能">
                <Link to={`${ticketsPath}/new`}><span className="gp-wbd-shortcut-icon"><Plus size={17} aria-hidden="true" /></span><span>新建工单<small>提交工作需求</small></span><ChevronRight size={15} aria-hidden="true" /></Link>
                <Link to={`${basePath}/marketing`}><span className="gp-wbd-shortcut-icon"><Megaphone size={17} aria-hidden="true" /></span><span>营销工作区<small>查看需求与创作进展</small></span><ChevronRight size={15} aria-hidden="true" /></Link>
                <Link to={`${basePath}/reviews`}><span className="gp-wbd-shortcut-icon"><ClipboardCheck size={17} aria-hidden="true" /></span><span>审核台<small>查看指派给我的审核</small></span><ChevronRight size={15} aria-hidden="true" /></Link>
                {canManageTeam && <>
                  <Link to={`${basePath}/team`}><span className="gp-wbd-shortcut-icon"><UsersRound size={17} aria-hidden="true" /></span><span>团队管理<small>管理成员与工作交接</small></span><ChevronRight size={15} aria-hidden="true" /></Link>
                  <Link to={`${basePath}/team/tickets`}><span className="gp-wbd-shortcut-icon"><Inbox size={17} aria-hidden="true" /></span><span>团队全部工单<small>查看与调整团队分工</small></span><ChevronRight size={15} aria-hidden="true" /></Link>
                  <Link to={`${basePath}/team/unassigned`}><span className="gp-wbd-shortcut-icon"><UserRoundCog size={17} aria-hidden="true" /></span><span>待分配工单<small>补齐处理人与审核人</small></span><ChevronRight size={15} aria-hidden="true" /></Link>
                  <Link to={`${basePath}/team/logs`}><span className="gp-wbd-shortcut-icon"><History size={17} aria-hidden="true" /></span><span>操作日志<small>查看团队与工单变更</small></span><ChevronRight size={15} aria-hidden="true" /></Link>
                </>}
              </nav>
            </section>
            <section className="gp-wbd-panel gp-wbd-marketing" aria-labelledby="gp-wbd-marketing-heading">
              <div className="gp-wbd-panel__header"><h2 id="gp-wbd-marketing-heading">最近营销需求</h2><Link className="gp-wbd-icon-link" to={`${basePath}/marketing`} aria-label="查看全部营销需求"><ArrowUpRight size={16} aria-hidden="true" /></Link></div>
              <div className="gp-wbd-marketing-list">{MARKETING_REQUESTS.map((request) => <Link key={request.id} className="gp-wbd-marketing-item" to={`${basePath}/marketing/${request.id}`}><span className="gp-wbd-marketing-item__type"><FileText size={13} aria-hidden="true" />{request.type}<span>{request.status}</span></span><h3>{request.title}</h3><span className="gp-wbd-marketing-item__time">{request.time}<ChevronRight size={13} aria-hidden="true" /></span></Link>)}</div>
            </section>
          </aside>
        </div>
      </div>
      {assigningTicket && <AssignmentDialog key={`${assigningTicket.id}:${assigningTicket.updatedAt}`} ticket={assigningTicket} teamMembers={teamMembers} onSave={saveAssignment} onClose={() => setAssigningTicketId(null)} />}
    </main>
  )
}
