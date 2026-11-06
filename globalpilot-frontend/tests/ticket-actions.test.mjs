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
const { submitTeamTicket, reviewTeamTicket, reassignTeamTicket, ticketActionErrorMessage } = await import('../src/api/ticket.ts')
const { actionSnapshotChanged, createTicketActionGuard, reconcileActionAssignments, ticketActionFailure, ticketActionPermissions, ticketActionReceiptMatches } = await import('../src/workbench/ticketActions.ts')
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
const scope = { userId: 'person-a', teamId: 'team-a', role: 'employee', ticketId: 'ticket-a' }
const timestamp = '2026-10-30T01:02:03.123456+00:00'
const ticket = { id: scope.ticketId, team_id: scope.teamId, created_by: 'creator-a', assignee_id: scope.userId, reviewer_id: 'reviewer-a', status: 'processing', title: '真实工单', description: '', processing_result: null, review_comment: null, created_at: timestamp, updated_at: timestamp }
configureAuth({ getAccessToken: () => 'action-test-token', refreshAccessToken: async () => null, onUnauthorized() {} })

await test('submit sends exactly the trimmed processing result using the authenticated encoded path', async () => {
  const controller = new AbortController()
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/team/team%20%2F%20a/tickets/ticket%20%2F%20a/submit')
    assert.equal(init.method, 'POST')
    assert.equal(init.credentials, 'include')
    assert.equal(init.headers.get('Authorization'), 'Bearer action-test-token')
    assert.equal(init.signal, controller.signal)
    assert.deepEqual(JSON.parse(init.body), { processing_result: '处理结果\n保留换行' })
    return json({ ...ticket, processing_result: '处理结果\n保留换行', status: 'awaiting_review' })
  }
  const result = await submitTeamTicket('team / a', 'ticket / a', '  处理结果\n保留换行  ', controller.signal)
  assert.equal(result.status, 'awaiting_review')
})

await test('review body allowlist preserves the exact server microsecond timestamp string', async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/team/team-a/tickets/ticket-a/review')
    assert.equal(init.method, 'POST')
    assert.deepEqual(JSON.parse(init.body), { decision: 'return', comment: '请补充材料', expected_updated_at: timestamp })
    return json({ ...ticket, status: 'needs_info', review_comment: '请补充材料' })
  }
  const result = await reviewTeamTicket(scope.teamId, scope.ticketId, { decision: 'return', comment: '  请补充材料  ', expected_updated_at: timestamp, status: 'completed', role: 'admin', processing_result: 'forged' })
  assert.equal(result.review_comment, '请补充材料')
})

await test('assignment PATCH allowlist permits same person/self and preserves timestamp precision', async () => {
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/team/team-a/tickets/ticket-a/assignment')
    assert.equal(init.method, 'PATCH')
    assert.deepEqual(JSON.parse(init.body), { assignee_id: scope.userId, reviewer_id: scope.userId, expected_updated_at: timestamp })
    return json({ ...ticket, reviewer_id: scope.userId })
  }
  const result = await reassignTeamTicket(scope.teamId, scope.ticketId, { assignee_id: scope.userId, reviewer_id: scope.userId, expected_updated_at: timestamp, status: 'completed', team_id: 'forged', review_comment: 'forged' })
  assert.equal(result.reviewer_id, scope.userId)
})

await test('only current handler can submit processing or returned work, regardless of admin role', () => {
  for (const status of ['processing', 'needs_info']) {
    assert.equal(ticketActionPermissions({ ...ticket, status }, scope.userId, 'employee').submit, true)
    assert.equal(ticketActionPermissions({ ...ticket, status }, 'unassigned-admin', 'admin').submit, false)
  }
  for (const status of ['awaiting_review', 'completed']) assert.equal(ticketActionPermissions({ ...ticket, status }, scope.userId, 'admin').submit, false)
})

await test('review authority is exact reviewer relation plus awaiting_review, never an admin bypass', () => {
  const waiting = { ...ticket, status: 'awaiting_review' }
  assert.equal(ticketActionPermissions(waiting, 'reviewer-a', 'employee').review, true)
  assert.equal(ticketActionPermissions(waiting, 'reviewer-a', 'admin').review, true)
  assert.equal(ticketActionPermissions(waiting, 'unassigned-admin', 'admin').review, false)
  for (const status of ['processing', 'needs_info', 'completed']) assert.equal(ticketActionPermissions({ ...waiting, status }, 'reviewer-a', 'admin').review, false)
})

