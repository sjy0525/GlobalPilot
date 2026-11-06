import { registerHooks } from 'node:module'
import test from 'node:test'
import assert from 'node:assert/strict'

registerHooks({ resolve(specifier, context, nextResolve) {
  try { return nextResolve(specifier, context) } catch (error) {
    if (specifier.startsWith('.') && !specifier.match(/\.[a-z]+$/)) return nextResolve(`${specifier}.ts`, context)
    throw error
  }
} })

const { ApiError, configureAuth } = await import('../src/api/http.ts')
const api = await import('../src/api/revisions.ts')
const helpers = await import('../src/workbench/revisions.ts')
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const scope = { userId: 'user-a', teamId: 'team-a', briefId: 'brief-a' }
const source = {
  draft_id: 'draft-a', version: 12, source_brief_id: 'brief-a',
  source_checkpoint_id: 'parent-head-a', platform: 'linkedin',
  blocks: [
    { block_id: 'editable', text: '支持 65W 输出，日常充电使用更顺手。' },
    { block_id: 'readonly', text: '  支持 30 天退货。\n' },
  ],
}
const requirements = {
  goal: '精简第一段，保留功率', allowed_block_ids: ['editable'],
  must_keep: ['65W'], length_limits: [],
}
const task = {
  task_id: 'task-a', brief_id: 'brief-a', platform: 'linkedin',
  status: 'awaiting_review', version: 4, checkpoint_id: 'child-head-a',
  draft: source, requirements,
  candidate: {
    candidate_id: 'candidate-a', draft_id: 'draft-a', base_version: 12,
    blocks: [{ block_id: 'editable', text: '支持 65W 输出，充电更顺手。' }, { ...source.blocks[1] }],
  },
}
const createPayload = {
  platform: 'linkedin', expected_draft_id: 'draft-a', expected_draft_version: 12,
  requirements, brand_rules: ['表达简洁'], evidence_document_ids: [],
  max_revisions: 3, idempotency_key: 'create-key',
}
const command = { expected_task_version: 4, expected_checkpoint_id: 'child-head-a', idempotency_key: 'run-key' }

function authorize() {
  configureAuth({ getAccessToken: () => 'revision-test-token', refreshAccessToken: async () => null, onUnauthorized() {} })
}
authorize()

function memoryStorage() {
  const entries = new Map()
  return {
    getItem(key) { return entries.get(key) ?? null },
    setItem(key, value) { entries.set(key, value) },
    removeItem(key) { entries.delete(key) },
    entries,
  }
}

await test('source read uses explicit team and encoded source identity with current auth and abort signal', async () => {
  const controller = new AbortController()
  globalThis.fetch = async (url, init) => {
    const parsed = new URL(url, 'http://test')
    assert.equal(parsed.pathname, '/api/briefs/brief%20%2F%20a/revisions/document')
    assert.equal(parsed.searchParams.get('team_id'), 'team / a')
    assert.equal(parsed.searchParams.get('platform'), 'platform & a')
    assert.equal(init.headers.get('Authorization'), 'Bearer revision-test-token')
    assert.equal(init.credentials, 'include')
    assert.equal(init.signal, controller.signal)
    return json(source)
  }
  assert.deepEqual(await api.readRevisionDocument('brief / a', 'platform & a', 'team / a', controller.signal), source)
})

await test('task history and task reads use scoped authenticated backend routes', async () => {
  const paths = []
  globalThis.fetch = async url => { paths.push(String(url)); return json(paths.length === 1 ? [task] : task) }
  assert.deepEqual(await api.listRevisionTasks('brief-a', 'team-a'), [task])
  assert.deepEqual(await api.getRevisionTask('task / a', 'team-a'), task)
  assert.deepEqual(paths, ['/api/briefs/brief-a/revisions?team_id=team-a', '/api/revision-tasks/task%20%2F%20a?team_id=team-a'])
})

await test('evidence picker loads only the explicitly selected team and brand', async () => {
  const controller = new AbortController()
  const docs = [{ id: 'doc-a', source: '品牌参数表', version: 2 }]
  globalThis.fetch = async (url, init) => {
    const parsed = new URL(url, 'http://test')
    assert.equal(parsed.pathname, '/api/documents')
    assert.equal(parsed.searchParams.get('team_id'), 'team-a')
    assert.equal(parsed.searchParams.get('brand_id'), 'brand / a')
    assert.equal(init.signal, controller.signal)
    return json(docs)
  }
  assert.deepEqual(await api.listRevisionEvidenceDocuments('brand / a', 'team-a', controller.signal), docs)
})

