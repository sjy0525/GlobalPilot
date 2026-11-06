import { registerHooks } from 'node:module'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import assert from 'node:assert/strict'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

// Compile the production TSX view in memory so its semantic HTML can be
// checked with ReactDOMServer; no browser or additional package is required.
registerHooks({
  resolve(specifier, context, nextResolve) {
    try { return nextResolve(specifier, context) } catch (error) {
      if (specifier.startsWith('.') && !specifier.match(/\.[a-z]+$/)) {
        for (const ext of ['.ts', '.tsx']) {
          try { return nextResolve(`${specifier}${ext}`, context) } catch { /* try the other source type */ }
        }
      }
      throw error
    }
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.tsx')) return {
      format: 'module', shortCircuit: true,
      source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext } }).outputText,
    }
    return nextLoad(url, context)
  },
})

const { ApiError, configureAuth } = await import('../src/api/http.ts')
const { fetchTicketAssignmentLogs, ticketAssignmentLogsErrorMessage, reassignTeamTicket } = await import('../src/api/ticket.ts')
const { assignmentLogsMatchScope, fetchTicketDetailWithLogs, isAssignmentLogAccessFailure } = await import('../src/workbench/ticketAssignmentLogs.ts')
const { createTicketReadGuard, observeTicketReadAccessFailure } = await import('../src/workbench/ticketReading.ts')
const { default: TicketAssignmentLogs } = await import('../src/pages/TicketDetail/TicketAssignmentLogs.tsx')

configureAuth({ getAccessToken: () => 'log-test-token', refreshAccessToken: async () => null, onUnauthorized() {} })
const stamp = '2026-10-30T01:02:03.123456+00:00'
const ticket = { id: 'ticket-a', team_id: 'team-a', created_by: 'creator-a', assignee_id: 'handler-new', reviewer_id: 'reviewer-new', title: '真实需求', description: '', status: 'processing', processing_result: null, review_comment: null, created_at: stamp, updated_at: stamp }
const log = { id: 'log-a', team_id: ticket.team_id, ticket_id: ticket.id, actor_user_id: 'admin-a', before_assignee_id: 'handler-old', after_assignee_id: 'handler-new', before_reviewer_id: 'reviewer-old', after_reviewer_id: 'reviewer-new', created_at: stamp }
const members = [
  { user_id: 'admin-a', nickname: '真实管理员', email: 'admin@example.invalid', role: 'admin' },
  { user_id: 'handler-new', nickname: '新处理人', email: 'handler@example.invalid', role: 'employee' },
  { user_id: 'reviewer-new', nickname: '新审核人', email: 'reviewer@example.invalid', role: 'employee' },
]
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const scope = { userId: 'reader-a', teamId: ticket.team_id, role: 'admin' }
const props = { result: { status: 'ready', rows: [log] }, loading: false, busy: false, members, namesLoaded: true, currentMemberId: 'admin-a', onRetry() {} }
const markup = (patch = {}) => renderToStaticMarkup(createElement(TicketAssignmentLogs, { ...props, ...patch }))
function deferred() { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

await test('logs GET uses both encoded identities, normal auth and abort signal, with no body or pagination', async () => {
  const controller = new AbortController()
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/team/team%20%2F%20a/tickets/ticket%20%2F%20a/assignment-logs')
    assert.equal(init.method || 'GET', 'GET')
    assert.equal(init.body, undefined)
    assert.equal(init.credentials, 'include')
    assert.equal(init.headers.get('Authorization'), 'Bearer log-test-token')
    assert.equal(init.signal, controller.signal)
    return json([log])
  }
  assert.deepEqual(await fetchTicketAssignmentLogs('team / a', 'ticket / a', controller.signal), [log])
})

await test('real empty log response stays an empty array', async () => {
  globalThis.fetch = async () => json([])
  assert.deepEqual(await fetchTicketAssignmentLogs(ticket.team_id, ticket.id), [])
})

await test('all nine fields and both scope identities must match before records can be displayed', () => {
  assert.equal(assignmentLogsMatchScope([log], ticket.team_id, ticket.id), true)
  assert.equal(assignmentLogsMatchScope([], ticket.team_id, ticket.id), true)
  assert.equal(assignmentLogsMatchScope([log], 'other-team', ticket.id), false)
  assert.equal(assignmentLogsMatchScope([log], ticket.team_id, 'other-ticket'), false)
  for (const value of [null, {}, [null], [{ ...log, created_at: null }], [{ ...log, actor_user_id: '' }], [{ ...log, ticket_id: undefined }]]) assert.equal(assignmentLogsMatchScope(value, ticket.team_id, ticket.id), false)
})