await test('only DB-derived admin can reassign an unfinished ticket; nullable reads expose no operations', () => {
  for (const status of ['processing', 'needs_info', 'awaiting_review']) {
    assert.equal(ticketActionPermissions({ ...ticket, status }, scope.userId, 'admin').assignment, true)
    assert.equal(ticketActionPermissions({ ...ticket, status }, scope.userId, 'employee').assignment, false)
  }
  assert.equal(ticketActionPermissions({ ...ticket, status: 'completed' }, scope.userId, 'admin').assignment, false)
  assert.deepEqual(ticketActionPermissions(null, scope.userId, 'admin'), { submit: false, review: false, assignment: false })
})

await test('synchronous mutation guard rejects double submit before React can render disabled state', () => {
  const guard = createTicketActionGuard(scope)
  assert.equal(guard.begin(), false)
  guard.open()
  assert.equal(guard.begin(), true)
  assert.equal(guard.begin(), false)
  guard.finish()
  assert.equal(guard.begin(), true)
  guard.close()
  assert.equal(guard.begin(), false)
})

await test('write receipts are isolated by account, team, actual role and ticket scope', () => {
  const guard = createTicketActionGuard(scope)
  guard.open()
  const snapshot = guard.capture()
  assert.equal(guard.isCurrent(snapshot, scope), true)
  for (const current of [null, { ...scope, userId: 'other-user' }, { ...scope, teamId: 'other-team' }, { ...scope, role: 'admin' }, { ...scope, ticketId: 'other-ticket' }]) assert.equal(guard.isCurrent(snapshot, current), false)
  assert.equal(ticketActionReceiptMatches(ticket, scope), true)
  assert.equal(ticketActionReceiptMatches({ ...ticket, id: 'other-ticket' }, scope), false)
  assert.equal(ticketActionReceiptMatches({ ...ticket, team_id: 'other-team' }, scope), false)
})

await test('late result is ignored after scope A to B to A and after StrictMode reopen', async () => {
  const guard = createTicketActionGuard(scope)
  guard.open()
  const snapshot = guard.capture()
  let resolve
  const pending = new Promise(r => { resolve = r })
  const receipts = []
  const receive = pending.then(result => { if (guard.isCurrent(snapshot, scope)) receipts.push(result) })
  guard.close()
  guard.open()
  resolve(ticket)
  await receive
  assert.deepEqual(receipts, [])
  assert.equal(guard.isCurrent(guard.capture(), scope), true)
})

await test('refreshing a draft detects even a one-microsecond change without mutating its frozen snapshot', () => {
  const draft = { comment: '保留已写意见', expected_updated_at: timestamp }
  assert.equal(actionSnapshotChanged(draft.expected_updated_at, ticket), false)
  assert.equal(actionSnapshotChanged(draft.expected_updated_at, null), false)
  assert.equal(actionSnapshotChanged(draft.expected_updated_at, { ...ticket, updated_at: '2026-10-30T01:02:03.123457+00:00' }), true)
  assert.equal(draft.expected_updated_at, timestamp)
  assert.equal(draft.comment, '保留已写意见')
})

await test('assignment failures clear only invalid selections while preserving other draft fields and self choice', () => {
  const draft = { assignee_id: 'removed-person', reviewer_id: scope.userId, expected_updated_at: timestamp, comment: '保留' }
  const result = reconcileActionAssignments(draft, [{ user_id: scope.userId }])
  assert.equal(result.assignee_id, '')
  assert.equal(result.reviewer_id, scope.userId)
  assert.match(result.notice, /失效的指派已清除/)
  assert.equal(draft.comment, '保留')
  assert.equal(draft.expected_updated_at, timestamp)
  assert.equal(reconcileActionAssignments({ assignee_id: scope.userId, reviewer_id: scope.userId }, [{ user_id: scope.userId }]).notice, '')
})

