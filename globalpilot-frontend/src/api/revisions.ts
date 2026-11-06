import { ApiError, apiJson } from './http'
import type { BrandDocument } from './documents'

export type DraftBlock = { block_id: string; text: string }
export type BlockLengthLimit = {
  block_id: string
  unit: 'unicode_chars' | 'whitespace_words'
  maximum: number
}
export type RevisionRequirements = {
  goal: string
  allowed_block_ids: string[]
  must_keep: string[]
  length_limits: BlockLengthLimit[]
}
export type DraftSnapshot = {
  draft_id: string
  version: number
  source_brief_id: string
  source_checkpoint_id: string
  platform: string
  blocks: DraftBlock[]
}
export type DraftCandidate = {
  candidate_id: string
  draft_id: string
  base_version: number
  blocks: DraftBlock[]
}
export type BrandEvidence = {
  evidence_id: string
  document_id: string
  document_version: number
  source: string
  quote: string
}
export type RevisionReviewContext = {
  rules_version: string
  brand_rules: string[]
  evidence: BrandEvidence[]
}
export type CriterionReview = {
  verdict: 'met' | 'unmet' | 'uncertain'
  note: string
}
export type FactReview = {
  claim: string
  verdict: 'supported' | 'unsupported' | 'contradicted' | 'uncertain'
  references: { evidence_id: string; quote: string }[]
  note: string
}
export type ModelSemanticReview = {
  goal: CriterionReview
  rules: (CriterionReview & { rule_index: number })[]
  blocks: { block_id: string; tone: CriterionReview; facts: FactReview[] }[]
  uncertainties: string[]
}
export type SemanticValidationResult = {
  status: 'passed' | 'needs_revision' | 'needs_clarification' | 'error'
  candidate_id: string
  input_hash: string
  review_version: string
  issues: string[]
  model_review: ModelSemanticReview | null
}
export type RevisionStatus =
  | 'created'
  | 'running'
  | 'needs_clarification'
  | 'awaiting_review'
  | 'adopting'
  | 'accepted'
  | 'rejected'
  | 'budget_exhausted'
  | 'error'

export type RevisionCreateRequest = {
  platform: string
  expected_draft_id: string
  expected_draft_version: number
  requirements: RevisionRequirements
  brand_rules?: string[]
  evidence_document_ids?: string[]
  max_revisions?: number
  idempotency_key: string
}
export type RevisionCommandInput = {
  expected_task_version: number
  expected_checkpoint_id: string | null
  idempotency_key: string
}
export type RevisionClarificationInput = RevisionCommandInput & {
  feedback: string
  goal?: string | null
  must_keep?: string[] | null
  length_limits?: BlockLengthLimit[] | null
  evidence_document_ids?: string[]
}
export type RevisionReviewInput = RevisionCommandInput & {
  action: 'approved' | 'rejected'
  feedback?: string | null
  blocks?: DraftBlock[] | null
}
export type RevisionTaskOutput = {
  task_id: string
  brief_id: string
  platform: string
  status: RevisionStatus
  version: number
  checkpoint_id: string | null
  draft: DraftSnapshot
  requirements: RevisionRequirements
  original_requirements: RevisionRequirements | null
  review_context: RevisionReviewContext | null
  candidate: DraftCandidate | null
  validation: SemanticValidationResult | null
  max_revisions: number
  revision_attempts: number
  stop_reason: string | null
  issues: string[]
  interrupt: Record<string, unknown> | null
  history: Record<string, unknown>[]
  adopted_version: number | null
  created_at: string
  updated_at: string
}
export type RevisionSample = {
  schema_version: string
  task_id: string
  brief_id: string
  platform: string
  original_draft: DraftSnapshot
  original_requirements: RevisionRequirements
  requirements: RevisionRequirements
  review_context: RevisionReviewContext
  model_candidate: DraftCandidate | null
  accepted_candidate: DraftCandidate
  validation: SemanticValidationResult
  human_review: {
    actor_user_id: string
    action: 'approved'
    feedback: string
    edited: boolean
    at: string
  }
  attempt_history: Record<string, unknown>[]
  clarification_history: Record<string, unknown>[]
  history: Record<string, unknown>[]
  source_checkpoint_id: string
  adopted_checkpoint_id: string
  adopted_version: number
}

function requiredId(value: string, label: string) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new ApiError(`缺少${label}，请重新加载页面。`, 400)
  }
  return encodeURIComponent(value)
}

