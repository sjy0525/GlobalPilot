import { useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  compareEvalRuns,
  getEvalSnapshot,
  getEvalTestSet,
  listEvalRuns,
  listEvalTasks,
  markEvalBaseline,
  rebuildEvalReport,
  reviewEvalTask,
  startEvalRun,
  type CompareSide,
  type EvalCase,
  type EvalCompare,
  type EvalRun,
  type EvalTask,
  type Provenance,
  type Verdict,
} from '../../api/evals'

type Tab = 'cases' | 'runs' | 'review' | 'compare' | 'report'

const TABS: { id: Tab; label: string }[] = [
  { id: 'cases', label: '测试集' },
  { id: 'runs', label: '批量评测' },
  { id: 'review', label: '评分复核' },
  { id: 'compare', label: '版本对比' },
  { id: 'report', label: '报告' },
]

const DIMENSIONS = [
  ['facts', '事实'],
  ['completeness', '需求完成度'],
  ['tone', '品牌语气'],
  ['format', '平台格式'],
] as const

const VERDICT_LABEL: Record<Verdict, string> = {
  pass: '通过',
  fail: '不通过',
  pending: '待确认',
}

function pct(rate: number | null | undefined) {
  if (rate == null) return '—'
  return `${(rate * 100).toFixed(1)}%`
}

function seconds(ms: number | null | undefined) {
  if (ms == null) return '—'
  return `${(ms / 1000).toFixed(1)} 秒`
}

function usd(value: number | null | undefined) {
  if (value == null) return '—'
  return `$${value.toFixed(6)}`
}

function VerdictPill({ verdict }: { verdict?: string | null }) {
  const key = verdict === 'pass' || verdict === 'fail' || verdict === 'pending' ? verdict : 'pending'
  return <span className={`gp-eval__verdict gp-eval__verdict--${key}`}>{VERDICT_LABEL[key]}</span>
}

function ProvenanceChips({ provenance }: { provenance: Provenance }) {
  const model = provenance.model_settings?.generator?.model || '—'
  const items = [
    ['测试集', provenance.test_set_version],
    ['工作流', provenance.workflow_version],
    ['Prompt', `${provenance.prompt_version} · ${provenance.prompt_fingerprint}`],
    ['模型', model],
    ['知识库', provenance.knowledge_version],
    ['评分规则', `${provenance.scoring_rules_version} · ${provenance.scoring_fingerprint}`],
  ]
  return (
    <div className="gp-eval__chips">
      {items.map(([label, value]) => (
        <span key={label} className="gp-badge gp-badge--muted">
          {label} {value}
        </span>
      ))}
    </div>
  )
}

function SummaryCards({ run }: { run: EvalRun }) {
  const summary = run.summary
  if (!summary) return null
  const cards = [
    ['初稿合格率', pct(summary.first_draft_pass_rate), `${summary.first_draft_passed} / ${summary.total}`],
    ['事实错误', String(summary.fact_error_count), `待确认 ${summary.fact_pending_count}`],
    ['平均耗时', seconds(summary.duration_ms_avg), `合计 ${seconds(summary.duration_ms_total)}`],
    ['单份合格成本', usd(summary.cost_per_qualified_usd), `生成合计 ${usd(summary.api_cost_usd_total)}`],
  ]
  return (
    <div className="gp-eval__metrics">
      {cards.map(([label, value, meta]) => (
        <div key={label} className="gp-eval__metric">
          <span>{label}</span>
          <b>{value}</b>
          <span>{meta}</span>
        </div>
      ))}
    </div>
  )
}

function CopyBlock({ task }: { task: EvalTask }) {
  const platforms = task.generated_content?.platforms
  if (!platforms || Object.keys(platforms).length === 0) {
    return <p className="gp-eval__block">没有初稿。</p>
  }
  return (
    <div className="gp-eval__block">
      {Object.entries(platforms).map(([platform, item]) => (
        <p key={platform}>
          {platform}
          {'\n'}Hook: {item.hook || ''}
          {'\n'}正文: {item.body || ''}
          {'\n'}CTA: {item.cta || ''}
          {'\n'}标签: {item.tags || ''}
        </p>
      ))}
    </div>
  )
}

