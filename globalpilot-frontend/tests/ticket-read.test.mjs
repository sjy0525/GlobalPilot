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
const { fetchTeamTickets, fetchTeamTicket, ticketReadErrorMessage } = await import('../src/api/ticket.ts')
const { createTicketReadGuard, ticketMatchesReadScope, ticketPerson, formatTicketTime, filterTicketRows } = await import('../src/workbench/ticketReading.ts')
configureAuth({ getAccessToken: () => 'read-test-access', refreshAccessToken: async () => null, onUnauthorized() {} })
const scope = { userId: 'reader-a', teamId: 'team-a', role: 'admin' }
const ticket = { id: 'ticket-a', team_id: scope.teamId, title: '品牌材料', description: '补充信息', created_by: 'creator-a', assignee_id: 'assignee-a', reviewer_id: 'reviewer-a', status: 'processing', created_at: '2026-10-28T08:30:00Z', updated_at: '2026-10-28T09:00:00Z' }
const people = [{ user_id: 'creator-a', email: 'alice@example.invalid', nickname: '爱丽丝', role: 'employee' }, { user_id: 'assignee-a', email: 'bob@example.invalid', nickname: '鲍勃', role: 'admin' }]
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function deferred() { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

await test('list GET returns the exact server array without pagination or client role parameters', async () => {
  const controller = new AbortController()
  const rows = [ticket, { ...ticket, id: 'ticket-b', created_by: 'someone-else' }]
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/team/team-a/tickets')
    assert.equal(init.method || 'GET', 'GET')
    assert.equal(init.body, undefined)
    assert.equal(init.headers.get('Authorization'), 'Bearer read-test-access')
    assert.equal(init.credentials, 'include')
    assert.equal(init.signal, controller.signal)
    return json(rows)
  }
  assert.deepEqual(await fetchTeamTickets(scope.teamId, controller.signal), rows)
})

await test('detail GET uses both encoded path identities and needs no creation receipt cache', async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/team/team%20%2F%20a/tickets/ticket%20%2F%20a')
    assert.equal(init.method || 'GET', 'GET')
    assert.equal(init.body, undefined)
    return json(ticket)
  }
  assert.deepEqual(await fetchTeamTicket('team / a', 'ticket / a'), ticket)
})

await test('an empty list remains a real empty array', async () => {
  globalThis.fetch = async () => json([])
  assert.deepEqual(await fetchTeamTickets(scope.teamId), [])
})

await test('both supported detail-not-found codes keep the same non-disclosing feedback', async () => {
  for (const code of ['TICKET_DETAIL_NOT_FOUND', 'TICKET_NOT_FOUND']) {
    globalThis.fetch = async () => json({ code, detail: 'hidden or missing' }, 404)
    let failed
    try { await fetchTeamTicket(scope.teamId, 'hidden-ticket') } catch (error) { failed = error }
    assert.equal(failed.code, code)
    assert.equal(ticketReadErrorMessage(failed, 'detail'), '工单不存在或你无权访问。')
    assert.doesNotMatch(ticketReadErrorMessage(failed, 'detail'), /团队不存在/)
  }
})

await test('read failures offer safe retry feedback rather than the creation uncertainty warning', () => {
  assert.match(ticketReadErrorMessage(new ApiError('offline', 0), 'list'), /重试/)
  assert.match(ticketReadErrorMessage(new ApiError('offline', 0), 'detail'), /重试/)
  assert.doesNotMatch(ticketReadErrorMessage(new ApiError('offline', 0), 'detail'), /创建结果/)
  assert.match(ticketReadErrorMessage(new ApiError('expired', 401), 'detail'), /重新登录/)
  assert.match(ticketReadErrorMessage(new ApiError('forbidden', 403), 'detail'), /团队访问权限/)
  assert.match(ticketReadErrorMessage(new ApiError('disabled', 403, 'ACCOUNT_DISABLED'), 'list'), /账号已停用/)
  assert.match(ticketReadErrorMessage(new ApiError('bad id', 422), 'detail'), /地址无效/)
})

await test('a failed GET can be explicitly retried without a hidden fallback result', async () => {
  let calls = 0
  globalThis.fetch = async () => ++calls === 1 ? json({ detail: 'unavailable' }, 503) : json([ticket])
  await assert.rejects(fetchTeamTickets(scope.teamId), error => error.status === 503)
  assert.equal(calls, 1)
  assert.deepEqual(await fetchTeamTickets(scope.teamId), [ticket])
  assert.equal(calls, 2)
})

await test('read guard scopes include the actual role as well as account, team, and ticket id', () => {
  const guard = createTicketReadGuard(scope, 'ticket-a')
  guard.open()
  const snapshot = guard.capture()
  assert.equal(guard.isCurrent(snapshot, scope, 'ticket-a'), true)
  assert.equal(guard.isCurrent(snapshot, { ...scope, userId: 'reader-b' }, 'ticket-a'), false)
  assert.equal(guard.isCurrent(snapshot, { ...scope, teamId: 'team-b' }, 'ticket-a'), false)
  assert.equal(guard.isCurrent(snapshot, { ...scope, role: 'employee' }, 'ticket-a'), false)
  assert.equal(guard.isCurrent(snapshot, scope, 'ticket-b'), false)
  assert.equal(guard.isCurrent(snapshot, null, 'ticket-a'), false)
})

