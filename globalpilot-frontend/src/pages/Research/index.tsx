import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'
import {
  SOCIAL_PLATFORMS,
  createBrief,
  type Region,
  type SocialPlatform,
} from '../../api/briefs'
import { persistLastBrief } from '../../api/marketing'
import { listHardwareBrands, type HardwareBrand } from '../../api/documents'
import { ApiError } from '../../api/http'
import type { TeamMembership } from '../../api/team'
import { useAuthStore } from '../../store/authStore'
import { useLiveTeamStore } from '../../workbench/liveTeamStore'

const MARKETS = [
  { value: 'eu-na', label: 'Europe & North America' },
  { value: 'sea', label: 'Southeast Asia' },
  { value: 'jp-kr', label: 'Japan & Korea' },
  { value: 'other', label: 'Other' },
] as const

const SOCIAL_LABELS: Record<SocialPlatform, string> = {
  tiktok: 'TikTok',
  instagram: 'Instagram',
  youtube: 'YouTube',
  linkedin: 'LinkedIn',
  facebook: 'Facebook',
  x: 'X',
}

type ResearchScope = {
  userId: string | null
  teamId: string | null
  teams: TeamMembership[]
  selectedTeamId: string
  teamLoading: boolean
  teamError: string
  onTeamChange: (teamId: string) => Promise<void>
  onRefreshTeams: () => Promise<void>
}

function currentResearchScope(userId: string | null, teamId: string | null) {
  const auth = useAuthStore.getState()
  const live = useLiveTeamStore.getState()
  return Boolean(userId && teamId && auth.isAuthenticated && auth.user?.id === userId
    && live.userId === userId && live.status === 'ready'
    && live.membership?.user_id === userId && live.membership.team_id === teamId
    && live.membership.status === 'active')
}

const Research = () => {
  const userId = useAuthStore((state) => state.isAuthenticated ? state.user?.id ?? null : null)
  const live = useLiveTeamStore()
  const refreshTeams = live.refreshTeams
  const identityMatches = live.userId === userId
  const teamId = identityMatches && live.status === 'ready' && live.membership?.user_id === userId
    ? live.membership?.team_id ?? null : null
  const teamLoading = Boolean(userId) && (!identityMatches || live.status === 'idle' || live.status === 'loading')
  const teamError = !userId ? 'Sign in to create a market brief.'
    : identityMatches && live.status === 'error' ? live.error
      : !teamLoading && !teamId ? 'No active team is available for this account.' : ''

  useEffect(() => {
    if (userId) void refreshTeams(userId)
  }, [userId, refreshTeams])

  return <ResearchForm
    key={`${userId ?? ''}:${teamId ?? ''}`}
    userId={userId}
    teamId={teamId}
    teams={identityMatches ? live.teams : []}
    selectedTeamId={identityMatches ? live.currentTeamId ?? '' : ''}
    teamLoading={teamLoading}
    teamError={teamError}
    onTeamChange={live.selectTeam}
    onRefreshTeams={() => userId ? refreshTeams(userId, true) : Promise.resolve()}
  />
}

