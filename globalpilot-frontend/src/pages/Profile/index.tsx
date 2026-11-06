import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import {
  listBriefs,
  type BriefListItem,
  type Region,
  type SocialPlatform,
} from '../../api/briefs'
import { persistLastBrief } from '../../api/marketing'
import { useAuth } from '../../store/authStore'
import { useLiveTeamStore } from '../../workbench/liveTeamStore'
import {
  profileMarketingRunUrl,
  profileMarketingTeam,
  verifyProfileMarketingRuns,
} from './marketingRuns'

type RunCollection = {
  scope: string
  revision: number
  records: BriefListItem[]
  error: string | null
}

const EMPTY_RUNS: BriefListItem[] = []

const SECTIONS = [
  { id: 'profile', label: 'Profile' },
  { id: 'workspace', label: 'Workspace' },
  { id: 'activity', label: 'Activity' },
  { id: 'session', label: 'Session' },
] as const

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

const RUN_STATUS: Record<
  BriefListItem['workflow_status'],
  { label: string; tone: 'review' | 'done' | 'muted' | 'warn' }
> = {
  awaiting_review: { label: 'In review', tone: 'review' },
  completed: { label: 'Approved', tone: 'done' },
  no_knowledge: { label: 'No knowledge', tone: 'muted' },
  max_revisions: { label: 'Revision cap', tone: 'warn' },
  self_review_failed: { label: 'Self-review failed', tone: 'warn' },
}

