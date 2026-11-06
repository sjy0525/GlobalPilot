import type { GraphNode, WorkflowStatus } from '../../api/marketing'

export type PipelineId = 'knowledge' | 'strategy' | 'copy' | 'self_review' | 'review' | 'deliver'
export type NodePhase = 'running' | 'done'

export const PIPELINE_STEPS: {
  id: PipelineId
  label: string
  hint: string
}[] = [
  { id: 'knowledge', label: 'Knowledge', hint: 'Retrieve facts and research' },
  { id: 'strategy', label: 'Strategy', hint: 'Draft the market plan' },
  { id: 'copy', label: 'Copy', hint: 'Write channel drafts' },
  { id: 'self_review', label: 'Self-review', hint: 'Check brand facts' },
  { id: 'review', label: 'Review', hint: 'Approve or send notes' },
  { id: 'deliver', label: 'Deliver', hint: 'Lock the final run' },
]

export const NODE_COPY: Record<GraphNode, { title: string; detail: string }> = {
  retrieve_context: {
    title: 'Retrieving brand knowledge',
    detail: 'Searching the hardware-line library for facts that can go into the plan.',
  },
  no_context: {
    title: 'No matching knowledge',
    detail: 'The brand library did not return usable context for this brief.',
  },
  research_agent: {
    title: 'Researching the market',
    detail: 'Checking what needs external evidence, searching when needed, and reviewing sources before strategy.',
  },
  generate_strategy: {
    title: 'Writing the market plan',
    detail: 'Positioning, pillars, and channel strategy from retrieved facts.',
  },
  generate_content: {
    title: 'Drafting channel copy',
    detail: 'Hooks, body, and CTAs for each selected platform.',
  },
  self_review: {
    title: 'Checking brand facts',
    detail: 'Reviewing each claim against brand sources. Platform format is already checked in code. Failed drafts revise automatically before human review.',
  },
  human_review: {
    title: 'Waiting for your review',
    detail: 'Approve to lock this run, or send notes for another draft.',
  },
  final_output: {
    title: 'Locking the approved run',
    detail: 'Saving strategy and copy as the final snapshot.',
  },
  max_revisions: {
    title: 'Revision cap reached',
    detail: 'The last draft stays on the desk.',
  },
  self_review_exhausted: {
    title: 'Self-review could not pass',
    detail: 'Automatic revisions hit the limit. The last draft stays on the desk.',
  },
}

const NODE_TO_PIPELINE: Record<GraphNode, PipelineId> = {
  retrieve_context: 'knowledge',
  no_context: 'knowledge',
  research_agent: 'knowledge',
  generate_strategy: 'strategy',
  generate_content: 'copy',
  self_review: 'self_review',
  human_review: 'review',
  final_output: 'deliver',
  max_revisions: 'deliver',
  self_review_exhausted: 'deliver',
}

const STATUS_TO_NODE: Record<WorkflowStatus, GraphNode> = {
  awaiting_review: 'human_review',
  completed: 'final_output',
  max_revisions: 'max_revisions',
  self_review_failed: 'self_review_exhausted',
  no_knowledge: 'no_context',
}

const NODE_METER: Record<GraphNode, { running: number; done: number }> = {
  retrieve_context: { running: 8, done: 14 },
  no_context: { running: 16, done: 20 },
  research_agent: { running: 16, done: 24 },
  generate_strategy: { running: 26, done: 48 },
  generate_content: { running: 52, done: 76 },
  self_review: { running: 77, done: 81 },
  human_review: { running: 82, done: 86 },
  final_output: { running: 92, done: 100 },
  max_revisions: { running: 92, done: 100 },
  self_review_exhausted: { running: 92, done: 100 },
}

export const FALLBACK_SEQUENCE: GraphNode[] = [
  'retrieve_context',
  'research_agent',
  'generate_strategy',
  'generate_content',
  'self_review',
]

export function nodeToPipeline(node: GraphNode): PipelineId {
  return NODE_TO_PIPELINE[node]
}

export function statusToNode(status: WorkflowStatus): GraphNode {
  return STATUS_TO_NODE[status]
}

export function pipelineIndex(id: PipelineId): number {
  return PIPELINE_STEPS.findIndex((step) => step.id === id)
}

export function stepState(
  id: PipelineId,
  current: PipelineId,
  status: WorkflowStatus | undefined,
  generating: boolean,
): 'done' | 'current' | 'pending' | 'error' {
  if (id === 'knowledge' && status === 'no_knowledge') return 'error'
  if (id === 'self_review' && status === 'self_review_failed') return 'error'
  const currentIndex = pipelineIndex(current)
  const index = pipelineIndex(id)
  if (index < currentIndex) return 'done'
  if (index === currentIndex) {
    if (!generating && (status === 'completed' || status === 'max_revisions' || status === 'self_review_failed')) {
      return 'done'
    }
    return 'current'
  }
  return 'pending'
}

export function meterTarget(
  node: GraphNode | null,
  phase: NodePhase,
  status: WorkflowStatus | undefined,
  generating: boolean,
): number {
  if (!generating && status === 'completed') return 100
  if (!generating && status === 'max_revisions') return 100
  if (!generating && status === 'self_review_failed') return 100
  if (!generating && status === 'awaiting_review') return 84
  if (status === 'no_knowledge') return 20
  if (!node) return generating ? 8 : 0
  return phase === 'running' || generating ? NODE_METER[node].running : NODE_METER[node].done
}
