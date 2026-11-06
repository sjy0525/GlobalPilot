import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { ApiError } from '../../api/http'
import type { Brief, Region, SocialPlatform } from '../../api/briefs'
import {
  getMarketingWorkflow,
  hasMarketingCopy,
  loadOrStartWorkflow,
  reviewOrStreamWorkflow,
  type ContentReview,
  type GraphNode,
  type MarketingWorkflow,
  type ReviewAction,
  type WorkflowStatus,
  type WorkflowStreamEvent,
} from '../../api/marketing'
import ChannelCopy from './Copy'
import { ResearchCard } from './Research'
import { MemoryCard } from './Memories'
import Revisions from './Revisions'
import { useMarketingScope, type MarketingScope } from './useMarketingScope'
import {
  FALLBACK_SEQUENCE,
  NODE_COPY,
  PIPELINE_STEPS,
  meterTarget,
  nodeToPipeline,
  statusToNode,
  stepState,
  type NodePhase,
  type PipelineId,
} from './pipeline'

type MarketingLocationState = {
  brief?: Brief
}

type Phase = 'idle' | 'loading' | 'generating' | 'ready' | 'error'

const REGION_LABELS: Record<Region, string> = {
  'eu-na': 'Europe & North America',
  sea: 'Southeast Asia',
  'jp-kr': 'Japan & Korea',
  other: 'Other',
}

const SOCIAL_LABELS: Record<SocialPlatform, string> = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
  youtube: 'YouTube',
  linkedin: 'LinkedIn',
  facebook: 'Facebook',
  x: 'X',
}

const STATUS_COPY: Record<
  WorkflowStatus,
  { badge: string; tone: 'sea' | 'warn' | 'muted'; lede: string }
> = {
  awaiting_review: {
    badge: 'In review',
    tone: 'sea',
    lede: 'Fact checks already passed. Approve the draft or request a revision.',
  },
  completed: {
    badge: 'Approved',
    tone: 'sea',
    lede: 'Approved copy is saved. Use a targeted revision to update selected body paragraphs.',
  },
  no_knowledge: {
    badge: 'No knowledge',
    tone: 'warn',
    lede: 'Nothing in the brand library matched this brief. Upload documents, then run research again.',
  },
  max_revisions: {
    badge: 'Revision cap',
    tone: 'warn',
    lede: 'The workflow hit its revision limit. The last draft is below.',
  },
  self_review_failed: {
    badge: 'Self-review failed',
    tone: 'warn',
    lede: 'Automatic revisions could not pass self-review. The blocker report is below.',
  },
}

function errorMessage(err: unknown) {
  return err instanceof Error ? err.message : 'Unable to load this marketing run.'
}