function formatWhen(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const diffMs = date.getTime() - Date.now()
  const abs = Math.abs(diffMs)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  if (abs < 60_000) return rtf.format(Math.round(diffMs / 1000), 'second')
  if (abs < 3_600_000) return rtf.format(Math.round(diffMs / 60_000), 'minute')
  if (abs < 86_400_000) return rtf.format(Math.round(diffMs / 3_600_000), 'hour')
  if (abs < 7 * 86_400_000) return rtf.format(Math.round(diffMs / 86_400_000), 'day')
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function uniqueInOrder<T>(values: T[]) {
  return [...new Set(values)]
}

const Profile = () => {
  const { user, updateProfile, logout } = useAuth()
  const live = useLiveTeamStore()
  const refreshTeams = live.refreshTeams
  const navigate = useNavigate()
  const nameScope = (user?.id ?? '') + ':' + (user?.name ?? '')
  const [nameDraft, setNameDraft] = useState({ scope: nameScope, value: user?.name ?? '' })
  const name = nameDraft.scope === nameScope ? nameDraft.value : (user?.name ?? '')
  const [saved, setSaved] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  const [signOutError, setSignOutError] = useState<string | null>(null)
  const [runsRevision, setRunsRevision] = useState(0)
  const [collection, setCollection] = useState<RunCollection>({
    scope: '', revision: -1, records: [], error: null,
  })
  const [activeSection, setActiveSection] = useState<string>('profile')

  const userId = user?.id ?? null
  const identityMatches = live.userId === userId
  const teamId = profileMarketingTeam(userId, live)
  const scope = userId && teamId ? userId + ':' + teamId : ''
  const currentRuns = Boolean(scope) && collection.scope === scope && collection.revision === runsRevision
  const teamLoading = !identityMatches || live.status === 'idle' || live.status === 'loading'
  const teams = identityMatches ? live.teams : []
  const runs = teamLoading || (Boolean(teamId) && !currentRuns) ? null : currentRuns ? collection.records : EMPTY_RUNS
  const runsError = identityMatches && live.status === 'error'
    ? live.error : currentRuns ? collection.error : null
  const initial = user?.name?.charAt(0)?.toUpperCase() || 'G'
  const dirty = name.trim() !== (user?.name ?? '') && name.trim().length > 0

  useEffect(() => {
    if (userId) void refreshTeams(userId)
  }, [userId, refreshTeams])

  useEffect(() => {
    if (!userId || !teamId) return
    let cancelled = false
    const controller = new AbortController()
    listBriefs(teamId, controller.signal)
      .then((records) => {
        if (!cancelled) setCollection({
          scope, revision: runsRevision,
          records: verifyProfileMarketingRuns(records, userId, teamId),
          error: null,
        })
      })
      .catch((err) => {
        if (!cancelled) setCollection({
          scope, revision: runsRevision, records: [],
          error: err instanceof Error ? err.message : 'Unable to load marketing runs.',
        })
      })
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [userId, teamId, scope, runsRevision])

  useEffect(() => {
    const nodes = SECTIONS.map((section) =>
      document.getElementById(section.id),
    ).filter((node): node is HTMLElement => Boolean(node))
    if (!nodes.length) return

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)
        const id = visible[0]?.target.id
        if (id) setActiveSection(id)
      },
      { rootMargin: '-18% 0px -62% 0px', threshold: [0.1, 0.35, 0.7] },
    )

    nodes.forEach((node) => observer.observe(node))
    return () => observer.disconnect()
  }, [])

  const markets = useMemo(
    () => (runs ? uniqueInOrder(runs.map((run) => REGION_LABELS[run.region])) : []),
    [runs],
  )
  const channels = useMemo(
    () =>
      runs
        ? uniqueInOrder(
            runs.flatMap((run) => run.social_media.map((item) => SOCIAL_LABELS[item])),
          )
        : [],
    [runs],
  )

  const openRun = (run: BriefListItem) => {
    if (!userId || !teamId || !currentRuns || !runs?.some(item => item.id === run.id)) return
    verifyProfileMarketingRuns([run], userId, teamId)
    persistLastBrief(run)
    navigate(profileMarketingRunUrl(run.id, teamId), { state: { brief: run } })
  }

  const onSave = (e: FormEvent) => {
    e.preventDefault()
    const next = name.trim()
    if (!next || next === user?.name) return
    updateProfile({ name: next })
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1600)
  }

  const onSignOut = async () => {
    setSigningOut(true)
    setSignOutError(null)
    try {
      await logout()
      navigate('/', { replace: true })
    } catch {
      setSignOutError('Unable to sign out. Please check your connection and try again.')
    } finally {
      setSigningOut(false)
    }
  }

  return (
    <main className="gp-page">
      <div className="gp-page__inner">
        <header className="gp-page__head">
          <div>
            <p className="gp-kicker">Account</p>
            <h1 className="gp-page__title">Your profile</h1>
            <p className="gp-page__lede">
              Identity, workspace defaults, and the marketing runs on this account.
            </p>
          </div>
        </header>

        <section className="gp-profile__hero" aria-label="Signed-in identity">
          <div className="gp-profile__avatar" aria-hidden>
            {initial}
          </div>
          <div className="gp-profile__hero-copy">
            <div className="gp-profile__hero-title-row">
              <h2 className="gp-profile__name">{user?.name}</h2>
              <span className="gp-badge">Pilot</span>
            </div>
            <p className="gp-profile__email">{user?.email}</p>
            <p className="gp-profile__hero-meta">
              {runsError
                ? 'Activity unavailable'
                : !teamLoading && !teamId
                  ? 'No active team'
                  : runs === null
                ? 'Loading activity…'
                : `${runs.length} marketing ${runs.length === 1 ? 'run' : 'runs'}`}
              <span aria-hidden> · </span>
              Signed in to this browser
            </p>
          </div>
        </section>

        <div className="gp-profile">
          <nav className="gp-profile__nav" aria-label="Account sections">
            {SECTIONS.map((section) => (
              <a
                key={section.id}
                href={`#${section.id}`}
                className={`gp-profile__nav-link${activeSection === section.id ? ' is-active' : ''}`}
              >
                {section.label}
              </a>
            ))}
          </nav>

          <div className="gp-profile__stack">
            <section id="profile" className="gp-profile-card">
              <header className="gp-profile-card__head">
                <h2 className="gp-profile-card__title">Personal information</h2>
                <p className="gp-profile-card__desc">
                  This name is shown in the workspace. Email is locked to the sign-in
                  account.
                </p>
              </header>

              <form onSubmit={onSave}>
                <div className="gp-profile-card__body">
                  <div className="gp-profile__fields">
                    <label className="gp-field">
                      <span>Display name</span>
                      <input
                        type="text"
                        value={name}
                        onChange={(e) => setNameDraft({ scope: nameScope, value: e.target.value })}
                        autoComplete="name"
                        required
                      />
                    </label>
                    <label className="gp-field gp-field--readonly">
                      <span className="gp-profile__email-label">
                        Email
                        <span className="gp-profile__lock">Locked</span>
                      </span>
                      <input type="email" value={user?.email ?? ''} readOnly />
                    </label>
                  </div>
                </div>
                <footer className="gp-profile-card__foot">
                  <p className="gp-profile-card__hint">
                    {saved
                      ? 'Saved on this device.'
                      : 'Display name changes apply to this session.'}
                  </p>
                  <button
                    type="submit"
                    className="gp-btn gp-btn--primary gp-btn--sm"
                    disabled={!dirty}
                  >
                    {saved ? 'Saved' : 'Save name'}
                  </button>
                </footer>
              </form>
            </section>

            <section id="workspace" className="gp-profile-card">
              <header className="gp-profile-card__head">
                <h2 className="gp-profile-card__title">Workspace</h2>
                <p className="gp-profile-card__desc">
                  Plan and the markets your briefs have used. Billing is not connected
                  yet.
                </p>
              </header>
              <div className="gp-profile-card__body">
                <div className="gp-profile__plan">
                  <div>
                    <p className="gp-profile__plan-kicker">Current plan</p>
                    <p className="gp-profile__plan-name">Pilot</p>
                    <p className="gp-profile__plan-copy">
                      Full research and marketing desk. Usage limits will land with
                      billing.
                    </p>
                  </div>
                  <span className="gp-badge gp-badge--muted">Shell</span>
                </div>

                <dl className="gp-profile__facts">
                  <div className="gp-profile__fact">
                    <dt>Markets</dt>
                    <dd>
                      {runsError ? (
                        <span className="gp-profile__empty-value">Unavailable</span>
                      ) : runs === null ? (
                        <span className="gp-skel gp-profile__skel-chip" />
                      ) : markets.length ? (
                        <ul className="gp-profile__chips">
                          {markets.map((market) => (
                            <li key={market}>{market}</li>
                          ))}
                        </ul>
                      ) : (
                        <span className="gp-profile__empty-value">
                          Set after your first brief
                        </span>
                      )}
                    </dd>
                  </div>
                  <div className="gp-profile__fact">
                    <dt>Channels</dt>
                    <dd>
                      {runsError ? (
                        <span className="gp-profile__empty-value">Unavailable</span>
                      ) : runs === null ? (
                        <span className="gp-skel gp-profile__skel-chip" />
                      ) : channels.length ? (
                        <ul className="gp-profile__chips">
                          {channels.map((channel) => (
                            <li key={channel}>{channel}</li>
                          ))}
                        </ul>
                      ) : (
                        <span className="gp-profile__empty-value">
                          Set after your first brief
                        </span>
                      )}
                    </dd>
                  </div>
                </dl>
              </div>
            </section>

            <section id="activity" className="gp-profile-card">
              <header className="gp-profile-card__head">
                <h2 className="gp-profile-card__title">Marketing runs</h2>
                <p className="gp-profile-card__desc">
                  Generated briefs stay on this account. Open one to restore the desk.
                </p>
                <label className="gp-field" style={{ marginTop: '1rem' }}>
                  <span>Marketing team</span>
                  <select
                    value={identityMatches ? live.currentTeamId ?? '' : ''}
                    disabled={teamLoading || !teams.length}
                    onChange={event => void live.selectTeam(event.target.value)}
                  >
                    {!teams.length ? <option value="">{teamLoading ? 'Checking teams…' : 'No active team'}</option> : null}
                    {teams.map(team => <option key={team.team_id} value={team.team_id}>{team.team_name}</option>)}
                  </select>
                </label>
              </header>
              <div className="gp-profile-card__body gp-profile-card__body--flush">
                {runsError ? (
                  <p className="gp-profile__alert" role="alert">
                    {runsError}
                    {' '}
                    <button type="button" className="gp-btn gp-btn--ghost gp-btn--sm" onClick={() => {
                      if (live.status === 'error' && userId) void refreshTeams(userId, true)
                      else setRunsRevision(value => value + 1)
                    }}>Retry runs</button>
                  </p>
                ) : null}
                {runs === null ? (
                  <div className="gp-profile__runs">
                    {[0, 1, 2].map((key) => (
                      <div key={key} className="gp-profile__run gp-profile__run--skel">
                        <span className="gp-skel gp-profile__skel-line" />
                        <span className="gp-skel gp-profile__skel-meta" />
                      </div>
                    ))}
                  </div>
                ) : !teamId && !runsError ? (
                  <div className="gp-profile__empty">
                    <p className="gp-profile__empty-title">No active team</p>
                    <p className="gp-profile__empty-copy">Join an active team to view its marketing runs.</p>
                  </div>
                ) : runs.length === 0 && !runsError ? (
                  <div className="gp-profile__empty">
                    <p className="gp-profile__empty-title">No saved runs yet</p>
                    <p className="gp-profile__empty-copy">
                      Start from research to generate a brief. It will show up here.
                    </p>
                    <Link to="/research" className="gp-btn gp-btn--primary gp-btn--sm">
                      Start research
                    </Link>
                  </div>
                ) : (
                  <div className="gp-profile__runs">
                    {runs.map((run) => {
                      const status = RUN_STATUS[run.workflow_status]
                      return (
                        <button
                          key={run.id}
                          type="button"
                          className="gp-profile__run"
                          onClick={() => openRun(run)}
                        >
                          <span className="gp-profile__run-main">
                            <span className="gp-profile__run-title">{run.goal}</span>
                            <span className="gp-profile__run-sub">
                              {REGION_LABELS[run.region]}
                              {run.revision_count > 0
                                ? ` · rev ${run.revision_count}`
                                : ''}
                            </span>
                          </span>
                          <span className="gp-profile__run-aside">
                            <span
                              className={`gp-profile__status gp-profile__status--${status.tone}`}
                            >
                              {status.label}
                            </span>
                            <time
                              className="gp-profile__run-when"
                              dateTime={run.updated_at}
                              title={new Date(run.updated_at).toLocaleString()}
                            >
                              {formatWhen(run.updated_at)}
                            </time>
                          </span>
                          <span className="gp-profile__run-chevron" aria-hidden>
                            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                              <path
                                d="M6 3.5L11 8l-5 4.5"
                                stroke="currentColor"
                                strokeWidth="1.5"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          </span>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            </section>

            <section id="session" className="gp-profile-card gp-profile-card--danger">
              {signOutError && <p role="alert">{signOutError}</p>}
              <header className="gp-profile-card__head">
                <h2 className="gp-profile-card__title">Sign out</h2>
                <p className="gp-profile-card__desc">
                  Signs you out of this browser. Your briefs
                  stay on the account.
                </p>
              </header>
              <footer className="gp-profile-card__foot">
                <p className="gp-profile-card__hint">
                  You can sign back in with the same email.
                </p>
                <button
                  type="button"
                  className="gp-btn gp-btn--danger gp-btn--sm"
                  disabled={signingOut}
                  onClick={() => void onSignOut()}
                >
                  {signingOut ? 'Signing out…' : 'Sign out'}
                </button>
              </footer>
            </section>
          </div>
        </div>
      </div>
    </main>
  )
}

export default Profile