await test('old admin response cannot return after admin-to-employee-to-admin ABA', async () => {
  const guard = createTicketReadGuard(scope, 'list')
  guard.open()
  const snapshot = guard.capture()
  const pending = deferred()
  const visible = []
  const receiving = pending.promise.then(rows => { if (guard.isCurrent(snapshot, scope, 'list')) visible.push(...rows) })
  assert.equal(guard.isCurrent(snapshot, { ...scope, role: 'employee' }, 'list'), false)
  guard.close()
  guard.open()
  pending.resolve([ticket])
  await receiving
  assert.deepEqual(visible, [])
  assert.equal(guard.isCurrent(guard.capture(), scope, 'list'), true)
})

await test('a fresh guard can reload a batched same-key scope while the closed guard rejects old data', () => {
  const oldGuard = createTicketReadGuard(scope, 'list')
  oldGuard.open()
  const oldSnapshot = oldGuard.capture()
  oldGuard.close()
  const freshGuard = createTicketReadGuard(scope, 'list', 1)
  freshGuard.open()
  assert.equal(oldGuard.isCurrent(oldSnapshot, scope, 'list'), false)
  assert.equal(freshGuard.isCurrent(freshGuard.capture(), scope, 'list'), true)
  assert.ok(freshGuard.capture().epoch > oldSnapshot.epoch)
})

await test('late detail A cannot replace a visible detail B in the same team', async () => {
  const first = createTicketReadGuard(scope, 'ticket-a')
  first.open()
  const snapshot = first.capture()
  const pending = deferred()
  let visible = null
  const oldReading = pending.promise.then(value => { if (first.isCurrent(snapshot, scope, 'ticket-b')) visible = value })
  first.close()
  const second = createTicketReadGuard(scope, 'ticket-b')
  second.open()
  const secondTicket = { ...ticket, id: 'ticket-b', title: '第二张工单' }
  if (second.isCurrent(second.capture(), scope, 'ticket-b')) visible = secondTicket
  pending.resolve(ticket)
  await oldReading
  assert.equal(visible.id, 'ticket-b')
  assert.equal(visible.title, '第二张工单')
})

await test('unmount and StrictMode cleanup invalidate prior read epochs', () => {
  const guard = createTicketReadGuard(scope, 'list')
  guard.open()
  const snapshot = guard.capture()
  guard.close()
  assert.equal(guard.isCurrent(snapshot, scope, 'list'), false)
  guard.open()
  assert.equal(guard.isCurrent(snapshot, scope, 'list'), false)
  assert.equal(guard.isCurrent(guard.capture(), scope, 'list'), true)
})

await test('ticket data must match the requested team and the requested detail id', () => {
  assert.equal(ticketMatchesReadScope(ticket, 'team-a'), true)
  assert.equal(ticketMatchesReadScope(ticket, 'team-b'), false)
  assert.equal(ticketMatchesReadScope(ticket, 'team-a', 'ticket-a'), true)
  assert.equal(ticketMatchesReadScope(ticket, 'team-a', 'ticket-b'), false)
})

await test('active member labels use only real nickname or email metadata', () => {
  assert.deepEqual(ticketPerson('creator-a', people, true), { name: '爱丽丝', detail: 'alice@example.invalid', historical: false })
  assert.deepEqual(ticketPerson('creator-a', [{ ...people[0], nickname: ' ' }], true), { name: 'alice', detail: 'alice@example.invalid', historical: false })
})

await test('missing historical members have no invented names or departments', () => {
  const person = ticketPerson('c16a568a-49be-4f07-904f-39bbce88d5a7', people, true)
  assert.deepEqual(person, { name: '历史成员', detail: 'c16a568a', historical: true })
})

await test('failed name lookup never mislabels members as departed historical people', () => {
  assert.deepEqual(ticketPerson('unknown-person', [], false), { name: '成员信息暂不可用', detail: 'unknown-', historical: false })
})

await test('local search and status filtering preserve the server-provided rows and relationships', () => {
  const second = { ...ticket, id: 'ticket-b', title: '其他需求', status: 'completed', created_by: 'someone-else', assignee_id: 'someone-else', reviewer_id: 'someone-else' }
  const rows = [ticket, second]
  assert.deepEqual(filterTicketRows(rows, '', 'all', people), rows)
  assert.deepEqual(filterTicketRows(rows, 'ALICE', 'all', people), [ticket])
  assert.deepEqual(filterTicketRows(rows, '', 'completed', people), [second])
  assert.deepEqual(filterTicketRows(rows, 'not-a-ticket', 'all', people), [])
  assert.equal(rows.length, 2)
})

await test('ticket times render in Shanghai timezone without inventing a date for invalid input', () => {
  assert.match(formatTicketTime('2026-10-28T08:30:00Z'), /2026\/10\/28.*16:30/)
  assert.equal(formatTicketTime('invalid'), '时间信息暂不可用')
})
