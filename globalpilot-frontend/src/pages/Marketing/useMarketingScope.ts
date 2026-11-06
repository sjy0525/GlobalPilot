import { useEffect, useState } from 'react'
import { listBriefs, type Brief } from '../../api/briefs'
import { ApiError } from '../../api/http'
import { persistLastBrief, readLastBrief } from '../../api/marketing'
import { useAuthStore } from '../../store/authStore'
import { useLiveTeamStore } from '../../workbench/liveTeamStore'

type BriefCollection = {
  scope: string
  revision: number
  records: Brief[]
  selectedId: string
  error: string
}

function selectionKey(userId: string, teamId: string) {
  return `gp-marketing-brief:${userId}:${teamId}`
}

function savedSelection(userId: string, teamId: string) {
  try { return window.sessionStorage.getItem(selectionKey(userId, teamId)) } catch { return null }
}

export function resolveMarketingBriefs(
  records: Brief[],
  { userId, teamId, requestedId, savedId, locationBrief, rememberedBrief }: {
    userId: string
    teamId: string
    requestedId: string | null
    savedId: string | null
    locationBrief?: Brief
    rememberedBrief?: Brief | null
  },
) {
  if (records.some((record) => record.user_id !== userId || record.team_id !== teamId)) {
    throw new ApiError('稿件列表与当前账号或团队不一致，请重新加载。', 403)
  }
  const matchesScope = (brief: Brief | null | undefined) =>
    brief?.user_id === userId && brief.team_id === teamId ? brief : null
  const explicit = matchesScope(locationBrief)
  const remembered = matchesScope(rememberedBrief)
  const available = [...records]
  // Newly created briefs are not in the saved-run list until execution starts.
  // Receipts only fill that gap for this actor/team; the workflow API still
  // authorizes every read and execution.
  for (const receipt of [remembered, explicit]) {
    if (receipt && !available.some((record) => record.id === receipt.id)) available.unshift(receipt)
  }
  const wanted = requestedId ?? explicit?.id ?? savedId ?? remembered?.id
  const selected = available.find((record) => record.id === wanted)
    ?? (requestedId ? null : available[0])
  return {
    records: available,
    selectedId: selected?.id ?? '',
    error: requestedId && !selected ? '当前团队中未找到这份营销稿件，或你没有访问权限。' : '',
  }
}

export function useMarketingScope(locationBrief: Brief | undefined, search: string) {
  const userId = useAuthStore((state) => state.user?.id ?? null)
  const live = useLiveTeamStore()
  const refreshTeams = live.refreshTeams
  const [rememberedBrief] = useState(readLastBrief)
  const [revision, setRevision] = useState(0)
  const [collection, setCollection] = useState<BriefCollection>({
    scope: '', revision: -1, records: [], selectedId: '', error: '',
  })
  const identityMatches = live.userId === userId
  const teamId = identityMatches && live.status === 'ready'
    ? live.membership?.team_id ?? null : null
  const scope = userId && teamId ? `${userId}:${teamId}` : ''
  const requestedId = new URLSearchParams(search).get('brief_id')

  useEffect(() => {
    if (userId) void refreshTeams(userId)
  }, [userId, refreshTeams])

  useEffect(() => {
    if (!userId || !teamId) return
    let cancelled = false
    const controller = new AbortController()
    void (async () => {
      try {
        const records: Brief[] = await listBriefs(teamId, controller.signal)
        if (cancelled) return
        const selection = resolveMarketingBriefs(records, {
          userId, teamId, requestedId, savedId: savedSelection(userId, teamId),
          locationBrief, rememberedBrief,
        })
        setCollection({ scope, revision, ...selection })
        const selected = selection.records.find((record) => record.id === selection.selectedId)
        if (selected) persistLastBrief(selected)
      } catch (error) {
        if (!cancelled) setCollection({
          scope, revision, records: [], selectedId: '',
          error: error instanceof Error ? error.message : '无法读取营销稿件。',
        })
      }
    })()
    return () => { cancelled = true; controller.abort() }
  }, [userId, teamId, scope, revision, locationBrief, rememberedBrief, requestedId])

  const current = Boolean(scope) && collection.scope === scope && collection.revision === revision
  const briefs = current ? collection.records : []
  const brief = briefs.find((record) => record.id === collection.selectedId) ?? null
  const teams = identityMatches ? live.teams : []
  const teamLoading = !identityMatches || live.status === 'idle' || live.status === 'loading'
  const briefsLoading = Boolean(teamId) && !current
  const error = identityMatches && live.status === 'error'
    ? live.error : current ? collection.error : ''

  const selectBrief = (briefId: string) => {
    const selected = briefs.find((record) => record.id === briefId)
    if (!selected || !userId || !teamId) return
    setCollection((value) => ({ ...value, selectedId: briefId, error: '' }))
    persistLastBrief(selected)
    try { window.sessionStorage.setItem(selectionKey(userId, teamId), briefId) } catch { /* Navigation remains available in memory. */ }
  }

  return {
    userId, teamId, brief, briefs, teams, teamLoading, briefsLoading, error,
    selectedTeamId: identityMatches ? live.currentTeamId ?? '' : '',
    selectTeam: live.selectTeam,
    selectBrief,
    refreshBriefs: () => setRevision((value) => value + 1),
    refreshTeams: () => userId ? refreshTeams(userId, true) : Promise.resolve(),
  }
}

export type MarketingScope = ReturnType<typeof useMarketingScope>