await test('create body allowlist preserves immutable source fields and strips forged status/ownership', async () => {
  const payload = structuredClone(createPayload)
  payload.team_id = 'forged-team'
  payload.status = 'accepted'
  payload.requirements.permitted = true
  payload.requirements.length_limits = [{ block_id: 'editable', unit: 'unicode_chars', maximum: 80, forged: true }]
  const before = structuredClone(payload)
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/briefs/brief-a/revisions?team_id=team-a')
    assert.equal(init.method, 'POST')
    const body = JSON.parse(init.body)
    assert.equal(body.expected_draft_version, 12)
    assert.equal(body.idempotency_key, 'create-key')
    assert.equal(body.team_id, undefined)
    assert.equal(body.status, undefined)
    assert.equal(body.requirements.permitted, undefined)
    assert.deepEqual(body.requirements.length_limits, [{ block_id: 'editable', unit: 'unicode_chars', maximum: 80 }])
    return json(task)
  }
  await api.createRevisionTask('brief-a', payload, 'team-a')
  assert.deepEqual(payload, before)
})

await test('run sends exactly the displayed task version, checkpoint and stable request key', async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/revision-tasks/task-a/run?team_id=team-a')
    assert.deepEqual(JSON.parse(init.body), command)
    return json(task)
  }
  await api.runRevisionTask('task-a', { ...command, candidate: 'forged', status: 'accepted' }, 'team-a')
})

await test('clarification preserves exact keep text and does not submit a replacement source or expanded range', async () => {
  globalThis.fetch = async (url, init) => {
    assert.match(url, /\/clarification\?team_id=team-a$/)
    const body = JSON.parse(init.body)
    assert.equal(body.feedback, '补充受众与资料')
    assert.deepEqual(body.must_keep, [' 65W '])
    assert.deepEqual(body.evidence_document_ids, ['doc-a'])
    assert.equal(body.draft, undefined)
    assert.equal(body.allowed_block_ids, undefined)
    return json(task)
  }
  await api.clarifyRevisionTask('task-a', {
    ...command, feedback: '补充受众与资料', must_keep: [' 65W '], evidence_document_ids: ['doc-a'],
    draft: 'forged', allowed_block_ids: ['readonly'],
  }, 'team-a')
})

await test('human review submits full paragraph IDs and exact whitespace without client-side approval flags', async () => {
  globalThis.fetch = async (url, init) => {
    assert.match(url, /\/review\?team_id=team-a$/)
    const body = JSON.parse(init.body)
    assert.equal(body.action, 'approved')
    assert.equal(body.blocks[0].text, '  支持 65W 输出。\n')
    assert.equal(body.blocks[0].editable, undefined)
    assert.equal(body.passed, undefined)
    return json(task)
  }
  await api.reviewRevisionTask('task-a', {
    ...command, action: 'approved', blocks: [{ block_id: 'editable', text: '  支持 65W 输出。\n', editable: true }], passed: true,
  }, 'team-a')
})

await test('sample export uses the server result rather than reconstructing a training label from UI text', async () => {
  const sample = { task_id: 'task-a', accepted_candidate: task.candidate, human_review: { edited: true } }
  globalThis.fetch = async url => {
    assert.equal(url, '/api/revision-tasks/task-a/sample?team_id=team-a')
    return json(sample)
  }
  assert.deepEqual(await api.exportRevisionSample('task-a', 'team-a'), sample)
})

await test('missing scope is rejected before any request is sent', () => {
  let calls = 0
  globalThis.fetch = async () => { calls += 1; return json(task) }
  assert.throws(() => api.listRevisionTasks('brief-a', ''), /团队/)
  assert.throws(() => api.getRevisionTask('', 'team-a'), /改稿任务/)
  assert.throws(() => api.readRevisionDocument('brief-a', ' ', 'team-a'), /平台/)
  assert.throws(() => api.listRevisionEvidenceDocuments('', 'team-a'), /品牌/)
  assert.equal(calls, 0)
})

await test('version/evidence conflicts preserve actionable backend status and code', async () => {
  globalThis.fetch = async () => json({ detail: '品牌证据已更新，请重新补充资料并检查', code: 'REVISION_EVIDENCE_CONFLICT' }, 409)
  await assert.rejects(api.reviewRevisionTask('task-a', { ...command, action: 'approved' }, 'team-a'), error =>
    error instanceof ApiError && error.status === 409 && error.code === 'REVISION_EVIDENCE_CONFLICT' && /品牌证据/.test(error.message))
})