await test('one detail read groups the real detail and log GETs using the same signal', async () => {
  const calls = []
  const controller = new AbortController()
  globalThis.fetch = async (url, init) => {
    calls.push(url)
    assert.equal(init.signal, controller.signal)
    return json(url.endsWith('/assignment-logs') ? [log] : ticket)
  }
  assert.deepEqual(await fetchTicketDetailWithLogs(ticket.team_id, ticket.id, controller.signal), { ticket, assignmentLogs: { status: 'ready', rows: [log] } })
  assert.deepEqual(calls.toSorted(), ['/api/team/team-a/tickets/ticket-a', '/api/team/team-a/tickets/ticket-a/assignment-logs'])
})

await test('non-auth log outage keeps readable detail and exposes a retryable error rather than empty history', async () => {
  globalThis.fetch = async url => url.endsWith('/assignment-logs') ? json({ detail: 'unavailable' }, 503) : json(ticket)
  const result = await fetchTicketDetailWithLogs(ticket.team_id, ticket.id)
  assert.deepEqual(result.ticket, ticket)
  assert.equal(result.assignmentLogs.status, 'error')
  assert.equal('rows' in result.assignmentLogs, false)
  assert.match(result.assignmentLogs.message, /无法读取改派记录.*重新加载/)
})

await test('network failure in logs does not replace readable detail with fabricated empty history', async () => {
  globalThis.fetch = async url => { if (url.endsWith('/assignment-logs')) throw new Error('offline'); return json(ticket) }
  const result = await fetchTicketDetailWithLogs(ticket.team_id, ticket.id)
  assert.equal(result.ticket.id, ticket.id)
  assert.equal(result.assignmentLogs.status, 'error')
  assert.match(result.assignmentLogs.message, /尚未读取/)
})

await test('401, 403 and hidden/cross-team 404 log errors reject the entire detail read', async () => {
  for (const [status, code] of [[401, 'AUTHENTICATION_REQUIRED'], [403, 'TEAM_ACCESS_DENIED'], [403, 'ACCOUNT_DISABLED'], [404, 'TICKET_DETAIL_NOT_FOUND']]) {
    globalThis.fetch = async url => url.endsWith('/assignment-logs') ? json({ code, detail: 'access denied' }, status) : json(ticket)
    await assert.rejects(fetchTicketDetailWithLogs(ticket.team_id, ticket.id), error => error instanceof ApiError && error.status === status && error.code === code)
    assert.equal(isAssignmentLogAccessFailure(new ApiError('denied', status, code)), true)
  }
  assert.equal(isAssignmentLogAccessFailure(new ApiError('offline', 0)), false)
  assert.equal(isAssignmentLogAccessFailure(new ApiError('unavailable', 503)), false)
})

await test('log access denial does not wait for a slower detail response before rejecting', async () => {
  const slow = deferred()
  globalThis.fetch = async url => url.endsWith('/assignment-logs') ? json({ code: 'TEAM_ACCESS_DENIED' }, 403) : slow.promise
  await assert.rejects(fetchTicketDetailWithLogs(ticket.team_id, ticket.id), error => error.status === 403)
  slow.resolve(json(ticket))
})

await test('a log 404 revokes the read generation and aborts immediately while member names are still pending', async () => {
  const slowMembers = deferred()
  const controller = new AbortController()
  const guard = createTicketReadGuard(scope, ticket.id)
  guard.open()
  const oldSnapshot = guard.capture()
  let revocations = 0
  let wholeGroupSettled = false
  globalThis.fetch = async url => url.endsWith('/assignment-logs') ? json({ code: 'TICKET_DETAIL_NOT_FOUND' }, 404) : json(ticket)
  const detail = observeTicketReadAccessFailure(fetchTicketDetailWithLogs(ticket.team_id, ticket.id, controller.signal), () => {
    ++revocations
    guard.close()
    controller.abort()
  })
  const wholeGroup = Promise.allSettled([detail, slowMembers.promise]).then(() => { wholeGroupSettled = true })
  await assert.rejects(detail, error => error.status === 404)
  assert.equal(revocations, 1)
  assert.equal(controller.signal.aborted, true)
  assert.equal(wholeGroupSettled, false)
  assert.equal(guard.isCurrent(oldSnapshot, scope, ticket.id), false)
  guard.open()
  assert.equal(guard.isCurrent(oldSnapshot, scope, ticket.id), false)
  assert.equal(guard.isCurrent(guard.capture(), scope, ticket.id), true)
  slowMembers.resolve([])
  await wholeGroup
})