function scopedPath(path: string, teamId: string, extra?: Record<string, string>) {
  requiredId(teamId, '团队信息')
  return `${path}?${new URLSearchParams({ team_id: teamId, ...extra }).toString()}`
}

function taskPath(taskId: string, suffix = '') {
  return `/api/revision-tasks/${requiredId(taskId, '改稿任务')}${suffix}`
}

function briefPath(briefId: string) {
  return `/api/briefs/${requiredId(briefId, '营销任务')}/revisions`
}

function commandBody(payload: RevisionCommandInput): RevisionCommandInput {
  return {
    expected_task_version: payload.expected_task_version,
    expected_checkpoint_id: payload.expected_checkpoint_id,
    idempotency_key: payload.idempotency_key,
  }
}

function lengthLimits(limits: BlockLengthLimit[]) {
  return limits.map(({ block_id, unit, maximum }) => ({ block_id, unit, maximum }))
}

export function readRevisionDocument(briefId: string, platform: string, teamId: string, signal?: AbortSignal) {
  requiredId(platform, '平台')
  return apiJson<DraftSnapshot>(scopedPath(`${briefPath(briefId)}/document`, teamId, { platform }), { signal })
}

export function listRevisionTasks(briefId: string, teamId: string, signal?: AbortSignal) {
  return apiJson<RevisionTaskOutput[]>(scopedPath(briefPath(briefId), teamId), { signal })
}

export function listRevisionEvidenceDocuments(brandId: string, teamId: string, signal?: AbortSignal) {
  requiredId(brandId, '品牌')
  return apiJson<BrandDocument[]>(scopedPath('/api/documents', teamId, { brand_id: brandId }), { signal })
}

export function getRevisionTask(taskId: string, teamId: string, signal?: AbortSignal) {
  return apiJson<RevisionTaskOutput>(scopedPath(taskPath(taskId), teamId), { signal })
}

export function createRevisionTask(briefId: string, payload: RevisionCreateRequest, teamId: string, signal?: AbortSignal) {
  const { requirements } = payload
  return apiJson<RevisionTaskOutput>(scopedPath(briefPath(briefId), teamId), {
    method: 'POST', signal,
    body: JSON.stringify({
      platform: payload.platform,
      expected_draft_id: payload.expected_draft_id,
      expected_draft_version: payload.expected_draft_version,
      requirements: {
        goal: requirements.goal,
        allowed_block_ids: [...requirements.allowed_block_ids],
        must_keep: [...requirements.must_keep],
        length_limits: lengthLimits(requirements.length_limits),
      },
      brand_rules: payload.brand_rules,
      evidence_document_ids: payload.evidence_document_ids,
      max_revisions: payload.max_revisions,
      idempotency_key: payload.idempotency_key,
    }),
  })
}

export function runRevisionTask(taskId: string, payload: RevisionCommandInput, teamId: string, signal?: AbortSignal) {
  return apiJson<RevisionTaskOutput>(scopedPath(taskPath(taskId, '/run'), teamId), {
    method: 'POST', body: JSON.stringify(commandBody(payload)), signal,
  })
}

export function clarifyRevisionTask(taskId: string, payload: RevisionClarificationInput, teamId: string, signal?: AbortSignal) {
  return apiJson<RevisionTaskOutput>(scopedPath(taskPath(taskId, '/clarification'), teamId), {
    method: 'POST', signal,
    body: JSON.stringify({
      ...commandBody(payload), feedback: payload.feedback, goal: payload.goal,
      must_keep: payload.must_keep,
      length_limits: payload.length_limits ? lengthLimits(payload.length_limits) : payload.length_limits,
      evidence_document_ids: payload.evidence_document_ids,
    }),
  })
}

export function reviewRevisionTask(taskId: string, payload: RevisionReviewInput, teamId: string, signal?: AbortSignal) {
  return apiJson<RevisionTaskOutput>(scopedPath(taskPath(taskId, '/review'), teamId), {
    method: 'POST', signal,
    body: JSON.stringify({
      ...commandBody(payload), action: payload.action, feedback: payload.feedback,
      blocks: payload.blocks?.map(({ block_id, text }) => ({ block_id, text })) ?? payload.blocks,
    }),
  })
}

export function exportRevisionSample(taskId: string, teamId: string, signal?: AbortSignal) {
  return apiJson<RevisionSample>(scopedPath(taskPath(taskId, '/sample'), teamId), { signal })
}