await test('lost network response retains the exact persisted operation for a later replay', async () => {
  const storage = memoryStorage()
  const operation = { kind: 'run', task_id: 'task-a', payload: command }
  helpers.savePendingRevisionOperation(scope, operation, storage)
  let calls = 0
  globalThis.fetch = async () => { calls += 1; throw new TypeError('lost response') }
  await assert.rejects(api.runRevisionTask('task-a', command, 'team-a'), error => error instanceof ApiError && error.status === 0)
  assert.equal(calls, 1)
  assert.deepEqual(helpers.readPendingRevisionOperation(scope, storage), operation)
})

await test('auth refresh replays the same logical body and request key', async () => {
  let token = 'old-token'
  const bodies = []
  configureAuth({ getAccessToken: () => token, refreshAccessToken: async () => { token = 'new-token'; return token }, onUnauthorized() { assert.fail('must refresh successfully') } })
  globalThis.fetch = async (_url, init) => {
    bodies.push(JSON.parse(init.body))
    return bodies.length === 1 ? json({ detail: 'expired' }, 401) : json(task)
  }
  try { await api.runRevisionTask('task-a', command, 'team-a') } finally { authorize() }
  assert.deepEqual(bodies, [command, command])
})

await test('request keys are secure independent UUIDs and command tokens are taken exactly from task', () => {
  const first = helpers.newRevisionKey()
  const second = helpers.newRevisionKey()
  assert.match(first, /^[0-9a-f-]{36}$/)
  assert.notEqual(first, second)
  assert.deepEqual(helpers.revisionCommand(task, 'stable-key'), { ...command, idempotency_key: 'stable-key' })
  for (const version of [0, -1, 1.5, NaN]) assert.throws(() => helpers.revisionCommand({ ...task, version }, 'key'), /版本/)
})

await test('pending operation storage is isolated by account, team and brief', () => {
  const storage = memoryStorage()
  const operation = { kind: 'create', payload: createPayload }
  helpers.savePendingRevisionOperation(scope, operation, storage)
  assert.deepEqual(helpers.readPendingRevisionOperation(scope, storage), operation)
  for (const other of [{ ...scope, userId: 'user-b' }, { ...scope, teamId: 'team-b' }, { ...scope, briefId: 'brief-b' }]) {
    assert.equal(helpers.readPendingRevisionOperation(other, storage), null)
  }
})

await test('encoded recovery scope components cannot collide at colon boundaries', () => {
  const storage = memoryStorage()
  helpers.savePendingRevisionOperation({ userId: 'a:b', teamId: 'c', briefId: 'd' }, { kind: 'create', payload: createPayload }, storage)
  helpers.savePendingRevisionOperation({ userId: 'a', teamId: 'b:c', briefId: 'd' }, { kind: 'create', payload: createPayload }, storage)
  assert.equal(storage.entries.size, 2)
})

await test('re-saving a replay preserves the key and payload; a different action cannot overwrite unresolved intent', () => {
  const storage = memoryStorage()
  const operation = { kind: 'run', task_id: 'task-a', payload: command }
  helpers.savePendingRevisionOperation(scope, operation, storage)
  helpers.savePendingRevisionOperation(scope, { payload: { idempotency_key: 'run-key', expected_checkpoint_id: 'child-head-a', expected_task_version: 4 }, task_id: 'task-a', kind: 'run' }, storage)
  for (const next of [
    { ...operation, payload: { ...command, idempotency_key: 'new-key' } },
    { ...operation, payload: { ...command, expected_task_version: 5 } },
    { ...operation, task_id: 'task-b' },
    { kind: 'review', task_id: 'task-a', payload: { ...command, action: 'approved' } },
  ]) assert.throws(() => helpers.savePendingRevisionOperation(scope, next, storage), /结果未确认/)
  assert.deepEqual(helpers.readPendingRevisionOperation(scope, storage), operation)
})

await test('stale response cannot clear another operation and successful matching response clears only its own scope', () => {
  const storage = memoryStorage()
  const operation = { kind: 'run', task_id: 'task-a', payload: command }
  helpers.savePendingRevisionOperation(scope, operation, storage)
  assert.equal(helpers.clearPendingRevisionOperation(scope, 'older-key', storage), false)
  assert.deepEqual(helpers.readPendingRevisionOperation(scope, storage), operation)
  assert.equal(helpers.clearPendingRevisionOperation(scope, 'run-key', storage), true)
  assert.equal(helpers.readPendingRevisionOperation(scope, storage), null)
})