const ResearchForm = ({ userId, teamId, teams, selectedTeamId, teamLoading, teamError, onTeamChange, onRefreshTeams }: ResearchScope) => {
  const navigate = useNavigate()
  const mounted = useRef(true)
  const submitting = useRef(false)
  const [brandRevision, setBrandRevision] = useState(0)
  const [brandResult, setBrandResult] = useState<{ revision: number; records: HardwareBrand[]; error: string }>({ revision: -1, records: [], error: '' })
  const [brandId, setBrandId] = useState('')
  const [goal, setGoal] = useState('')
  const [sellingPoints, setSellingPoints] = useState('')
  const [market, setMarket] = useState('')
  const [persona, setPersona] = useState('')
  const [socialMedia, setSocialMedia] = useState<SocialPlatform[]>([])
  const [channels, setChannels] = useState('')
  const [competitorUrl, setCompetitorUrl] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const brandsLoading = Boolean(teamId) && brandResult.revision !== brandRevision
  const brands = !brandsLoading ? brandResult.records : []
  const brandsError = !brandsLoading ? brandResult.error : ''

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    if (!teamId || !currentResearchScope(userId, teamId)) return
    let cancelled = false
    const controller = new AbortController()
    listHardwareBrands(teamId, controller.signal)
      .then((records) => {
        if (cancelled || !currentResearchScope(userId, teamId)) return
        if (records.some((record) => record.team_id !== teamId)) {
          throw new ApiError('Brand library does not belong to the current team. Reload the team to continue.', 403)
        }
        setBrandResult({ revision: brandRevision, records, error: '' })
        setBrandId((current) => records.some((record) => record.id === current) ? current : records[0]?.id || '')
      })
      .catch((err) => {
        if (!cancelled && currentResearchScope(userId, teamId)) {
          setBrandResult({ revision: brandRevision, records: [], error: err instanceof Error ? err.message : 'Unable to load brands.' })
        }
      })
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [userId, teamId, brandRevision])

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (
      submitting.current ||
      !teamId ||
      !currentResearchScope(userId, teamId) ||
      brandsLoading ||
      !brands.some((brand) => brand.id === brandId) ||
      !brandId ||
      !goal.trim() ||
      !sellingPoints.trim() ||
      !market ||
      !persona.trim() ||
      socialMedia.length === 0
    ) {
      return
    }
    submitting.current = true
    setError(null)
    setPending(true)
    try {
      const brief = await createBrief({
        brand_id: brandId,
        goal: goal.trim(),
        selling_points: sellingPoints.trim(),
        region: market as Region,
        user_persona: persona.trim(),
        social_media: socialMedia,
        purchase_channels: channels.trim() || null,
        competitor_url: competitorUrl.trim() || null,
      }, teamId)
      if (!mounted.current || !currentResearchScope(userId, teamId)) return
      if (brief.user_id !== userId || brief.team_id !== teamId || brief.brand_id !== brandId) {
        throw new ApiError('The saved brief does not match the current account, team or brand. Reload before continuing.', 403)
      }
      persistLastBrief(brief)
      navigate(`/marketing?brief_id=${encodeURIComponent(brief.id)}`, { state: { brief } })
    } catch (err) {
      if (mounted.current && currentResearchScope(userId, teamId)) {
        setError(err instanceof Error ? err.message : 'Unable to save brief.')
      }
    } finally {
      submitting.current = false
      if (mounted.current) setPending(false)
    }
  }

  return (
    <main className="gp-page">
      <div className="gp-page__inner gp-research">
        <header className="gp-page__head">
          <div>
            <p className="gp-kicker">Research</p>
            <h1 className="gp-page__title">Market brief</h1>
            <p className="gp-page__lede">
              Fill in the brief, then run research. The canvas picks up from here.
            </p>
          </div>
        </header>

        <label className="gp-research__label-block">
          <span className="gp-research__label">Team</span>
          <select name="teamId" value={selectedTeamId} disabled={pending || teamLoading || teams.length === 0} onChange={(event) => { void onTeamChange(event.target.value) }}>
            <option value="" disabled>{teamLoading ? 'Loading teams…' : 'Select a team'}</option>
            {teams.map((team) => <option key={team.team_id} value={team.team_id}>{team.team_name}</option>)}
          </select>
        </label>
        {teamLoading ? <p role="status" className="gp-research__hint">Checking team access…</p> : null}
        {teamError ? <p role="alert" className="gp-research__error">{teamError} <button type="button" className="gp-btn gp-btn--ghost" onClick={() => { void onRefreshTeams() }}>Retry team</button></p> : null}
        {brandsError ? <p role="alert" className="gp-research__error">{brandsError} <button type="button" className="gp-btn gp-btn--ghost" onClick={() => setBrandRevision((value) => value + 1)}>Retry brands</button></p> : null}
        {error ? <p role="alert" className="gp-research__error">{error}</p> : null}

        <form className="gp-panel gp-research__form gp-animate-fade-up" onSubmit={onSubmit}>
          <label className="gp-research__field">
            <span className="gp-research__num">01</span>
            <span className="gp-research__label-block">
              <span className="gp-research__label">Brand library</span>
              <span className="gp-research__hint">
                Which hardware line this brief should pull knowledge from.
              </span>
              <select
                name="brandId"
                value={brandId}
                onChange={(e) => setBrandId(e.target.value)}
                disabled={pending || !teamId || brandsLoading}
                required
              >
                <option value="" disabled>
                  {brandsLoading ? 'Loading brands…' : 'Select a brand'}
                </option>
                {brands.map((brand) => (
                  <option key={brand.id} value={brand.id}>
                    {brand.name}
                  </option>
                ))}
              </select>
              {teamId && !brandsLoading && !brandsError && brands.length === 0 ? <span className="gp-research__hint">No brand libraries are available in this team.</span> : null}
            </span>
          </label>

          <label className="gp-research__field">
            <span className="gp-research__num">02</span>
            <span className="gp-research__label-block">
              <span className="gp-research__label">Marketing goal</span>
              <span className="gp-research__hint">
                What this campaign should achieve.
              </span>
              <textarea
                name="goal"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="e.g. Enter the US SMB market with 20 qualified demos in Q4"
                required
              />
            </span>
          </label>

          <label className="gp-research__field">
            <span className="gp-research__num">03</span>
            <span className="gp-research__label-block">
              <span className="gp-research__label">Core selling points</span>
              <span className="gp-research__hint">The reasons to buy, in a few lines.</span>
              <textarea
                name="sellingPoints"
                className="gp-research__textarea--short"
                value={sellingPoints}
                onChange={(e) => setSellingPoints(e.target.value)}
                placeholder="e.g. Weekly exec pulse, three hours back for EMs, one source of truth across channels"
                required
              />
            </span>
          </label>

          <div className="gp-research__row">
            <label className="gp-research__field">
              <span className="gp-research__num">04</span>
              <span className="gp-research__label-block">
                <span className="gp-research__label">Target market</span>
                <span className="gp-research__hint">Region you want to enter or grow.</span>
                <select
                  name="market"
                  value={market}
                  onChange={(e) => setMarket(e.target.value)}
                  required
                >
                  <option value="" disabled>
                    Select a region
                  </option>
                  {MARKETS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </span>
            </label>

            <label className="gp-research__field">
              <span className="gp-research__num">05</span>
              <span className="gp-research__label-block">
                <span className="gp-research__label">User persona</span>
                <span className="gp-research__hint">Who you need to reach in that market.</span>
                <input
                  type="text"
                  name="persona"
                  value={persona}
                  onChange={(e) => setPersona(e.target.value)}
                  placeholder="e.g. Engineering managers at 50–200 person SaaS"
                  required
                />
              </span>
            </label>
          </div>

          <div className="gp-research__field">
            <span className="gp-research__num">06</span>
            <fieldset className="gp-research__label-block">
              <legend className="gp-research__label">Social platforms</legend>
              <span className="gp-research__hint">
                Pick every channel this campaign should publish to.
              </span>
              <div className="gp-research__chips">
                {SOCIAL_PLATFORMS.map((value) => (
                  <label key={value} className="gp-research__chip">
                    <input
                      type="checkbox"
                      name="socialMedia"
                      value={value}
                      checked={socialMedia.includes(value)}
                      onChange={() => {
                        setSocialMedia((current) =>
                          current.includes(value)
                            ? current.filter((platform) => platform !== value)
                            : [...current, value],
                        )
                      }}
                    />
                    {SOCIAL_LABELS[value]}
                  </label>
                ))}
              </div>
            </fieldset>
          </div>

          <div className="gp-research__row">
            <label className="gp-research__field">
              <span className="gp-research__num">07</span>
              <span className="gp-research__label-block">
                <span className="gp-research__label">
                  Purchase channels
                  <span className="gp-research__optional">Optional</span>
                </span>
                <span className="gp-research__hint">Where they buy or convert.</span>
                <input
                  type="text"
                  name="channels"
                  value={channels}
                  onChange={(e) => setChannels(e.target.value)}
                  placeholder="e.g. Shopify, Amazon, outbound demo"
                />
              </span>
            </label>

            <label className="gp-research__field">
              <span className="gp-research__num">08</span>
              <span className="gp-research__label-block">
                <span className="gp-research__label">
                  Competitor URL
                  <span className="gp-research__optional">Optional</span>
                </span>
                <span className="gp-research__hint">A site to read against.</span>
                <input
                  type="url"
                  name="competitorUrl"
                  value={competitorUrl}
                  onChange={(e) => setCompetitorUrl(e.target.value)}
                  placeholder="https://"
                />
              </span>
            </label>
          </div>

          <div className="gp-research__actions">
            <button
              type="submit"
              className="gp-btn gp-btn--primary"
              disabled={pending || !teamId || brandsLoading || !brands.some((brand) => brand.id === brandId) || socialMedia.length === 0}
            >
              {pending ? 'Saving…' : 'Run research'}
            </button>
          </div>
        </form>
      </div>
    </main>
  )
}

export default Research