const EvalPage = () => {
  const [tab, setTab] = useState<Tab>('cases')
  const [snapshot, setSnapshot] = useState<Provenance | null>(null)
  const [cases, setCases] = useState<EvalCase[]>([])
  const [runs, setRuns] = useState<EvalRun[]>([])
  const [error, setError] = useState<string | null>(null)
  const [label, setLabel] = useState('')
  const [starting, setStarting] = useState(false)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [tasks, setTasks] = useState<EvalTask[]>([])
  const [openTaskId, setOpenTaskId] = useState<string | null>(null)
  const [leftId, setLeftId] = useState('')
  const [rightId, setRightId] = useState('')
  const [comparison, setComparison] = useState<EvalCompare | null>(null)
  const [reportRunId, setReportRunId] = useState('')

  const running = runs.some((run) => run.status === 'queued' || run.status === 'running')
  const completed = runs.filter((run) => run.status === 'completed')
  const activeRun = runs.find((run) => run.id === activeRunId) || null
  const reportRun = runs.find((run) => run.id === reportRunId) || null

  async function refreshRuns() {
    const next = await listEvalRuns()
    setRuns(next)
    setActiveRunId((current) => current || next[0]?.id || null)
    setReportRunId((current) => current || next.find((run) => run.report_markdown)?.id || next[0]?.id || '')
    setLeftId((current) => current || next.find((run) => run.is_baseline)?.id || next[0]?.id || '')
    return next
  }

  useEffect(() => {
    let cancelled = false
    Promise.all([getEvalSnapshot(), getEvalTestSet(), listEvalRuns()])
      .then(([nextSnapshot, testSet, nextRuns]) => {
        if (cancelled) return
        setSnapshot(nextSnapshot)
        setCases(testSet.cases)
        setRuns(nextRuns)
        setActiveRunId(nextRuns[0]?.id || null)
        setReportRunId(nextRuns[0]?.id || '')
        const baseline = nextRuns.find((run) => run.is_baseline)
        setLeftId(baseline?.id || nextRuns[0]?.id || '')
        setRightId(nextRuns.find((run) => run.id !== (baseline?.id || nextRuns[0]?.id))?.id || '')
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '评测数据没有加载出来')
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => {
      refreshRuns().catch((err: unknown) => {
        setError(err instanceof Error ? err.message : '评测进度没有刷新')
      })
    }, 2000)
    return () => window.clearInterval(id)
  }, [running])

  useEffect(() => {
    if (!activeRunId || tab !== 'review') return
    let cancelled = false
    listEvalTasks(activeRunId)
      .then((rows) => {
        if (!cancelled) setTasks(rows)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '任务没有加载出来')
      })
    return () => {
      cancelled = true
    }
  }, [activeRunId, tab, activeRun?.status, activeRun?.task_done])

  const onStart = async (event: FormEvent) => {
    event.preventDefault()
    if (!snapshot) return
    setStarting(true)
    setError(null)
    try {
      const run = await startEvalRun(label, snapshot.workflow_version)
      setLabel('')
      setActiveRunId(run.id)
      setTab('runs')
      await refreshRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '评测没有开始')
    } finally {
      setStarting(false)
    }
  }

  const onBaseline = async (runId: string) => {
    setError(null)
    try {
      await markEvalBaseline(runId)
      await refreshRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '基线没有保存')
    }
  }

  const onCompare = async (event: FormEvent) => {
    event.preventDefault()
    if (!leftId || !rightId) return
    setError(null)
    try {
      setComparison(await compareEvalRuns(leftId, rightId))
    } catch (err) {
      setError(err instanceof Error ? err.message : '这两次评测不能对比')
    }
  }

  const onRebuild = async () => {
    if (!reportRunId) return
    setError(null)
    try {
      const run = await rebuildEvalReport(reportRunId)
      setRuns((current) => current.map((item) => (item.id === run.id ? run : item)))
    } catch (err) {
      setError(err instanceof Error ? err.message : '报告没有更新')
    }
  }

  return (
    <main className="gp-page gp-page--wide">
      <div className="gp-page__inner">
        <header className="gp-page__head">
          <div>
            <p className="gp-kicker">Eval</p>
            <h1 className="gp-page__title">评测管理</h1>
            <p className="gp-page__lede">
              保存当前版本作为基线，改 Prompt、检索或审核后再用同一测试集跑一遍。初稿合格率的分母包含超时和空输出。
            </p>
          </div>
        </header>

        <div className="gp-eval__tabs" role="tablist">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              className={`gp-eval__tab${tab === item.id ? ' is-active' : ''}`}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>

        {error ? <p className="gp-marketing__error">{error}</p> : null}

        {tab === 'cases' ? <CaseList cases={cases} /> : null}
        {tab === 'runs' ? (
          <RunList
            snapshot={snapshot}
            runs={runs}
            label={label}
            starting={starting}
            onLabel={setLabel}
            onStart={onStart}
            onBaseline={onBaseline}
          />
        ) : null}
        {tab === 'review' ? (
          <ReviewList
            runs={runs}
            activeRun={activeRun}
            tasks={tasks}
            openTaskId={openTaskId}
            onRun={setActiveRunId}
            onOpen={setOpenTaskId}
            onReviewed={(task) => {
              setTasks((current) => current.map((item) => (item.id === task.id ? task : item)))
              refreshRuns().catch(() => undefined)
            }}
          />
        ) : null}
        {tab === 'compare' ? (
          <ComparePanel
            runs={completed}
            leftId={leftId}
            rightId={rightId}
            comparison={comparison}
            onLeft={setLeftId}
            onRight={setRightId}
            onSubmit={onCompare}
          />
        ) : null}
        {tab === 'report' ? (
          <ReportPanel run={reportRun} runs={runs} runId={reportRunId} onRun={setReportRunId} onRebuild={onRebuild} />
        ) : null}
      </div>
    </main>
  )
}