function formatElapsed(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

function useElapsed(running: boolean) {
  const [clock, setClock] = useState({ running, ms: 0 })
  // Reset on an input transition before rendering children. The effect only
  // subscribes to the timer, so a new run never paints the previous elapsed time.
  if (clock.running !== running) setClock({ running, ms: 0 })
  useEffect(() => {
    if (!running) return
    const started = Date.now()
    let active = true
    const id = window.setInterval(() => {
      if (active) setClock({ running: true, ms: Date.now() - started })
    }, 1000)
    return () => { active = false; window.clearInterval(id) }
  }, [running])
  return clock.running === running ? clock.ms : 0
}

function useMeterPercent(target: number, generating: boolean) {
  const [meter, setMeter] = useState({ target, generating, shown: target })
  if (meter.target !== target || meter.generating !== generating) {
    setMeter({
      target,
      generating,
      shown: generating && meter.generating ? Math.max(meter.shown, target) : target,
    })
  }
  useEffect(() => {
    if (!generating) return
    let active = true
    const id = window.setInterval(() => {
      if (!active) return
      setMeter((value) => {
        if (value.target !== target || value.generating !== generating) return value
        const cap = Math.min(target + 12, 96)
        if (value.shown >= cap) return value
        return { ...value, shown: Math.round((value.shown + 0.4) * 10) / 10 }
      })
    }, 280)
    return () => { active = false; window.clearInterval(id) }
  }, [target, generating])
  return Math.min(100, generating ? Math.max(meter.shown, target) : target)
}

const Marketing = () => {
  const location = useLocation()
  const navigate = useNavigate()
  const locationBrief = (location.state as MarketingLocationState | null)?.brief
  const scope = useMarketingScope(locationBrief, location.search)
  const navigationScope: MarketingScope = {
    ...scope,
    selectBrief: (briefId) => {
      const selected = scope.briefs.find((record) => record.id === briefId)
      if (!selected) return
      scope.selectBrief(briefId)
      const params = new URLSearchParams(location.search)
      params.set('brief_id', briefId)
      navigate({ pathname: location.pathname, search: '?' + params.toString() }, { replace: true, state: { brief: selected } })
    },
    selectTeam: async (teamId) => {
      const params = new URLSearchParams(location.search)
      params.delete('brief_id')
      navigate({ pathname: location.pathname, search: params.toString() ? '?' + params.toString() : '' }, { replace: true, state: {} })
      await scope.selectTeam(teamId)
    },
  }
  return <MarketingDesk key={`${scope.userId}:${scope.teamId}:${scope.brief?.id ?? ''}`} scope={navigationScope} />
}

const MarketingDesk = ({ scope }: { scope: MarketingScope }) => {
  const { brief, teamId, userId } = scope
  const mounted = useRef(true)
  const [workflow, setWorkflow] = useState<MarketingWorkflow | null>(null)
  const [phase, setPhase] = useState<Phase>(brief ? 'loading' : 'idle')
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState('')
  const [feedbackError, setFeedbackError] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<ReviewAction | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [activeNode, setActiveNode] = useState<GraphNode | null>(null)
  const [nodePhase, setNodePhase] = useState<NodePhase>('running')
  const [streamed, setStreamed] = useState(false)
  const [revisionBusy, setRevisionBusy] = useState(false)
  const briefId = brief?.id ?? null
  const generating = phase === 'generating' || pendingAction !== null
  const booting = phase === 'loading'
  const elapsed = useElapsed(generating)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    // MarketingDesk is keyed by actor/team/brief, so a scope change already
    // gets fresh state. Explicit retries reset their state in the submit event.
    if (!briefId || !teamId) return

    let cancelled = false

    const applyStreamEvent = (event: WorkflowStreamEvent) => {
      if (event.type !== 'node') return
      if (cancelled) return
      setPhase('generating')
      setStreamed(true)
      setActiveNode(event.node)
      setNodePhase(event.phase)
      if (!event.strategy && !event.content && !event.research) return
      setWorkflow((current) => ({
        brief_id: current?.brief_id || briefId,
        checkpoint_id: null,
        status: current?.status ?? 'awaiting_review',
        revision_count: current?.revision_count ?? 0,
        strategy: event.strategy ?? current?.strategy ?? null,
        content: event.content ?? current?.content ?? null,
        research: event.research ?? current?.research ?? null,
        research_validation: event.research_validation ?? current?.research_validation ?? null,
        final_output: current?.final_output ?? null,
      }))
    }

    void (async () => {
      try {
        const next = await loadOrStartWorkflow(
          briefId,
          applyStreamEvent,
          () => {
            if (cancelled) return
            setPhase('generating')
            setActiveNode('retrieve_context')
            setNodePhase('running')
          },
          teamId,
          () => !cancelled,
        )
        if (cancelled) return
        setWorkflow(next)
        setActiveNode(statusToNode(next.status))
        setNodePhase('done')
        setPhase('ready')
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) {
          setWorkflow(null)
          setPhase('error')
          setError('这份稿件已不可访问，请刷新稿件列表。')
          return
        }
        setError(errorMessage(err))
        setPhase('error')
      }
    })()

    return () => {
      cancelled = true
    }
  }, [briefId, teamId, reloadKey])

  useEffect(() => {
    if (!generating || streamed) return
    const sequence: GraphNode[] =
      pendingAction === 'approved'
        ? ['final_output']
        : pendingAction === 'rejected'
          ? ['generate_strategy', 'generate_content', 'self_review']
          : FALLBACK_SEQUENCE
    const delays =
      pendingAction === 'approved'
        ? [999_999]
        : pendingAction === 'rejected'
          ? [16_000, 20_000, 999_999]
          : [4_000, 30_000, 18_000, 999_999]
    let step = 0
    let timer = 0
    // The generation/review event already sets the first stage immediately.
    const startFallback = window.setTimeout(() => {
      const tick = () => {
        if (step >= sequence.length - 1) return
        step += 1
        setActiveNode(sequence[step])
        timer = window.setTimeout(tick, delays[step] ?? 20_000)
      }
      timer = window.setTimeout(tick, delays[0])
    }, 700)
    return () => {
      window.clearTimeout(startFallback)
      window.clearTimeout(timer)
    }
  }, [generating, streamed, pendingAction, reloadKey])

  const status = workflow?.status
  const showCopy =
    status !== 'no_knowledge' && (hasMarketingCopy(workflow?.content) || generating || booting)
  const currentPipeline: PipelineId = activeNode
    ? nodeToPipeline(activeNode)
    : status
      ? nodeToPipeline(statusToNode(status))
      : 'knowledge'
  const target = meterTarget(activeNode, nodePhase, status, generating)
  const percent = useMeterPercent(target, generating)

  const platforms = useMemo(() => {
    if (!brief) return ''
    return brief.social_media.map((platform) => SOCIAL_LABELS[platform]).join(' · ')
  }, [brief])

  const liveCopy = activeNode ? NODE_COPY[activeNode] : null

  const onReview = async (action: ReviewAction) => {
    if (!brief || !teamId || revisionBusy || pendingAction) return
    if (!workflow?.checkpoint_id) {
      setError("请重新加载稿件后再审核。")
      return
    }
    const note = feedback.trim()
    if (action === 'rejected' && !note) {
      setFeedbackError('请填写修改意见后再退回。')
      return
    }
    if (action === 'approved' && note) {
      setFeedbackError('已填写修改意见时请使用退回，或先清空意见再批准。')
      return
    }
    setFeedbackError(null)
    setError(null)
    setPendingAction(action)
    setStreamed(false)
    setActiveNode(action === 'approved' ? 'final_output' : 'generate_strategy')
    setNodePhase('running')
    setPhase('generating')
    try {
      const next = await reviewOrStreamWorkflow(
        brief.id,
        action,
        workflow.checkpoint_id,
        action === 'rejected' ? note : null,
        (event) => {
          if (!mounted.current) return
          if (event.type !== 'node') return
          setStreamed(true)
          setActiveNode(event.node)
          setNodePhase(event.phase)
          if (!event.strategy && !event.content && !event.research) return
          setWorkflow((current) => ({
            brief_id: current?.brief_id || brief.id,
            checkpoint_id: null,
            status: current?.status ?? 'awaiting_review',
            revision_count: current?.revision_count ?? 0,
            strategy: event.strategy ?? current?.strategy ?? null,
            content: event.content ?? current?.content ?? null,
            research: event.research ?? current?.research ?? null,
            research_validation: event.research_validation ?? current?.research_validation ?? null,
            final_output: current?.final_output ?? null,
          }))
        },
        teamId,
        () => mounted.current,
      )
      if (!mounted.current) return
      setWorkflow(next)
      setFeedback('')
      setActiveNode(statusToNode(next.status))
      setNodePhase('done')
      setPhase('ready')
    } catch (err) {
      if (!mounted.current) return
      setError(errorMessage(err))
      // Recovery reloads the checkpoint; never approve a stale or partial draft.
      setPhase('error')
    } finally {
      if (mounted.current) setPendingAction(null)
    }
  }

  const onRetry = (e: FormEvent) => {
    e.preventDefault()
    if (!briefId) return
    setError(null)
    setWorkflow(null)
    setStreamed(false)
    setActiveNode(null)
    setNodePhase('running')
    setPhase('loading')
    setReloadKey((key) => key + 1)
  }

  const writingCopy =
    (generating || booting) && !hasMarketingCopy(workflow?.content)

  return (
    <main className="gp-page gp-page--wide">
      <div className="gp-page__inner">
        <header className="gp-page__head">
          <div>
            <p className="gp-kicker">Workspace</p>
            <h1 className="gp-page__title">Marketing</h1>
            <p className="gp-page__lede">
              {generating && liveCopy
                ? liveCopy.detail
                : booting
                  ? 'Opening the latest snapshot for this brief.'
                  : status
                    ? STATUS_COPY[status].lede
                    : brief
                      ? 'Retrieving brand context, then drafting channel copy.'
                      : 'Run a market brief first. This desk picks up the saved run and walks it through review.'}
            </p>
          </div>
          {status && !generating && !booting ? (
            <span
              className={`gp-badge${STATUS_COPY[status].tone === 'warn' ? ' gp-badge--warn' : STATUS_COPY[status].tone === 'muted' ? ' gp-badge--muted' : ''}`}
            >
              {STATUS_COPY[status].badge}
              {workflow && workflow.revision_count > 0
                ? ` · rev ${workflow.revision_count}`
                : ''}
            </span>
          ) : generating && brief ? (
            <span className="gp-badge">{liveCopy?.title ?? 'Drafting'}</span>
          ) : booting && brief ? (
            <span className="gp-badge gp-badge--muted">Restoring</span>
          ) : null}
        </header>

        <section className="gp-marketing__scope" aria-label="营销稿件选择">
          <label className="gp-field">
            团队
            <select
              aria-label="团队"
              value={scope.selectedTeamId}
              disabled={scope.teamLoading || generating}
              onChange={(event) => void scope.selectTeam(event.target.value)}
            >
              {!scope.teams.length ? <option value="">{scope.teamLoading ? '正在确认团队…' : '暂无可用团队'}</option> : null}
              {scope.teams.map((team) => <option key={team.team_id} value={team.team_id}>{team.team_name}</option>)}
            </select>
          </label>
          <label className="gp-field">
            营销稿件
            <select
              aria-label="营销稿件"
              value={brief?.id ?? ''}
              disabled={scope.teamLoading || scope.briefsLoading || generating || !scope.briefs.length}
              onChange={(event) => scope.selectBrief(event.target.value)}
            >
              {!brief ? <option value="">{scope.briefsLoading ? '正在读取稿件…' : '请选择营销稿件'}</option> : null}
              {scope.briefs.map((record) => <option key={record.id} value={record.id}>{record.goal}</option>)}
            </select>
          </label>
          <button
            type="button"
            className="gp-btn gp-btn--ghost gp-btn--sm"
            disabled={scope.teamLoading || scope.briefsLoading || generating || revisionBusy}
            onClick={() => teamId ? scope.refreshBriefs() : void scope.refreshTeams()}
          >刷新稿件</button>
        </section>

        {scope.error ? <p className="gp-marketing__error" role="alert">{scope.error}</p> : null}
        {scope.teamLoading || scope.briefsLoading ? <p role="status">正在读取当前团队的营销稿件…</p> : null}

        {error ? <p className="gp-marketing__error">{error}</p> : null}

        {brief ? (
          <MemoryCard
            key={`${teamId}:${brief.id}`}
            briefId={brief.id}
            teamId={teamId ?? undefined}
            feedbackPending={pendingAction === 'rejected'}
            refreshKey={`${workflow?.checkpoint_id ?? ''}:${reloadKey}`}
          />
        ) : null}

        {!brief && phase === 'idle' && !scope.teamLoading && !scope.briefsLoading && !scope.error ? (
          <div className="gp-empty">
            <p className="gp-empty__title">No brief on this desk</p>
            <p className="gp-empty__copy">
              Fill in research for a hardware line, then this page will generate
              social copy against the brand library.
            </p>
            <Link to="/research" className="gp-btn gp-btn--primary gp-btn--sm mt-5">
              Open research
            </Link>
          </div>
        ) : null}

        {brief && phase === 'error' ? (
          <form className="gp-empty" onSubmit={onRetry}>
            <p className="gp-empty__title">Couldn’t reach this run</p>
            <p className="gp-empty__copy">
              Check that the API is up, then retry. The brief itself is still saved.
            </p>
            <button type="submit" className="gp-btn gp-btn--primary gp-btn--sm mt-5">
              Retry
            </button>
          </form>
        ) : null}

        {brief && phase !== 'error' ? (
          <div className="gp-marketing__desk">
            <PipelineCard
              current={currentPipeline}
              status={status}
              generating={generating}
              booting={booting}
              percent={booting ? 0 : percent}
              elapsed={elapsed}
              liveTitle={liveCopy?.title ?? null}
              revisionCount={workflow?.revision_count ?? 0}
            />

            <section className="gp-marketing__brief" aria-label="Brief summary">
              <p className="gp-marketing__brief-goal">{brief.goal}</p>
              <div className="gp-marketing__chips">
                <span className="gp-marketing__chip">
                  {REGION_LABELS[brief.region]}
                </span>
                <span className="gp-marketing__chip">{brief.user_persona}</span>
                {platforms ? (
                  <span className="gp-marketing__chip">{platforms}</span>
                ) : null}
              </div>
            </section>

            {status === 'no_knowledge' && phase === 'ready' ? (
              <div className="gp-empty">
                <p className="gp-empty__title">Brand library came back empty</p>
                <p className="gp-empty__copy">
                  {workflow?.final_output ||
                    'Upload txt or PDF documents for this hardware line, then start a new brief.'}
                </p>
                <Link to="/assets" className="gp-btn gp-btn--primary gp-btn--sm mt-5">
                  Open assets
                </Link>
              </div>
            ) : null}

            {workflow?.research ? (
              <ResearchCard report={workflow.research} validation={workflow.research_validation} />
            ) : null}

            {showCopy ? (
              <article className="gp-panel gp-marketing__sheet">
                <h2 className="gp-panel__title">Channel copy</h2>
                <p className="gp-panel__meta">
                  {platforms
                    ? `Drafts for ${platforms}.`
                    : 'Per-platform hooks, body, and CTAs.'}
                </p>
                {writingCopy ? (
                  <div className="gp-marketing__skel" aria-hidden>
                    <span className="gp-skel" />
                    <span className="gp-skel" />
                    <span className="gp-skel" />
                    <span className="gp-skel gp-marketing__skel-short" />
                  </div>
                ) : (
                  <ChannelCopy
                    content={workflow?.content}
                    empty="Channel copy will land here once the run reaches this step."
                  />
                )}
              </article>
            ) : null}

            {workflow?.self_review && !generating ? (
              <SelfReviewCard report={workflow.self_review} />
            ) : null}

            {brief && teamId && userId && phase === 'ready' && hasMarketingCopy(workflow?.content) ? (
              <Revisions
                briefId={brief.id}
                brandId={brief.brand_id}
                teamId={teamId}
                userId={userId}
                platforms={Object.keys(workflow?.content?.platforms ?? {})}
                disabled={generating || booting}
                onBusyChange={setRevisionBusy}
                onAdopted={async () => {
                  const latest = await getMarketingWorkflow(brief.id, teamId)
                  if (!mounted.current) return
                  setWorkflow(latest)
                  setFeedback('')
                  setActiveNode(statusToNode(latest.status))
                  setNodePhase('done')
                }}
              />
            ) : null}
            {status === 'awaiting_review' && !workflow?.self_review && !generating ? (
              <p role="status">This saved draft has no self-review report. Request a revision so the workflow can re-run checks before approval.</p>
            ) : null}

            {status === 'completed' || status === 'max_revisions' || status === 'self_review_failed' ? (
              <p className="gp-marketing__lock" role="status">
                {status === 'completed'
                  ? 'Approved copy is saved. A targeted revision creates an updated draft for review.'
                  : status === 'self_review_failed'
                    ? 'Stopped after automatic self-review revisions. Use the blocker report above to see why.'
                    : 'Stopped at the revision cap. The last channel copy stays on the desk.'}
              </p>
            ) : null}

            {status === 'awaiting_review' ? (
              <form
                className="gp-panel gp-marketing__dock"
                onSubmit={(e) => {
                  e.preventDefault()
                  void onReview('rejected')
                }}
              >
                <div className="gp-marketing__dock-copy">
                  <p className="gp-marketing__card-kicker">Human review</p>
                  <h2 className="gp-panel__title">Send it back or lock it</h2>
                  <p className="gp-panel__meta">
                    Rejection needs notes. The next draft reuses retrieved brand
                    context plus whatever you write here.
                  </p>
                </div>
                <label
                  className={`gp-field${feedbackError ? ' is-invalid' : ''}`}
                >
                  Revision notes
                  <textarea
                    name="feedback"
                    value={feedback}
                    onChange={(e) => {
                      setFeedback(e.target.value)
                      if (feedbackError) setFeedbackError(null)
                    }}
                    placeholder="e.g. Drop the award claim, lead TikTok with the weekly pulse, keep LinkedIn more operator-to-operator."
                    disabled={pendingAction !== null || revisionBusy}
                    aria-invalid={feedbackError ? true : undefined}
                    aria-describedby={
                      feedbackError ? 'gp-feedback-error' : undefined
                    }
                  />
                  {feedbackError ? (
                    <span id="gp-feedback-error" className="gp-field__error" role="alert">
                      {feedbackError}
                    </span>
                  ) : null}
                </label>
                <div className="gp-marketing__review-actions">
                  <button
                    type="submit"
                    className="gp-btn gp-btn--danger"
                    disabled={pendingAction !== null || revisionBusy}
                  >
                    {pendingAction === 'rejected' ? 'Sending notes…' : 'Request revision'}
                  </button>
                  <button
                    type="button"
                    className="gp-btn gp-btn--primary"
                    disabled={
                      pendingAction !== null
                      || revisionBusy
                      || Boolean(feedback.trim())
                      || workflow?.self_review?.status !== 'passed'
                    }
                    onClick={() => void onReview('approved')}
                  >
                    {pendingAction === 'approved' ? 'Approving…' : 'Approve draft'}
                  </button>
                </div>
              </form>
            ) : null}
          </div>
        ) : null}
      </div>
    </main>
  )
}