await test('saved request is a snapshot independent of later edits to caller objects', () => {
  const storage = memoryStorage()
  const operation = structuredClone({ kind: 'create', payload: createPayload })
  helpers.savePendingRevisionOperation(scope, operation, storage)
  operation.payload.requirements.goal = 'Changed after sending'
  const restored = helpers.readPendingRevisionOperation(scope, storage)
  assert.equal(restored.payload.requirements.goal, requirements.goal)
  restored.payload.requirements.goal = 'Changed after reading'
  assert.equal(helpers.readPendingRevisionOperation(scope, storage).payload.requirements.goal, requirements.goal)
})

await test('corrupt recovery data and mismatched account envelopes produce an explicit recovery error', () => {
  const storage = memoryStorage()
  helpers.savePendingRevisionOperation(scope, { kind: 'run', task_id: 'task-a', payload: command }, storage)
  const [key] = storage.entries.keys()
  storage.setItem(key, '{bad-json')
  assert.throws(() => helpers.readPendingRevisionOperation(scope, storage), /恢复信息已损坏/)
  storage.setItem(key, JSON.stringify({ version: 1, scope: { ...scope, userId: 'other-user' }, operation: { kind: 'run', task_id: 'task-a', payload: command } }))
  assert.throws(() => helpers.readPendingRevisionOperation(scope, storage), /不匹配/)
})

await test('invalid recovered request formats are never sent as valid replay operations', () => {
  const storage = memoryStorage()
  for (const operation of [
    { kind: 'run', task_id: 'task-a', payload: { ...command, expected_task_version: 0 } },
    { kind: 'run', task_id: 'task-a', payload: { ...command, expected_checkpoint_id: undefined } },
    { kind: 'review', task_id: 'task-a', payload: { ...command, action: 'rejected', feedback: ' ' } },
    { kind: 'create', payload: { ...createPayload, requirements: { ...requirements, allowed_block_ids: [] } } },
  ]) assert.throws(() => helpers.savePendingRevisionOperation(scope, operation, storage), /无效/)
  assert.equal(storage.entries.size, 0)
})

await test('storage failure stops before an unresolved operation could be replaced or sent', () => {
  const storage = { getItem() { return null }, setItem() { throw new Error('quota exceeded') }, removeItem() {} }
  assert.throws(() => helpers.savePendingRevisionOperation(scope, { kind: 'run', task_id: 'task-a', payload: command }, storage), /quota/)
})

await test('human edit builder changes only editable text and preserves readonly whitespace, IDs and ordering', () => {
  const before = structuredClone(task)
  const blocks = helpers.buildRevisionReviewBlocks(task, { editable: '  支持 65W 输出。\n' })
  assert.deepEqual(blocks, [{ block_id: 'editable', text: '  支持 65W 输出。\n' }, source.blocks[1]])
  assert.deepEqual(task, before)
  blocks[1].text = 'mutated'
  assert.deepEqual(task, before)
})

await test('human edit builder rejects scope expansion, blank text and changed candidate structure', () => {
  assert.throws(() => helpers.buildRevisionReviewBlocks(task, { readonly: 'changed' }), /只能编辑/)
  assert.throws(() => helpers.buildRevisionReviewBlocks(task, { editable: ' \n' }), /不能为空/)
  const wrong = structuredClone(task)
  wrong.candidate.blocks.reverse()
  assert.throws(() => helpers.buildRevisionReviewBlocks(wrong, {}), /结构已变化/)
  const outside = structuredClone(task)
  outside.candidate.blocks[1].text = 'changed outside'
  assert.throws(() => helpers.buildRevisionReviewBlocks(outside, {}), /只读段落/)
})

await test('displayed length uses the same Unicode code points and whitespace split as backend rules', () => {
  assert.equal(helpers.countRevisionText('👩‍💻e\u0301é', 'unicode_chars'), 6)
  assert.equal(helpers.countRevisionText("don't co-operate 中文", 'whitespace_words'), 3)
  assert.equal(helpers.countRevisionText('a\u001cb\u0085c\u3000d', 'whitespace_words'), 4)
  assert.equal(helpers.countRevisionText('\ufeff', 'whitespace_words'), 1)
  assert.equal(helpers.countRevisionText(' \n\t', 'whitespace_words'), 0)
})

await test('user-facing status never conflates a submitted decision with adopted manuscript', () => {
  assert.equal(helpers.revisionStatusLabel('adopting'), '正在采纳')
  assert.equal(helpers.revisionStatusLabel('accepted'), '已采纳')
  assert.equal(helpers.revisionStatusLabel('awaiting_review'), '待审核')
  assert.match(helpers.revisionStatusLabel('future-status'), /未知状态/)
})