await test('non-auth history failures do not revoke the operation draft generation', async () => {
  let revocations = 0
  globalThis.fetch = async url => url.endsWith('/assignment-logs') ? json({ detail: 'unavailable' }, 503) : json(ticket)
  const result = await observeTicketReadAccessFailure(fetchTicketDetailWithLogs(ticket.team_id, ticket.id), () => { ++revocations })
  assert.equal(result.assignmentLogs.status, 'error')
  assert.equal(revocations, 0)
})

await test('detail access denial rejects the group even if log GET returns records', async () => {
  globalThis.fetch = async url => url.endsWith('/assignment-logs') ? json([log]) : json({ code: 'TICKET_DETAIL_NOT_FOUND' }, 404)
  await assert.rejects(fetchTicketDetailWithLogs(ticket.team_id, ticket.id), error => error.code === 'TICKET_DETAIL_NOT_FOUND')
})

await test('a malformed or different-scope history is discarded and reported while keeping the correct detail', async () => {
  for (const rows of [[{ ...log, team_id: 'other-team' }], [{ ...log, ticket_id: 'other-ticket' }], [null], null]) {
    globalThis.fetch = async url => json(url.endsWith('/assignment-logs') ? rows : ticket)
    const result = await fetchTicketDetailWithLogs(ticket.team_id, ticket.id)
    assert.equal(result.ticket.id, ticket.id)
    assert.equal(result.assignmentLogs.status, 'error')
    assert.match(result.assignmentLogs.message, /不一致.*停止显示/)
    assert.equal('rows' in result.assignmentLogs, false)
  }
})

await test('explicit retry replaces a failed log read with the actual server records', async () => {
  let logReads = 0
  globalThis.fetch = async url => url.endsWith('/assignment-logs') ? ++logReads === 1 ? json({ detail: 'unavailable' }, 503) : json([log]) : json(ticket)
  assert.equal((await fetchTicketDetailWithLogs(ticket.team_id, ticket.id)).assignmentLogs.status, 'error')
  assert.deepEqual((await fetchTicketDetailWithLogs(ticket.team_id, ticket.id)).assignmentLogs, { status: 'ready', rows: [log] })
  assert.equal(logReads, 2)
})

await test('refresh after a real mocked assignment PATCH reloads both detail and its new audit record', async () => {
  let changed = false
  let logReads = 0
  globalThis.fetch = async (url, init) => {
    if (init.method === 'PATCH') { changed = true; return json({ ...ticket, assignee_id: 'next-handler' }) }
    if (url.endsWith('/assignment-logs')) { ++logReads; return json(changed ? [log, { ...log, id: 'log-b', before_assignee_id: ticket.assignee_id, after_assignee_id: 'next-handler' }] : [log]) }
    return json(changed ? { ...ticket, assignee_id: 'next-handler' } : ticket)
  }
  const before = await fetchTicketDetailWithLogs(ticket.team_id, ticket.id)
  await reassignTeamTicket(ticket.team_id, ticket.id, { assignee_id: 'next-handler', reviewer_id: ticket.reviewer_id, expected_updated_at: stamp })
  const after = await fetchTicketDetailWithLogs(ticket.team_id, ticket.id)
  assert.equal(before.assignmentLogs.rows.length, 1)
  assert.equal(after.assignmentLogs.rows.length, 2)
  assert.equal(after.ticket.assignee_id, 'next-handler')
  assert.equal(after.assignmentLogs.rows[1].after_assignee_id, 'next-handler')
  assert.equal(logReads, 2)
})

await test('a delayed old history cannot enter another account, team, role or ticket scope', async () => {
  for (const current of [{ ...scope, userId: 'reader-b' }, { ...scope, teamId: 'team-b' }, { ...scope, role: 'employee' }, scope]) {
    const guard = createTicketReadGuard(scope, ticket.id)
    guard.open()
    const snapshot = guard.capture()
    const slow = deferred()
    globalThis.fetch = async url => url.endsWith('/assignment-logs') ? slow.promise : json(ticket)
    const accepted = []
    const reading = fetchTicketDetailWithLogs(ticket.team_id, ticket.id).then(data => { if (guard.isCurrent(snapshot, current, current === scope ? 'ticket-b' : ticket.id)) accepted.push(data) })
    slow.resolve(json([log]))
    await reading
    assert.deepEqual(accepted, [])
  }
})