function SelfReviewCard({ report }: { report: ContentReview }) {
  const failed = report.status !== 'passed'
  const failedFacts = report.facts.filter((fact) => fact.verdict !== 'supported')
  return (
    <article className={`gp-panel gp-marketing__card gp-marketing__audit${failed ? ' is-blocked' : ''}`}>
      <p className="gp-marketing__card-kicker">Model self-review</p>
      <h2 className="gp-panel__title">
        {failed ? 'Blocked before human review' : 'Brand facts passed'}
      </h2>
      <p className="gp-panel__meta">
        {failed
          ? report.summary || 'Self-review did not pass. The blockers below are what stopped the graph.'
          : 'Checked against retrieved brand sources. Platform format is validated in code. Human review is still required.'}
      </p>
      {failed ? (
        <>
          <p className="gp-marketing__blocker-line">
            Stuck at <strong>{report.stuck_at || 'self_review'}</strong>
            {report.attempts ? ` · attempt ${report.attempts}` : ''}
            {report.blockers?.length ? ` · ${report.blockers.join(', ')}` : ''}
          </p>
          {report.errors.length ? (
            <>
              <h3>Code checks</h3>
              <ul>{report.errors.map((error, i) => <li key={i}>{error}</li>)}</ul>
            </>
          ) : null}
        </>
      ) : null}
      <h3>Brand facts</h3>
      {(failed ? failedFacts : report.facts).length === 0 ? (
        <p>{failed ? 'No unsupported claims were listed.' : 'No factual claims were listed by the reviewer.'}</p>
      ) : (
        <ul>{(failed ? failedFacts : report.facts).map((fact, i) => (
          <li key={i}><strong>{fact.verdict}</strong>: {fact.claim}
            {fact.evidence && <blockquote>{fact.evidence}</blockquote>}
            {fact.suggestion && <p>{fact.suggestion}</p>}
          </li>
        ))}</ul>
      )}
    </article>
  )
}

