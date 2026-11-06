import type {
  DraftBlock,
  RevisionClarificationInput,
  RevisionCommandInput,
  RevisionCreateRequest,
  RevisionReviewInput,
  RevisionTaskOutput,
} from '../api/revisions'

export type RevisionOperationScope = { userId: string; teamId: string; briefId: string }
export type RevisionPendingOperation =
  | { kind: 'create'; payload: RevisionCreateRequest }
  | { kind: 'run'; task_id: string; payload: RevisionCommandInput }
  | { kind: 'clarification'; task_id: string; payload: RevisionClarificationInput }
  | { kind: 'review'; task_id: string; payload: RevisionReviewInput }

export type RevisionOperationStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

const STATUS_LABELS: Record<string, string> = {
  created: '待运行', running: '执行中', needs_clarification: '等待补充',
  awaiting_review: '待审核', adopting: '正在采纳', accepted: '已采纳',
  rejected: '已拒绝', budget_exhausted: '已达修订上限', error: '执行失败',
}

export function revisionStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? `未知状态：${status}`
}

export function newRevisionKey(): string {
  if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') {
    throw new Error('当前浏览器无法生成安全的请求标识，请使用本地地址或 HTTPS。')
  }
  return crypto.randomUUID()
}

export function revisionCommand(
  task: Pick<RevisionTaskOutput, 'version' | 'checkpoint_id'>,
  key: string,
): RevisionCommandInput {
  if (!Number.isSafeInteger(task.version) || task.version < 1 || !key.trim()) {
    throw new Error('任务版本或请求标识无效，请刷新任务。')
  }
  return {
    expected_task_version: task.version,
    expected_checkpoint_id: task.checkpoint_id,
    idempotency_key: key,
  }
}

function operationKey(scope: RevisionOperationScope): string {
  if (![scope.userId, scope.teamId, scope.briefId].every(value => typeof value === 'string' && value.trim())) {
    throw new Error('改稿恢复信息缺少账号、团队或营销任务。')
  }
  return 'gp-revision-operation:v1:' + [scope.userId, scope.teamId, scope.briefId].map(encodeURIComponent).join(':')
}

function operationStorage(storage?: RevisionOperationStorage): RevisionOperationStorage {
  if (storage) return storage
  if (typeof window === 'undefined') throw new Error('无法保存改稿恢复信息。')
  return window.localStorage
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function nonblank(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value.trim())
}

function validOperation(value: unknown): value is RevisionPendingOperation {
  if (!isRecord(value) || !isRecord(value.payload) || !nonblank(value.payload.idempotency_key)) return false
  const payload = value.payload
  if (value.kind === 'create') {
    return nonblank(payload.platform) && nonblank(payload.expected_draft_id) &&
      typeof payload.expected_draft_version === 'number' && Number.isSafeInteger(payload.expected_draft_version) && payload.expected_draft_version > 0 &&
      isRecord(payload.requirements) && nonblank(payload.requirements.goal) &&
      Array.isArray(payload.requirements.allowed_block_ids) && payload.requirements.allowed_block_ids.length > 0 && payload.requirements.allowed_block_ids.every(nonblank) &&
      Array.isArray(payload.requirements.must_keep) && payload.requirements.must_keep.every(nonblank) &&
      Array.isArray(payload.requirements.length_limits)
  }
  if (!['run', 'clarification', 'review'].includes(String(value.kind)) || !nonblank(value.task_id)) return false
  if (typeof payload.expected_task_version !== 'number' || !Number.isSafeInteger(payload.expected_task_version) || payload.expected_task_version < 1) return false
  if (payload.expected_checkpoint_id !== null && !nonblank(payload.expected_checkpoint_id)) return false
  if (value.kind === 'clarification') return nonblank(payload.feedback)
  if (value.kind === 'review') {
    return (payload.action === 'approved' || payload.action === 'rejected') &&
      (payload.action !== 'rejected' || nonblank(payload.feedback))
  }
  return true
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  if (isRecord(value)) return '{' + Object.keys(value).filter(key => value[key] !== undefined).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}'
  return JSON.stringify(value) ?? 'null'
}

export function readPendingRevisionOperation(
  scope: RevisionOperationScope,
  storage?: RevisionOperationStorage,
): RevisionPendingOperation | null {
  const raw = operationStorage(storage).getItem(operationKey(scope))
  if (raw === null) return null
  let parsed: unknown
  try { parsed = JSON.parse(raw) } catch { throw new Error('改稿恢复信息已损坏，请先查看任务状态。') }
  if (!isRecord(parsed) || parsed.version !== 1 || !isRecord(parsed.scope) ||
    parsed.scope.userId !== scope.userId || parsed.scope.teamId !== scope.teamId || parsed.scope.briefId !== scope.briefId ||
    !validOperation(parsed.operation)) {
    throw new Error('改稿恢复信息与当前任务不匹配，请先查看任务状态。')
  }
  return parsed.operation
}

export function savePendingRevisionOperation(
  scope: RevisionOperationScope,
  operation: RevisionPendingOperation,
  storage?: RevisionOperationStorage,
): void {
  if (!validOperation(operation)) throw new Error('无法保存无效的改稿请求。')
  const target = operationStorage(storage)
  const existing = readPendingRevisionOperation(scope, target)
  if (existing && canonical(existing) !== canonical(operation)) {
    throw new Error('还有结果未确认的改稿操作，请先恢复该操作。')
  }
  // Persist before sending. A lost response must replay the same body and key.
  target.setItem(operationKey(scope), JSON.stringify({ version: 1, scope, operation }))
}

export function clearPendingRevisionOperation(
  scope: RevisionOperationScope,
  idempotencyKey: string,
  storage?: RevisionOperationStorage,
): boolean {
  const target = operationStorage(storage)
  const existing = readPendingRevisionOperation(scope, target)
  if (!existing || existing.payload.idempotency_key !== idempotencyKey) return false
  target.removeItem(operationKey(scope))
  return true
}

export function buildRevisionReviewBlocks(
  task: Pick<RevisionTaskOutput, 'candidate' | 'draft' | 'requirements'>,
  edits: Record<string, string>,
): DraftBlock[] {
  if (!task.candidate) throw new Error('当前没有可审核的候选稿。')
  const allowed = new Set(task.requirements.allowed_block_ids)
  if (Object.keys(edits).some(id => !allowed.has(id))) throw new Error('只能编辑允许修改的段落。')
  const source = task.draft.blocks
  const blocks = task.candidate.blocks
  if (blocks.length !== source.length || blocks.some((block, index) => block.block_id !== source[index].block_id)) {
    throw new Error('候选段落结构已变化，请重新加载任务。')
  }
  return blocks.map((block, index) => {
    if (!allowed.has(block.block_id) && block.text !== source[index].text) throw new Error('只读段落已变化，请重新加载任务。')
    const text = Object.hasOwn(edits, block.block_id) ? edits[block.block_id] : block.text
    if (!text.trim()) throw new Error('段落正文不能为空。')
    return { block_id: block.block_id, text }
  })
}

export function countRevisionText(text: string, unit: 'unicode_chars' | 'whitespace_words'): number {
  if (unit === 'unicode_chars') return Array.from(text).length
  // Match Python str.split(), including U+001C..U+001F and excluding U+FEFF.
  // eslint-disable-next-line no-control-regex -- These control separators are part of Python's documented whitespace set.
  return text.split(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u).filter(Boolean).length
}
