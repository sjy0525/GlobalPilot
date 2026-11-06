import { useEffect, useMemo, useState } from 'react'
import { Navigate, useLocation } from 'react-router'
import { ArrowDown, ArrowUpRight, History, Search, SearchX, ShieldCheck, X } from 'lucide-react'
import { useWorkbench } from '../../workbench/context'
import { ACTION_LABELS, type OperationLog } from '../../workbench/model'
import Link from '../../workbench/WorkbenchLink'
import '../../css/components/workbench-admin.css'

const TIME_FILTERS = [{ value: 'all', label: '全部时间' }, { value: 'today', label: '今天' }, { value: '7', label: '最近 7 天' }, { value: '30', label: '最近 30 天' }]
const dateFormatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
const dayFormatter = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })

export default function OperationLogs() {
  const { logs, canManageTeam, basePath, currentMemberId } = useWorkbench()
  const location = useLocation()
  const [query, setQuery] = useState('')
  const [actor, setActor] = useState('')
  const [action, setAction] = useState('')
  const [targetType, setTargetType] = useState('')
  const [time, setTime] = useState('all')
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000)
    return () => window.clearInterval(timer)
  }, [])
  const actors = useMemo(() => [...new Map(logs.map((log) => [log.actorId, { id: log.actorId, name: log.actorName }])).values()], [logs])
  const orderedLogs = useMemo(() => [...logs].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()), [logs])
  const todayCount = logs.filter((log) => dayFormatter.format(new Date(log.createdAt)) === dayFormatter.format(new Date(now))).length
  const visibleLogs = orderedLogs.filter((log) => {
    if (actor && log.actorId !== actor) return false
    if (action && log.action !== action) return false
    if (targetType && log.targetType !== targetType) return false
    if (time === 'today' && dayFormatter.format(new Date(log.createdAt)) !== dayFormatter.format(new Date(now))) return false
    if (time !== 'all' && time !== 'today' && new Date(log.createdAt).getTime() < now - Number(time) * 24 * 60 * 60 * 1000) return false
    const needle = query.trim().toLocaleLowerCase()
    return !needle || [log.actorName, ACTION_LABELS[log.action], log.targetName, log.targetId, log.before, log.after, log.reason, log.targetType === 'member' ? '团队成员' : '工单'].join(' ').toLocaleLowerCase().includes(needle)
  })
  const hasFilters = Boolean(query || actor || action || targetType || time !== 'all')
  const actionCount = (kind: OperationLog['action']) => logs.filter((log) => log.action === kind).length
  function clearFilters() { setQuery(''); setActor(''); setAction(''); setTargetType(''); setTime('all') }

  if (!canManageTeam) return <Navigate to={{ pathname: basePath, search: location.search }} replace />

  return (
    <main className="gp-admin">
      <div className="gp-admin__inner">
        <header className="gp-admin-heading"><div><p className="gp-admin-eyebrow">团队管理</p><h1>操作日志</h1><p>查看团队成员与工单分配的变化，追溯每次调整的原因。</p></div><span className="gp-admin-readonly"><ShieldCheck size={15} aria-hidden="true" />记录仅供查看</span></header>
        <section className="gp-admin-log-overview" aria-label="操作记录概览"><div><History size={19} aria-hidden="true" /><span>累计操作<strong>{logs.length}<small>条</small></strong></span></div><div><span>今日操作<strong>{todayCount}<small>条</small></strong></span></div><div><span>工单改派<strong>{actionCount('ticket_reassigned')}<small>次</small></strong></span></div><p>成员添加、移除、角色调整与工单改派<br />统一记录操作者、变化内容和原因</p></section>

        <section className="gp-admin-panel" aria-labelledby="gp-admin-logs-title">
          <div className="gp-admin-panel-heading"><div><h2 id="gp-admin-logs-title">操作记录</h2><p>按最新时间排序，保留历史操作者与对象名称。</p></div><span className="gp-admin-total">共 {logs.length} 条</span></div>
          <div className="gp-admin-filters">
            <div className="gp-admin-search"><Search size={16} aria-hidden="true" /><label className="gp-admin-sr-only" htmlFor="gp-admin-log-search">搜索操作者、对象、变化或原因</label><input id="gp-admin-log-search" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索操作者、对象、变化或原因" />{query && <button className="gp-admin-icon-button" type="button" aria-label="清空日志搜索" onClick={() => setQuery('')}><X size={14} aria-hidden="true" /></button>}</div>
            <div className="gp-admin-filter-row">
              <label className="gp-admin-filter"><span>操作者</span><select value={actor} onChange={(event) => setActor(event.target.value)}><option value="">全部操作者</option>{actors.map((person) => <option key={person.id} value={person.id}>{person.name}</option>)}</select></label>
              <label className="gp-admin-filter"><span>动作</span><select value={action} onChange={(event) => setAction(event.target.value)}><option value="">全部动作</option>{Object.entries(ACTION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label className="gp-admin-filter"><span>对象</span><select value={targetType} onChange={(event) => setTargetType(event.target.value)}><option value="">全部对象</option><option value="member">团队成员</option><option value="ticket">工单</option></select></label>
              <label className="gp-admin-filter"><span>时间</span><select value={time} onChange={(event) => setTime(event.target.value)}>{TIME_FILTERS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
              {hasFilters && <button className="gp-admin-text-button" type="button" onClick={clearFilters}>重置筛选</button>}
            </div>
          </div>
          <div className="gp-admin-table-wrap"><table className="gp-admin-table gp-admin-log-table"><thead><tr><th scope="col">操作者 / 时间</th><th scope="col">动作</th><th scope="col">操作对象</th><th scope="col">前后变化</th><th scope="col">原因</th></tr></thead><tbody>{visibleLogs.map((log) => <tr key={log.id}>
            <td><span className="gp-admin-log-actor"><span className="gp-admin-log-avatar" aria-hidden="true">{log.actorName.slice(0, 1)}</span><strong>{log.actorName}{log.actorId === currentMemberId && <small className="gp-admin-self">我</small>}</strong></span><time className="gp-admin-log-time" dateTime={log.createdAt}>{dateFormatter.format(new Date(log.createdAt))}</time></td>
            <td><span className={`gp-admin-action is-${log.action}`}>{ACTION_LABELS[log.action]}</span></td>
            <td>{log.targetType === 'ticket' ? <Link className="gp-admin-log-target" to={`${basePath}/tickets/${log.targetId}`}>{log.targetName}<ArrowUpRight size={12} aria-hidden="true" /></Link> : <strong className="gp-admin-log-target">{log.targetName}</strong>}<small className="gp-admin-log-target-meta">{log.targetType === 'ticket' ? log.targetId : '团队成员'}</small></td>
            <td><div className="gp-admin-changes"><div><span>调整前</span><p>{log.before || '无'}</p></div><ArrowDown size={12} aria-hidden="true" /><div><span>调整后</span><p>{log.after || '无'}</p></div></div></td>
            <td><p className="gp-admin-log-reason">{log.reason || '未填写原因'}</p></td>
          </tr>)}</tbody></table>
            {visibleLogs.length === 0 && <div className="gp-admin-empty">{hasFilters ? <SearchX size={30} strokeWidth={1.5} aria-hidden="true" /> : <History size={30} strokeWidth={1.5} aria-hidden="true" />}<h3>{hasFilters ? '没有找到符合条件的记录' : '暂无操作记录'}</h3><p>{hasFilters ? '试试其他关键词，或调整筛选条件。' : '团队成员与工单分配发生变化后，会在这里留下记录。'}</p>{hasFilters && <button className="gp-admin-text-button" type="button" onClick={clearFilters}>清除筛选</button>}</div>}
          </div>
          <footer className="gp-admin-panel-footer"><span aria-live="polite">当前显示 {visibleLogs.length} 条记录</span><span>按操作时间倒序</span></footer>
        </section>
      </div>
    </main>
  )
}