function CaseList({ cases }: { cases: EvalCase[] }) {
  if (!cases.length) {
    return (
      <div className="gp-empty">
        <h2 className="gp-empty__title">测试集还没准备好</h2>
        <p className="gp-empty__copy">启动 API 后会写入 30 条营销需求。</p>
      </div>
    )
  }
  return (
    <section className="gp-panel">
      <h2 className="gp-panel__title">营销场景 {cases.length} 条</h2>
      <p className="gp-panel__meta">每条都有用户需求、品牌资料、目标平台、必须包含的卖点和不能出现的说法。</p>
      <div className="mt-4">
        {cases.map((item) => (
          <details key={item.id} className="gp-eval__case">
            <summary>
              {item.code} {item.title}
              <span className="gp-badge gp-badge--muted ml-2">{item.platforms.join(' / ')}</span>
            </summary>
            <div>
              <p>用户需求：{item.user_request}</p>
              <p className="mt-2">品牌资料：{item.brand_profile}</p>
              <p className="mt-2">必须包含：{item.must_include.join('、')}</p>
              <p className="mt-2">不能出现：{item.forbidden_claims.join('、')}</p>
              <p className="mt-2">
                {item.brand_name} · {item.region} · {item.persona}
                {item.purchase_channels ? ` · ${item.purchase_channels}` : ''}
              </p>
            </div>
          </details>
        ))}
      </div>
    </section>
  )
}