await test('assignment-specific 403 refreshes ticket without falsely invalidating team membership', () => {
  for (const code of ['TICKET_ASSIGNEE_REQUIRED', 'TICKET_REVIEWER_REQUIRED']) {
    const failure = ticketActionFailure(new ApiError('changed', 403, code))
    assert.equal(failure.refreshTicket, true)
    assert.equal(failure.refreshIdentity, false)
    assert.equal(failure.clearSensitive, false)
    assert.equal(failure.conflict, true)
    assert.doesNotMatch(ticketActionErrorMessage(new ApiError('changed', 403, code)), /团队访问权限/)
  }
})

await test('membership and account denials clear sensitive data, while lost admin role refreshes identity', () => {
  for (const error of [new ApiError('expired', 401), new ApiError('removed', 403, 'TEAM_ACCESS_DENIED'), new ApiError('disabled', 403, 'ACCOUNT_DISABLED')]) {
    const failure = ticketActionFailure(error)
    assert.equal(failure.clearSensitive, true)
    assert.equal(failure.refreshIdentity, true)
  }
  const admin = ticketActionFailure(new ApiError('demoted', 403, 'ADMIN_REQUIRED'))
  assert.equal(admin.refreshIdentity, true)
  assert.equal(admin.clearSensitive, false)
})

await test('409 conflict requires explicit latest-state confirmation and invalid assignment reloads candidates', () => {
  for (const code of ['TICKET_REVIEW_OUTDATED', 'TICKET_ASSIGNMENT_OUTDATED', 'TICKET_STATUS_CONFLICT']) {
    const failure = ticketActionFailure(new ApiError('conflict', 409, code))
    assert.equal(failure.conflict, true)
    assert.equal(failure.refreshTicket, true)
    assert.equal(failure.unknown, false)
    assert.match(ticketActionErrorMessage(new ApiError('conflict', 409, code)), /未执行/)
  }
  assert.equal(ticketActionFailure(new ApiError('invalid', 400, 'INVALID_TICKET_ASSIGNMENT')).refreshCandidates, true)
})

await test('network and server errors classify as uncertain, never as a confirmed failed write', () => {
  for (const error of [new ApiError('offline', 0), new ApiError('server', 500), new ApiError('unavailable', 503), new Error('unknown')]) {
    assert.equal(ticketActionFailure(error).unknown, true)
    assert.match(ticketActionErrorMessage(error), /无法确认操作结果/)
    assert.match(ticketActionErrorMessage(error), /勿重复提交/)
  }
})

await test('all three unkeyed write APIs make one attempt on network failure or 5xx', async () => {
  const actions = [() => submitTeamTicket(scope.teamId, scope.ticketId, '结果'), () => reviewTeamTicket(scope.teamId, scope.ticketId, { decision: 'approve', comment: '', expected_updated_at: timestamp }), () => reassignTeamTicket(scope.teamId, scope.ticketId, { assignee_id: scope.userId, reviewer_id: scope.userId, expected_updated_at: timestamp })]
  for (const action of actions) {
    let calls = 0
    globalThis.fetch = async () => { ++calls; throw new Error('lost connection') }
    await assert.rejects(action(), error => error.status === 0)
    assert.equal(calls, 1)
    calls = 0
    globalThis.fetch = async () => { ++calls; return json({ detail: 'unavailable' }, 503) }
    await assert.rejects(action(), error => error.status === 503)
    assert.equal(calls, 1)
  }
})

await test('validation and hidden-ticket errors have operation-specific feedback', () => {
  assert.match(ticketActionErrorMessage(new ApiError('invalid', 422)), /退回.*审核意见/)
  assert.equal(ticketActionErrorMessage(new ApiError('hidden', 404, 'TICKET_DETAIL_NOT_FOUND')), '工单不存在或你无权访问。')
  assert.equal(ticketActionFailure(new ApiError('hidden', 404)).clearSensitive, true)
  assert.match(ticketActionErrorMessage(new ApiError('disabled', 403, 'ACCOUNT_DISABLED')), /账号已停用/)
  assert.match(ticketActionErrorMessage(new ApiError('demoted', 403, 'ADMIN_REQUIRED')), /管理权限/)
})