await test('scope ABA invalidation rejects a delayed old detail+history group after reopening the same key', async () => {
  const guard = createTicketReadGuard(scope, ticket.id)
  guard.open()
  const snapshot = guard.capture()
  const slow = deferred()
  globalThis.fetch = async url => url.endsWith('/assignment-logs') ? slow.promise : json(ticket)
  let accepted = null
  const reading = fetchTicketDetailWithLogs(ticket.team_id, ticket.id).then(data => { if (guard.isCurrent(snapshot, scope, ticket.id)) accepted = data })
  guard.close()
  guard.open()
  slow.resolve(json([log]))
  await reading
  assert.equal(accepted, null)
  assert.equal(guard.isCurrent(guard.capture(), scope, ticket.id), true)
})

await test('semantic history markup shows ordered records, raw time, real actor and both responsibility changes', () => {
  const html = markup()
  assert.match(html, /<section[^>]*aria-label="改派记录"/)
  assert.match(html, /<ol[^>]*>[\s\S]*<li>/)
  assert.match(html, new RegExp(`datetime="${stamp.replaceAll('+', '\\+')}"`, 'i'))
  for (const text of ['真实管理员', '新处理人', '新审核人', '操作人', '处理人', '审核人', '改派前', '改派后']) assert.ok(html.includes(text), text)
  assert.match(html, /2026\/10\/30.*09:02/)
})

await test('history uses honest historical IDs and distinct unavailable-member fallback without invented names', () => {
  const history = markup()
  assert.match(history, /历史成员/)
  assert.match(history, /handler-/)
  assert.match(history, /reviewer/)
  assert.doesNotMatch(history, /研发部|设计部|已离职员工/)
  const unavailable = markup({ members: [], namesLoaded: false })
  assert.match(unavailable, /成员信息暂不可用/)
  assert.doesNotMatch(unavailable, /历史成员/)
})

await test('empty, failed and loading markup remain distinct and hide stale records', () => {
  const empty = markup({ result: { status: 'ready', rows: [] } })
  assert.match(empty, /暂无改派记录/)
  assert.doesNotMatch(empty, /<ol|role="alert"/)
  const failed = markup({ result: { status: 'error', message: '服务器暂时不可用' } })
  assert.match(failed, /role="alert"/)
  assert.match(failed, /服务器暂时不可用/)
  assert.match(failed, /重新加载改派记录/)
  assert.doesNotMatch(failed, /暂无改派记录|真实管理员/)
  const loading = markup({ loading: true })
  assert.match(loading, /aria-busy="true"/)
  assert.match(loading, /正在读取改派记录/)
  assert.doesNotMatch(loading, /真实管理员|新处理人|<ol/)
})

await test('retry is disabled during a pending write and unchanged roles are explicitly labelled', () => {
  assert.match(markup({ result: { status: 'error', message: '需要重试' }, busy: true }), /<button[^>]*disabled=""/)
  assert.match(markup({ result: { status: 'ready', rows: [{ ...log, before_reviewer_id: log.after_reviewer_id }] } }), /未变化/)
})

await test('member metadata and server error text are safely escaped in React markup', () => {
  const html = markup({ members: [{ ...members[0], nickname: '<script>unsafe()</script>' }] })
  assert.match(html, /&lt;script&gt;unsafe\(\)&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script>/)
  const failed = markup({ result: { status: 'error', message: '<img src=x onerror=alert(1)>' } })
  assert.match(failed, /&lt;img/)
  assert.doesNotMatch(failed, /<img/)
})

await test('read error messages distinguish hidden ticket, account state and retryable failure', () => {
  assert.equal(ticketAssignmentLogsErrorMessage(new ApiError('hidden', 404, 'TICKET_DETAIL_NOT_FOUND')), '工单不存在或你无权访问。')
  assert.match(ticketAssignmentLogsErrorMessage(new ApiError('disabled', 403, 'ACCOUNT_DISABLED')), /账号已停用/)
  assert.match(ticketAssignmentLogsErrorMessage(new ApiError('removed', 403)), /团队访问权限已失效/)
  assert.match(ticketAssignmentLogsErrorMessage(new ApiError('expired', 401)), /重新登录/)
  assert.match(ticketAssignmentLogsErrorMessage(new ApiError('invalid', 422)), /地址/)
  assert.match(ticketAssignmentLogsErrorMessage(new ApiError('offline', 0)), /尚未读取/)
  assert.match(ticketAssignmentLogsErrorMessage(new Error('unknown')), /重新加载/)
})