function RunList({
  snapshot,
  runs,
  label,
  starting,
  onLabel,
  onStart,
  onBaseline,
}: {
  snapshot: Provenance | null
  runs: EvalRun[]
  label: string
  starting: boolean
  onLabel: (value: string) => void
  onStart: (event: FormEvent) => void
  onBaseline: (runId: string) => void
}) {
  return (
    <section className="gp-panel">
      <h2 className="gp-panel__title">用当前工作流跑初稿</h2>
      <p className="gp-panel__meta">
        只执行检索、策略和第一版文案，不进入自审返工。下面的版本会写进这次运行，之后改代码也不会改掉它。
      </p>
      {snapshot ? <ProvenanceChips provenance={snapshot} /> : null}
      <form className="gp-eval__actions" onSubmit={onStart}>
        <label className="gp-field min-w-64">
          运行名称
          <input value={label} onChange={(event) => onLabel(event.target.value)} placeholder="例如：改完事实提示词" />
        </label>
        <button className="gp-btn gp-btn--primary" type="submit" disabled={!snapshot || starting}>
          {starting ? '正在排队' : `跑 ${snapshot?.test_set_version || '测试集'}`}
        </button>
      </form>
      <div className="gp-eval__table-wrap mt-6">
        <table className="gp-eval__table">
          <thead>
            <tr>
              <th>运行</th>
              <th>进度</th>
              <th>初稿合格率</th>
              <th>事实错误</th>
              <th>版本</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {runs.length === 0 ? (
              <tr>
                <td colSpan={6}>还没有评测。先跑一轮，再把它存成基线。</td>
              </tr>
            ) : null}
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <div className="font-semibold">{run.label}</div>
                  <div className="text-xs text-[var(--gp-ink-muted)]">
                    {run.status}
                    {run.is_baseline ? ' · 基线' : ''}
                    {run.error ? ` · ${run.error}` : ''}
                  </div>
                </td>
                <td>
                  {run.task_done}/{run.task_total}
                </td>
                <td>{pct(run.summary?.first_draft_pass_rate)}</td>
                <td>{run.summary ? run.summary.fact_error_count : '—'}</td>
                <td className="text-xs">
                  {run.provenance.workflow_version}
                  <br />
                  {run.provenance.prompt_version}
                </td>
                <td>
                  {run.status === 'completed' && !run.is_baseline ? (
                    <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" onClick={() => onBaseline(run.id)}>
                      存为基线
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function ReviewList({
  runs,
  activeRun,
  tasks,
  openTaskId,
  onRun,
  onOpen,
  onReviewed,
}: {
  runs: EvalRun[]
  activeRun: EvalRun | null
  tasks: EvalTask[]
  openTaskId: string | null
  onRun: (id: string) => void
  onOpen: (id: string | null) => void
  onReviewed: (task: EvalTask) => void
}) {
  const openTask = tasks.find((task) => task.id === openTaskId) || null
  return (
    <section className="gp-panel">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="gp-panel__title">评分与复核</h2>
          <p className="gp-panel__meta">格式和字数由代码判断。事实、需求完成度和品牌语气由独立评测模型判断。四项都通过才算合格。</p>
        </div>
        <label className="gp-field min-w-56">
          评测运行
          <select value={activeRun?.id || ''} onChange={(event) => onRun(event.target.value)}>
            {runs.map((run) => (
              <option key={run.id} value={run.id}>
                {run.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {activeRun ? <SummaryCards run={activeRun} /> : null}
      <div className="gp-eval__table-wrap">
        <table className="gp-eval__table">
          <thead>
            <tr>
              <th>用例</th>
              <th>结果</th>
              <th>事实</th>
              <th>完成度</th>
              <th>语气</th>
              <th>格式</th>
              <th>耗时</th>
              <th>成本</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr
                key={task.id}
                className="gp-eval__row"
                onClick={() => onOpen(openTaskId === task.id ? null : task.id)}
              >
                <td>
                  <div className="font-semibold">
                    {task.code} {task.title}
                  </div>
                  <div className="text-xs text-[var(--gp-ink-muted)]">
                    {task.timed_out ? '超时' : task.empty_output ? '空输出' : task.execution_status}
                  </div>
                </td>
                <td>{task.qualified ? <VerdictPill verdict="pass" /> : <VerdictPill verdict="fail" />}</td>
                {DIMENSIONS.map(([key]) => (
                  <td key={key}>
                    <VerdictPill verdict={task.scores?.[key]?.verdict} />
                  </td>
                ))}
                <td>{seconds(task.duration_ms)}</td>
                <td>{usd(task.api_cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {openTask ? <TaskDetail task={openTask} onReviewed={onReviewed} /> : null}
    </section>
  )
}

function TaskDetail({ task, onReviewed }: { task: EvalTask; onReviewed: (task: EvalTask) => void }) {
  const locked = task.timed_out || task.empty_output || task.execution_status === 'timeout' || task.execution_status === 'error'
  const [note, setNote] = useState('')
  const [pending, setPending] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Record<(typeof DIMENSIONS)[number][0], Verdict>>({
    facts: task.scores?.facts?.verdict || 'pending',
    completeness: task.scores?.completeness?.verdict || 'pending',
    tone: task.scores?.tone?.verdict || 'pending',
    format: task.scores?.format?.verdict || 'pending',
  })

  useEffect(() => {
    setDraft({
      facts: task.scores?.facts?.verdict || 'pending',
      completeness: task.scores?.completeness?.verdict || 'pending',
      tone: task.scores?.tone?.verdict || 'pending',
      format: task.scores?.format?.verdict || 'pending',
    })
    setNote(task.human_note || '')
  }, [task])

  const changed = DIMENSIONS.some(([key]) => draft[key] !== task.scores?.[key]?.verdict)

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setPending(true)
    setLocalError(null)
    try {
      const body: { note: string; facts?: Verdict; completeness?: Verdict; tone?: Verdict; format?: Verdict } = {
        note: note.trim(),
      }
      for (const [key] of DIMENSIONS) {
        if (draft[key] !== task.scores?.[key]?.verdict) body[key] = draft[key]
      }
      onReviewed(await reviewEvalTask(task.id, body))
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : '复核没有保存')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="mt-5 border-t border-[var(--gp-line)] pt-5">
      <h3 className="gp-panel__title">
        {task.code} {task.title}
      </h3>
      {task.failure_reasons.length ? (
        <ul className="mt-3 list-disc pl-5 text-sm">
          {task.failure_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : (
        <p className="gp-panel__meta">四项都通过，这是未经返工的合格初稿。</p>
      )}
      <div className="gp-eval__detail mt-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--gp-ink-muted)]">用户需求</p>
          <p className="gp-eval__block">{task.user_request}</p>
          <p className="mt-3 text-xs font-semibold uppercase tracking-[0.12em] text-[var(--gp-ink-muted)]">品牌资料</p>
          <p className="gp-eval__block">{task.brand_profile}</p>
          <p className="mt-3 text-xs font-semibold uppercase tracking-[0.12em] text-[var(--gp-ink-muted)]">检索资料</p>
          <p className="gp-eval__block">{task.retrieved_context || '没有检索到资料'}</p>
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[var(--gp-ink-muted)]">初稿</p>
          <CopyBlock task={task} />
          {task.error ? <p className="gp-marketing__error mt-3">{task.error}</p> : null}
        </div>
      </div>
      {locked ? (
        <p className="gp-panel__meta mt-4">超时、空输出和报错保持失败，不会从统计里拿掉。</p>
      ) : (
        <form className="mt-4 grid gap-3" onSubmit={onSubmit}>
          <div className="grid gap-3 sm:grid-cols-4">
            {DIMENSIONS.map(([key, label]) => (
              <label key={key} className="gp-field">
                {label}
                <select
                  value={draft[key]}
                  onChange={(event) => setDraft((current) => ({ ...current, [key]: event.target.value as Verdict }))}
                >
                  <option value="pass">通过</option>
                  <option value="fail">不通过</option>
                  <option value="pending">待确认</option>
                </select>
              </label>
            ))}
          </div>
          <label className="gp-field">
            纠正原因
            <textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="写清模型哪里判错了" />
          </label>
          {localError ? <p className="gp-marketing__error">{localError}</p> : null}
          <button className="gp-btn gp-btn--primary gp-btn--sm w-fit" type="submit" disabled={pending || !changed || !note.trim()}>
            保存复核
          </button>
        </form>
      )}
    </div>
  )
}

function SideReasons({ side }: { side: CompareSide }) {
  return (
    <ul className="list-disc pl-5">
      {side.reasons.map((reason) => (
        <li key={reason}>{reason}</li>
      ))}
      <li>
        事实 <VerdictPill verdict={side.facts_verdict} /> · {seconds(side.duration_ms)} · {usd(side.api_cost_usd)}
        {side.timed_out ? ' · 超时' : ''}
        {side.empty_output ? ' · 空输出' : ''}
      </li>
    </ul>
  )
}

function ComparePanel({
  runs,
  leftId,
  rightId,
  comparison,
  onLeft,
  onRight,
  onSubmit,
}: {
  runs: EvalRun[]
  leftId: string
  rightId: string
  comparison: EvalCompare | null
  onLeft: (id: string) => void
  onRight: (id: string) => void
  onSubmit: (event: FormEvent) => void
}) {
  const failures = useMemo(
    () => (comparison ? comparison.rows.filter((row) => !row.left.qualified || !row.right.qualified) : []),
    [comparison],
  )
  return (
    <section className="gp-panel">
      <h2 className="gp-panel__title">同一批任务的前后对比</h2>
      <p className="gp-panel__meta">两次运行必须是同一个测试集版本。点开一条可以看到两侧的失败原因。</p>
      <form className="gp-eval__actions" onSubmit={onSubmit}>
        <label className="gp-field min-w-56">
          修改前
          <select value={leftId} onChange={(event) => onLeft(event.target.value)}>
            <option value="">选择运行</option>
            {runs.map((run) => (
              <option key={run.id} value={run.id}>
                {run.is_baseline ? '基线 · ' : ''}
                {run.label}
              </option>
            ))}
          </select>
        </label>
        <label className="gp-field min-w-56">
          修改后
          <select value={rightId} onChange={(event) => onRight(event.target.value)}>
            <option value="">选择运行</option>
            {runs.map((run) => (
              <option key={run.id} value={run.id}>
                {run.label}
              </option>
            ))}
          </select>
        </label>
        <button className="gp-btn gp-btn--primary" type="submit" disabled={!leftId || !rightId}>
          对比
        </button>
      </form>
      {comparison ? (
        <div className="mt-6">
          <div className="gp-eval__metrics">
            <div className="gp-eval__metric">
              <span>初稿合格率</span>
              <b>
                {pct(comparison.left.summary?.first_draft_pass_rate)} → {pct(comparison.right.summary?.first_draft_pass_rate)}
              </b>
            </div>
            <div className="gp-eval__metric">
              <span>事实错误</span>
              <b>
                {comparison.left.summary?.fact_error_count ?? '—'} → {comparison.right.summary?.fact_error_count ?? '—'}
              </b>
            </div>
            <div className="gp-eval__metric">
              <span>平均耗时</span>
              <b>
                {seconds(comparison.left.summary?.duration_ms_avg)} → {seconds(comparison.right.summary?.duration_ms_avg)}
              </b>
            </div>
            <div className="gp-eval__metric">
              <span>单份合格成本</span>
              <b>
                {usd(comparison.left.summary?.cost_per_qualified_usd)} → {usd(comparison.right.summary?.cost_per_qualified_usd)}
              </b>
            </div>
          </div>
          {failures.length === 0 ? <p className="gp-panel__meta">两侧都没有未合格任务。</p> : null}
          {failures.map((row) => (
            <details key={row.code} className="gp-eval__case">
              <summary>
                {row.code} {row.title}
              </summary>
              <div className="gp-eval__detail">
                <div>
                  <p className="font-semibold text-[var(--gp-ink)]">{comparison.left.label}</p>
                  <SideReasons side={row.left} />
                </div>
                <div>
                  <p className="font-semibold text-[var(--gp-ink)]">{comparison.right.label}</p>
                  <SideReasons side={row.right} />
                </div>
              </div>
            </details>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function ReportPanel({
  run,
  runs,
  runId,
  onRun,
  onRebuild,
}: {
  run: EvalRun | null
  runs: EvalRun[]
  runId: string
  onRun: (id: string) => void
  onRebuild: () => void
}) {
  return (
    <section className="gp-panel">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="gp-panel__title">评测报告</h2>
          <p className="gp-panel__meta">报告里固定这次的测试集、工作流、Prompt、模型、知识库和评分规则。</p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="gp-field min-w-56">
            运行
            <select value={runId} onChange={(event) => onRun(event.target.value)}>
              {runs.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" onClick={onRebuild} disabled={!runId}>
            按最新复核重算
          </button>
        </div>
      </div>
      {run?.provenance ? <ProvenanceChips provenance={run.provenance} /> : null}
      {run?.summary ? (
        <div className="mt-4">
          <SummaryCards run={run} />
        </div>
      ) : null}
      <pre className="gp-eval__report mt-4">{run?.report_markdown || '这次运行还没有报告。'}</pre>
    </section>
  )
}

export default EvalPage
