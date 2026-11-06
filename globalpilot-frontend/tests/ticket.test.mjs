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
const { buildTicketCreateBody, createTeamTicket, ticketErrorMessage } = await import('../src/api/ticket.ts')
const { createTicketScopeGuard, receiptMatchesScope, reconcileTicketAssignments } = await import('../src/workbench/ticketCreation.ts')
configureAuth({ getAccessToken: () => 'ticket-test-access', refreshAccessToken: async () => null, onUnauthorized() {} })

const scope = { userId: 'user-a', teamId: 'team-a' }
const draft = { title: '  新品宣传材料  ', description: '  补充说明\n保留正文换行。  ', assignee_id: 'person-a', reviewer_id: 'person-b' }
const ticket = { ...buildTicketCreateBody(draft), id: 'ticket-real-id', team_id: scope.teamId, created_by: scope.userId, status: 'processing', created_at: '2026-10-28T08:00:00Z', updated_at: '2026-10-28T08:00:00Z' }
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function deferred() { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

await test('ticket body uses only the four permitted fields and trims surrounding whitespace', () => {
  const body = buildTicketCreateBody({ ...draft, team_id: 'forged-team', created_by: 'forged-user', status: 'completed', role: 'admin' })
  assert.deepEqual(body, { title: '新品宣传材料', description: '补充说明\n保留正文换行。', assignee_id: 'person-a', reviewer_id: 'person-b' })
  assert.deepEqual(Object.keys(body), ['title', 'description', 'assignee_id', 'reviewer_id'])
})

await test('real ticket request encodes team path, reuses auth, and returns the server receipt', async () => {
  const controller = new AbortController()
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/team/team%20%2F%20a/tickets')
    assert.equal(init.method, 'POST')
    assert.equal(init.credentials, 'include')
    assert.equal(init.headers.get('Authorization'), 'Bearer ticket-test-access')
    assert.equal(init.signal, controller.signal)
    assert.deepEqual(JSON.parse(init.body), buildTicketCreateBody(draft))
    return json(ticket, 201)
  }
  const result = await createTeamTicket('team / a', { ...draft, created_by: 'forged-user', status: 'completed' }, controller.signal)
  assert.equal(result.id, 'ticket-real-id')
  assert.equal(result.status, 'processing')
  assert.equal(result.created_by, scope.userId)
})

await test('server-side invalid assignment is surfaced without retrying the POST', async () => {
  let calls = 0
  globalThis.fetch = async () => { calls++; return json({ code: 'INVALID_TICKET_ASSIGNMENT', detail: 'invalid assignment' }, 400) }
  let failed
  try { await createTeamTicket(scope.teamId, draft) } catch (error) { failed = error }
  assert.equal(calls, 1)
  assert.equal(failed.code, 'INVALID_TICKET_ASSIGNMENT')
  assert.match(ticketErrorMessage(failed), /刷新成员后重新选择/)
})

await test('network failure never retries ticket creation or claims the result is known', async () => {
  let calls = 0
  globalThis.fetch = async () => { calls++; throw new Error('connection interrupted') }
  let failed
  try { await createTeamTicket(scope.teamId, draft) } catch (error) { failed = error }
  assert.equal(calls, 1)
  assert.equal(failed.status, 0)
  assert.equal(ticketErrorMessage(failed), '连接中断，暂时无法确认创建结果，请勿重复提交。')
  assert.match(ticketErrorMessage(failed, true), /重试/)
})

await test('server failure does not automatically repeat an unkeyed create request', async () => {
  let calls = 0
  globalThis.fetch = async () => { calls++; return json({ detail: 'unavailable' }, 503) }
  await assert.rejects(createTeamTicket(scope.teamId, draft), error => error.status === 503)
  assert.equal(calls, 1)
})

await test('scope guard rejects both an old account and an old team', () => {
  const guard = createTicketScopeGuard(scope)
  guard.open()
  const snapshot = guard.capture()
  assert.equal(guard.isCurrent(snapshot, scope), true)
  assert.equal(guard.isCurrent(snapshot, { ...scope, userId: 'user-b' }), false)
  assert.equal(guard.isCurrent(snapshot, { ...scope, teamId: 'team-b' }), false)
  assert.equal(guard.isCurrent(snapshot, null), false)
})

await test('unmount or StrictMode cleanup prevents old responses from entering a reopened scope', () => {
  const guard = createTicketScopeGuard(scope)
  guard.open()
  const oldSnapshot = guard.capture()
  guard.close()
  assert.equal(guard.isCurrent(oldSnapshot, scope), false)
  guard.open()
  assert.equal(guard.isCurrent(oldSnapshot, scope), false)
  assert.equal(guard.isCurrent(guard.capture(), scope), true)
})

await test('late receipt is ignored after team A to B to A navigation', async () => {
  const guard = createTicketScopeGuard(scope)
  guard.open()
  const snapshot = guard.capture()
  const pending = deferred()
  const receipts = []
  const reading = pending.promise.then(value => {
    if (guard.isCurrent(snapshot, scope) && receiptMatchesScope(value, scope)) receipts.push(value)
  })
  guard.close()
  guard.open()
  pending.resolve(ticket)
  await reading
  assert.deepEqual(receipts, [])
})

await test('server receipt must match the submitted actor and team', () => {
  assert.equal(receiptMatchesScope(ticket, scope), true)
  assert.equal(receiptMatchesScope({ ...ticket, team_id: 'another-team' }, scope), false)
  assert.equal(receiptMatchesScope({ ...ticket, created_by: 'another-user' }, scope), false)
})

await test('refresh clears inactive assignments while preserving title, description, and valid selection', () => {
  const result = reconcileTicketAssignments(draft, [{ user_id: 'person-b' }])
  assert.equal(result.draft.assignee_id, '')
  assert.equal(result.draft.reviewer_id, 'person-b')
  assert.equal(result.draft.title, draft.title)
  assert.equal(result.draft.description, draft.description)
  assert.match(result.notice, /原处理人/)
  assert.doesNotMatch(result.notice, /原审核人/)
})

await test('both removed assignments are cleared and explained', () => {
  const result = reconcileTicketAssignments(draft, [])
  assert.equal(result.draft.assignee_id, '')
  assert.equal(result.draft.reviewer_id, '')
  assert.match(result.notice, /原处理人/)
  assert.match(result.notice, /原审核人/)
})

await test('one active person, including the creator, can handle and review the same ticket', () => {
  const samePerson = { ...draft, assignee_id: scope.userId, reviewer_id: scope.userId, description: '' }
  const result = reconcileTicketAssignments(samePerson, [{ user_id: scope.userId }])
  assert.deepEqual(result.draft, samePerson)
  assert.equal(result.notice, '')
  assert.equal(buildTicketCreateBody(samePerson).description, '')
})

await test('membership, disabled-account, and validation failures have specific Chinese feedback', () => {
  assert.match(ticketErrorMessage(new ApiError('forbidden', 403)), /团队访问权限已失效/)
  assert.match(ticketErrorMessage(new ApiError('disabled', 403, 'ACCOUNT_DISABLED'), true), /账号已停用/)
  assert.match(ticketErrorMessage(new ApiError('invalid', 422)), /标题/)
  assert.match(ticketErrorMessage(new ApiError('expired', 401)), /重新登录/)
  assert.match(ticketErrorMessage(new ApiError('missing', 404)), /团队不存在/)
})
