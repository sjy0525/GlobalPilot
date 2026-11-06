import { apiJson } from './http'

export type Verdict = 'pass' | 'fail' | 'pending'

export type ScoreCell = {
  verdict: Verdict
  reason: string
  source: 'code' | 'model' | 'human'
}

export type EvalCase = {
  id: string
  code: string
  ordinal: number
  title: string
  user_request: string
  brand_profile: string
  brand_name: string
  platforms: string[]
  must_include: string[]
  forbidden_claims: string[]
  region: string
  persona: string
  purchase_channels: string | null
}

export type EvalTestSet = {
  id: string
  version: string
  name: string
  description: string
  cases: EvalCase[]
}

export type Provenance = {
  test_set_version: string
  workflow_version: string
  prompt_version: string
  prompt_fingerprint: string
  model_settings: {
    generator?: { model?: string; temperature?: number }
    judge?: { model?: string }
    embedding?: { model?: string }
    rerank?: { model?: string }
    price_card?: string
    cost_scope?: string
    execution_mode?: string
  }
  knowledge_version: string
  scoring_rules_version: string
  scoring_fingerprint: string
  criteria?: string[]
  pass_rule?: string
  rate_formula?: string
}

export type RunSummary = {
  total: number
  first_draft_passed: number
  first_draft_pass_rate: number | null
  qualified_count: number
  fact_error_count: number
  fact_pending_count: number
  empty_count: number
  timeout_count: number
  error_count: number
  duration_ms_total: number
  duration_ms_avg: number | null
  api_cost_usd_total: number
  cost_per_qualified_usd: number | null
}

export type EvalRun = {
  id: string
  label: string
  status: 'queued' | 'running' | 'completed' | 'failed' | string
  is_baseline: boolean
  provenance: Provenance
  summary: RunSummary | null
  report_markdown: string | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  task_total: number
  task_done: number
}

export type EvalTask = {
  id: string
  case_id: string
  code: string
  title: string
  user_request: string
  brand_profile: string
  brand_name: string
  platforms: string[]
  must_include: string[]
  forbidden_claims: string[]
  execution_status: string
  empty_output: boolean
  timed_out: boolean
  revision_count: number
  qualified: boolean
  generated_content: { platforms?: Record<string, { hook?: string; body?: string; cta?: string; tags?: string }> } | null
  strategy: string | null
  retrieved_context: string | null
  duration_ms: number | null
  api_cost_usd: number | null
  judge_cost_usd: number | null
  error: string | null
  scores: Partial<Record<'facts' | 'completeness' | 'tone' | 'format', ScoreCell>> | null
  auto_scores: Partial<Record<'facts' | 'completeness' | 'tone' | 'format', ScoreCell>> | null
  human_note: string | null
  failure_reasons: string[]
}

export type CompareSide = {
  qualified: boolean
  reasons: string[]
  facts_verdict: Verdict | null
  duration_ms: number | null
  api_cost_usd: number | null
  empty_output: boolean
  timed_out: boolean
  execution_status: string
}

export type EvalCompare = {
  left: EvalRun
  right: EvalRun
  rows: { code: string; title: string; left: CompareSide; right: CompareSide }[]
  comparison_markdown: string
}

export function getEvalSnapshot() {
  return apiJson<Provenance>('/api/evals/snapshot')
}

export function getEvalTestSet() {
  return apiJson<EvalTestSet>('/api/evals/testset')
}

export function listEvalRuns() {
  return apiJson<EvalRun[]>('/api/evals/runs')
}

export function startEvalRun(label: string, workflowVersion: string) {
  return apiJson<EvalRun>('/api/evals/runs', {
    method: 'POST',
    body: JSON.stringify({
      label: label.trim() || null,
      workflow_version: workflowVersion,
    }),
  })
}

export function listEvalTasks(runId: string) {
  return apiJson<EvalTask[]>(`/api/evals/runs/${runId}/tasks`)
}

export function markEvalBaseline(runId: string) {
  return apiJson<EvalRun>(`/api/evals/runs/${runId}/baseline`, { method: 'POST' })
}

export function rebuildEvalReport(runId: string) {
  return apiJson<EvalRun>(`/api/evals/runs/${runId}/report`, { method: 'POST' })
}

export function reviewEvalTask(
  taskId: string,
  body: {
    facts?: Verdict
    completeness?: Verdict
    tone?: Verdict
    format?: Verdict
    note: string
  },
) {
  return apiJson<EvalTask>(`/api/evals/tasks/${taskId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function compareEvalRuns(left: string, right: string) {
  const query = new URLSearchParams({ left, right })
  return apiJson<EvalCompare>(`/api/evals/compare?${query}`)
}
