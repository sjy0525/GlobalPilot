import { useEffect, useState } from 'react'
import {
  getBriefMemories,
  type BriefMemories,
  type MemoryJobStatus,
} from '../../api/memories'

const JOB_LABELS: Record<MemoryJobStatus, string> = {
  pending: 'Queued',
  running: 'Checking preferences',
  completed: 'Checked',
  failed: 'Processing failed',
  needs_review: 'Needs review',
}

const REGION_LABELS: Record<string, string> = {
  'eu-na': 'Europe & North America',
  sea: 'Southeast Asia',
  'jp-kr': 'Japan & Korea',
  other: 'Other',
}

const PLATFORM_LABELS: Record<string, string> = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
  youtube: 'YouTube',
  linkedin: 'LinkedIn',
  facebook: 'Facebook',
  x: 'X',
}

export function MemoryCard({
  briefId,
  teamId,
  feedbackPending,
  refreshKey,
}: {
  briefId: string
  teamId?: string
  feedbackPending: boolean
  refreshKey: string
}) {
  const [snapshot, setSnapshot] = useState<BriefMemories | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retryKey, setRetryKey] = useState(0)
  const [submission, setSubmission] = useState<{
    pending: boolean
    priorJobIds: string[] | null
  }>({ pending: feedbackPending, priorJobIds: null })

  if (submission.pending !== feedbackPending) {
    setSubmission({
      pending: feedbackPending,
      priorJobIds: snapshot?.jobs.map((job) => job.id) ?? null,
    })
  }

  useEffect(() => {
    let cancelled = false
    let timer = 0
    const controller = new AbortController()

    const refresh = async () => {
      try {
        const result = await getBriefMemories(briefId, controller.signal, teamId)
        if (cancelled) return
        setSnapshot(result)
        setError(null)
        const hasActiveJob = result.jobs.some(
          (job) => job.status === 'pending' || job.status === 'running',
        )
        if (hasActiveJob || feedbackPending) {
          timer = window.setTimeout(() => void refresh(), 2000)
        }
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Unable to load brand preferences.')
      }
    }

    void refresh()
    return () => {
      cancelled = true
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [briefId, teamId, feedbackPending, refreshKey, retryKey])

  const latestJob = snapshot?.jobs.toSorted((left, right) =>
    new Date(right.created_at).getTime() - new Date(left.created_at).getTime(),
  )[0]
  const active = latestJob?.status === 'pending' || latestJob?.status === 'running'
  const attention = latestJob?.status === 'failed' || latestJob?.status === 'needs_review'
  const priorJobIds = submission.priorJobIds
  const waitingForCurrentJob = feedbackPending && (
    priorJobIds === null
      ? !active
      : !snapshot?.jobs.some((job) => !priorJobIds.includes(job.id))
  )

  return (
    <section className="gp-panel gp-marketing__memories" aria-label="Brand preferences">
      <div className="gp-marketing__memory-heading">
        <div>
          <p className="gp-marketing__card-kicker">Automatic memory</p>
          <h2 className="gp-panel__title">Brand preferences</h2>
        </div>
        {latestJob && !waitingForCurrentJob ? (
          <span className={`gp-badge${attention ? ' gp-badge--warn' : active ? '' : ' gp-badge--muted'}`}>
            {JOB_LABELS[latestJob.status]}
          </span>
        ) : null}
      </div>
      <p className="gp-panel__meta">
        Preferences are private to your account and shared across briefs in the selected product category.
        Revision notes are checked automatically for lasting preferences.
      </p>

      {error ? (
        <div className="gp-marketing__memory-error" role="alert">
          <p>Unable to refresh saved preferences. {error}</p>
          <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" onClick={() => setRetryKey((key) => key + 1)}>
            Retry preferences
          </button>
        </div>
      ) : null}

      <div role="status" aria-live="polite" aria-atomic="true">
        {!snapshot && !error ? <p className="gp-panel__meta">Loading saved preferences…</p> : null}
        {waitingForCurrentJob ? (
          <p className="gp-marketing__memory-status">
            This feedback will be checked automatically once accepted.
          </p>
        ) : latestJob ? (
          <>
            <p className={`gp-marketing__memory-status${attention ? ' is-attention' : ''}`}>
              {latestJob.summary || (latestJob.status === 'pending'
                ? 'Your feedback is waiting to be checked.'
                : latestJob.status === 'running'
                  ? 'Checking whether your feedback contains a lasting preference…'
                  : latestJob.status === 'failed'
                    ? 'The preference check failed. No further changes will be attempted automatically.'
                    : latestJob.status === 'needs_review'
                      ? 'The preference check needs review before another attempt.'
                      : 'The preference check is complete.')}
            </p>
            {attention && latestJob.error?.trim() ? (
              <p className="gp-marketing__memory-status is-attention">{latestJob.error}</p>
            ) : null}
          </>
        ) : null}
      </div>

      {snapshot?.items.length ? (
        <ul className="gp-marketing__memory-list">
          {snapshot.items.map((memory) => (
            <li key={memory.memory_id} className="gp-marketing__memory-item">
              <p className="gp-marketing__memory-content">{memory.content}</p>
              <p className="gp-marketing__memory-scope">
                {REGION_LABELS[memory.regions] || memory.regions}
                {' · '}
                {memory.platforms.length
                  ? memory.platforms.map((platform) => PLATFORM_LABELS[platform] || platform).join(', ')
                  : 'All platforms'}
                {' · '}v{memory.version}
              </p>
            </li>
          ))}
        </ul>
      ) : snapshot ? (
        <p className="gp-marketing__memory-empty">No long-term preferences saved for this product category yet.</p>
      ) : null}
    </section>
  )
}
