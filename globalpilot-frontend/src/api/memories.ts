import { apiJson } from './http'

export type BrandPreference = {
  memory_id: string
  content: string
  regions: string
  platforms: string[]
  version: number
  update_at: string
}

export type MemoryJobStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'needs_review'

export type MemoryJob = {
  id: string
  status: MemoryJobStatus
  summary: string | null
  error: string | null
  created_at: string
  updated_at: string
}

export type BriefMemories = {
  items: BrandPreference[]
  jobs: MemoryJob[]
}

export function getBriefMemories(briefId: string, signal?: AbortSignal, teamId?: string) {
  const scope = teamId ? `?team_id=${encodeURIComponent(teamId)}` : ''
  return apiJson<BriefMemories>(
    `/api/briefs/${encodeURIComponent(briefId)}/memories${scope}`,
    { signal },
  )
}