function PipelineCard({
  current,
  status,
  generating,
  booting,
  percent,
  elapsed,
  liveTitle,
  revisionCount,
}: {
  current: PipelineId
  status: WorkflowStatus | undefined
  generating: boolean
  booting: boolean
  percent: number
  elapsed: number
  liveTitle: string | null
  revisionCount: number
}) {
  const rounded = Math.round(percent)
  const statusLine = booting
    ? 'Restoring this run'
    : generating
      ? (liveTitle ?? 'Running the graph')
      : status === 'awaiting_review'
        ? 'Paused for review'
        : status === 'completed'
          ? 'Run complete'
          : status === 'max_revisions'
            ? 'Stopped at the cap'
            : status === 'self_review_failed'
              ? 'Self-review could not pass'
              : status === 'no_knowledge'
                ? 'Knowledge miss'
                : 'Preparing the run'

  return (
    <section className="gp-pipeline" aria-label="Workflow progress">
      <ol className="gp-pipe">
        {PIPELINE_STEPS.map((step, index) => {
          const state = booting
            ? 'pending'
            : stepState(step.id, current, status, generating)
          return (
            <li key={step.id} className={`gp-pipe__step is-${state}`}>
              <div className="gp-pipe__track">
                {index > 0 ? (
                  <span
                    className={`gp-pipe__line${state === 'done' || (state === 'current' && !generating && status === 'completed') ? ' is-done' : state === 'current' ? ' is-current' : ''}`}
                    aria-hidden
                  />
                ) : null}
                <span className="gp-pipe__dot" aria-hidden>
                  {state === 'done' ? <CheckIcon /> : state === 'error' ? '!' : index + 1}
                </span>
              </div>
              <span className="gp-pipe__label">{step.label}</span>
              <span className="gp-pipe__hint">{step.hint}</span>
            </li>
          )
        })}
      </ol>

      <div
        className={`gp-meter${generating ? ' is-live' : ''}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={rounded}
        aria-label={statusLine}
      >
        <div className="gp-meter__fill" style={{ width: `${rounded}%` }} />
      </div>

      <div className="gp-pipeline__meta">
        <p className="gp-pipeline__now" aria-live="polite">
          <span className="gp-pipeline__now-label">{statusLine}</span>
          {generating ? <span className="gp-pipeline__pulse" aria-hidden /> : null}
        </p>
        <p className="gp-pipeline__stats">
          <span>{rounded}%</span>
          {generating ? <span>{formatElapsed(elapsed)}</span> : null}
          {revisionCount > 0 ? <span>Rev {revisionCount}</span> : null}
        </p>
      </div>
    </section>
  )
}

function CheckIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden>
      <path
        d="M2.2 6.2l2.4 2.4 5.2-5.4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default Marketing
