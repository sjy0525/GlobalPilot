import { registerHooks } from 'node:module'
import test from 'node:test'
import assert from 'node:assert/strict'

registerHooks({ resolve(specifier, context, nextResolve) {
  try { return nextResolve(specifier, context) } catch (error) {
    if (specifier.startsWith('.') && !specifier.match(/\.[a-z]+$/)) return nextResolve(`${specifier}.ts`, context)
    throw error
  }
} })

const selection = new Map()
globalThis.window = {
  localStorage: { removeItem() {} },
  sessionStorage: { getItem: key => selection.get(key) ?? null, setItem: (key, value) => selection.set(key, value) },
}
const { useAuthStore: auth } = await import('../src/store/authStore.ts')
const { useLiveTeamStore: teams } = await import('../src/workbench/liveTeamStore.ts')
const { teamErrorMessage } = await import('../src/api/team.ts')
const { getWorkbenchAccess } = await import('../src/workbench/permissions.ts')
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const membership = (teamId = 'team-a', role = 'admin', userId = 'user-1') => ({ team_id: teamId, team_name: `团队 ${teamId}`, user_id: userId, role, status: 'active' })
const identity = team => ({ team_id: team.team_id, user_id: team.user_id, role: team.role, status: team.status })
const member = (userId = 'user-1', role = 'admin') => ({ user_id: userId, email: `${userId}@example.invalid`, nickname: userId, role })
function signIn(userId = 'user-1') {
  auth.setState({ user: { id: userId, name: userId, email: `${userId}@example.invalid`, role: 'admin', teamId: 'forged-team' }, token: `test-${userId}`, isAuthenticated: true, ready: true })
  teams.getState().reset(userId)
}
function deferred() { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
async function until(predicate) {
  for (let count = 0; count < 20 && !predicate(); count++) await new Promise(resolve => setImmediate(resolve))
  assert.equal(predicate(), true, 'expected request was not reached')
}
function basicApi({ memberships = [membership()], people = [member()], post, patch, remove } = {}) {
  return async (url, init) => {
    if (url === '/api/team') return json(memberships)
    if (url.endsWith('/me')) return json(identity(memberships.find(item => url === `/api/team/${item.team_id}/me`)))
    if (url.endsWith('/members/available')) return json(people)
    if (init?.method === 'POST' && post) return post(url, init)
    if (init?.method === 'PATCH' && patch) return patch(url, init)
    if (init?.method === 'DELETE' && remove) return remove(url, init)
    throw new Error(`Unexpected API call ${url}`)
  }
}

await test('workbench identity comes from membership APIs rather than login role or stored selection', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi({ memberships: [membership('team-a', 'employee')] })
  await teams.getState().refreshTeams('user-1')
  const current = teams.getState().membership
  assert.equal(current.role, 'employee')
  assert.equal(current.team_id, 'team-a')
  assert.equal(current.team_name, '团队 team-a')
  assert.equal(teams.getState().teams[0].team_name, '团队 team-a')
  assert.equal(getWorkbenchAccess({ teamId: current.team_id, role: current.role, status: current.status }).canManageTeam, false)
  assert.equal(getWorkbenchAccess({ teamId: current.team_id, role: current.role, status: 'removed' }).canAccessWorkbench, false)
  assert.deepEqual(teams.getState().members, [])
})

await test('selected team survives reload per account, while current role is fetched again', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi({ memberships: [membership(), membership('team-b', 'employee')] })
  await teams.getState().refreshTeams('user-1')
  await teams.getState().selectTeam('team-b')
  assert.equal(selection.get('gp-team-selection:user-1'), 'team-b')
  teams.getState().reset('user-1')
  await teams.getState().refreshTeams('user-1')
  assert.equal(teams.getState().membership.team_id, 'team-b')
  assert.equal(teams.getState().membership.role, 'employee')
  assert.equal(teams.getState().membership.team_name, '团队 team-b')
  assert.deepEqual(teams.getState().teams.map(team => team.team_name), ['团队 team-a', '团队 team-b'])
  signIn('user-2')
  globalThis.fetch = basicApi({ memberships: [membership('team-a', 'employee', 'user-2')] })
  await teams.getState().refreshTeams('user-2')
  assert.equal(teams.getState().membership.team_id, 'team-a')
  assert.equal(selection.get('gp-team-selection:user-1'), 'team-b')
})

await test('late team responses cannot restore data after account switch or logout', async () => {
  selection.clear()
  signIn('old-account')
  const pending = deferred()
  globalThis.fetch = async () => pending.promise
  const reading = teams.getState().refreshTeams('old-account')
  signIn('new-account')
  pending.resolve(json([membership('old-team', 'admin', 'old-account')]))
  await reading
  assert.equal(teams.getState().userId, 'new-account')
  assert.equal(teams.getState().membership, null)
  assert.deepEqual(teams.getState().members, [])
  auth.setState({ user: null, token: null, isAuthenticated: false })
  assert.equal(teams.getState().userId, null)
  assert.deepEqual(teams.getState().teams, [])
})

await test('a late selected-team identity response cannot overwrite a newer selection', async () => {
  selection.clear()
  signIn()
  const choices = [membership(), membership('team-b', 'employee')]
  globalThis.fetch = basicApi({ memberships: choices })
  await teams.getState().refreshTeams('user-1')
  const pending = deferred()
  const basic = basicApi({ memberships: choices })
  let readingB = false
  globalThis.fetch = async (url, init) => {
    if (url === '/api/team/team-b/me') { readingB = true; return pending.promise }
    return basic(url, init)
  }
  const earlier = teams.getState().selectTeam('team-b')
  await until(() => readingB)
  await teams.getState().selectTeam('team-a')
  pending.resolve(json(identity(choices[1])))
  await earlier
  assert.equal(teams.getState().membership.team_id, 'team-a')
  assert.equal(teams.getState().membership.role, 'admin')
  assert.deepEqual(teams.getState().members, [member()])
})

await test('a rejected member read clears old data and rechecks an administrator downgrade', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  assert.equal(teams.getState().members.length, 1)
  globalThis.fetch = async (url) => url.endsWith('/members/available')
    ? json({ detail: 'forbidden', code: 'FORBIDDEN' }, 403)
    : json(identity(membership('team-a', 'employee')))
  await teams.getState().refreshMembers()
  assert.equal(teams.getState().membership.role, 'employee')
  assert.equal(teams.getState().membership.team_name, '团队 team-a')
  assert.equal(teams.getState().teams[0].team_name, '团队 team-a')
  assert.deepEqual(teams.getState().members, [])
})

await test('adding sends email only, prevents duplicate submission, and refreshes real members', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  const pending = deferred()
  let writes = 0
  globalThis.fetch = basicApi({ people: [member(), member('new-user', 'employee')], post: (_url, init) => {
    writes++
    assert.deepEqual(JSON.parse(init.body), { email: 'new-user@example.invalid' })
    assert.ok(init.signal instanceof AbortSignal)
    return pending.promise
  } })
  const adding = teams.getState().addMember(' new-user@example.invalid ')
  assert.equal(teams.getState().memberMutation, 'add')
  await assert.rejects(teams.getState().addMember('new-user@example.invalid'), /重复提交/)
  pending.resolve(json(member('new-user', 'employee'), 201))
  const result = await adding
  assert.equal(result.value.role, 'employee')
  assert.equal(result.currentScope, true)
  assert.equal(writes, 1)
  assert.equal(teams.getState().members.length, 2)
  assert.equal(teams.getState().memberMutation, null)
})

await test('target-account disabled error preserves a valid administrator and has clear feedback', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ post: () => json({ detail: 'disabled', code: 'ACCOUNT_DISABLED' }, 403) })
  let failed
  try { await teams.getState().addMember('disabled@example.invalid') } catch (error) { failed = error }
  assert.equal(failed.code, 'ACCOUNT_DISABLED')
  assert.equal(teamErrorMessage(failed, true), '该账号已停用，无法添加。')
  assert.equal(teams.getState().membership.role, 'admin')
  assert.deepEqual(teams.getState().members, [member()])
  assert.equal(teams.getState().memberMutation, null)
  assert.equal(teamErrorMessage({}), '请求失败，请稍后重试。')
})

await test('a removed member loses the workspace and old member data after refresh', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ memberships: [] })
  await teams.getState().refreshTeams('user-1')
  assert.equal(teams.getState().membership, null)
  assert.deepEqual(teams.getState().members, [])
  assert.equal(teams.getState().status, 'ready')
})

await test('removal during a pending add cancels its scope and never leaves a stuck submit state', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  const pending = deferred()
  let signal
  globalThis.fetch = basicApi({ memberships: [], post: (_url, init) => { signal = init.signal; return pending.promise } })
  const adding = teams.getState().addMember('new-user@example.invalid')
  await teams.getState().refreshTeams('user-1')
  assert.equal(signal.aborted, true)
  assert.equal(teams.getState().memberMutation, null)
  pending.resolve(json(member('new-user', 'employee'), 201))
  assert.equal((await adding).currentScope, false)
  assert.equal(teams.getState().membership, null)
  assert.deepEqual(teams.getState().members, [])
})

await test('role update sends only role and refreshes teams, identity, and members after success', async () => {
  selection.clear()
  signIn()
  const people = [member(), member('worker', 'employee')]
  globalThis.fetch = basicApi({ people })
  await teams.getState().refreshTeams('user-1')
  const requests = []
  const api = basicApi({ people, patch: (url, init) => {
    assert.equal(url, '/api/team/team-a/members/worker/role')
    assert.deepEqual(JSON.parse(init.body), { role: 'admin' })
    assert.ok(init.signal instanceof AbortSignal)
    people[1] = member('worker', 'admin')
    return json(people[1])
  } })
  globalThis.fetch = (url, init) => { requests.push([init?.method || 'GET', url]); return api(url, init) }
  const result = await teams.getState().changeMemberRole('worker', 'admin')
  assert.equal(result.value.role, 'admin')
  assert.equal(result.currentScope, true)
  assert.equal(teams.getState().members.find(item => item.user_id === 'worker').role, 'admin')
  assert.deepEqual(requests, [['PATCH', '/api/team/team-a/members/worker/role'], ['GET', '/api/team'], ['GET', '/api/team/team-a/me'], ['GET', '/api/team/team-a/members/available']])
  assert.equal(teams.getState().memberMutation, null)
})

await test('member removal handles a real empty 204 response without a request body', async () => {
  selection.clear()
  signIn()
  const people = [member(), member('worker', 'employee')]
  globalThis.fetch = basicApi({ people })
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ people, remove: (url, init) => {
    assert.equal(url, '/api/team/team-a/members/worker')
    assert.equal(init.body, undefined)
    people.pop()
    return new Response(null, { status: 204 })
  } })
  const result = await teams.getState().removeMember('worker')
  assert.equal(result.value, undefined)
  assert.equal(result.currentScope, true)
  assert.deepEqual(teams.getState().members, [member()])
  assert.equal(teams.getState().memberMutation, null)
})

await test('one pending role write blocks add, role, and removal writes together', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  const pending = deferred()
  let writes = 0
  globalThis.fetch = basicApi({ patch: () => { writes++; return pending.promise } })
  const changing = teams.getState().changeMemberRole('worker', 'admin')
  assert.equal(teams.getState().memberMutation, 'role')
  await assert.rejects(teams.getState().addMember('another@example.invalid'), /重复提交/)
  await assert.rejects(teams.getState().removeMember('worker'), /重复提交/)
  await assert.rejects(teams.getState().changeMemberRole('worker', 'employee'), /重复提交/)
  assert.equal(writes, 1)
  pending.resolve(json(member('worker', 'admin')))
  await changing
  assert.equal(teams.getState().memberMutation, null)
})

await test('last-admin conflicts remain server-authoritative for both role and removal', async () => {
  for (const kind of ['role', 'remove']) {
    selection.clear()
    signIn()
    globalThis.fetch = basicApi()
    await teams.getState().refreshTeams('user-1')
    const reject = () => json({ code: 'LAST_ADMIN_REQUIRED', detail: 'last admin' }, 409)
    globalThis.fetch = basicApi({ patch: reject, remove: reject })
    const operation = kind === 'role' ? teams.getState().changeMemberRole('user-1', 'employee') : teams.getState().removeMember('user-1')
    let failed
    try { await operation } catch (error) { failed = error }
    assert.equal(failed.code, 'LAST_ADMIN_REQUIRED')
    assert.match(teamErrorMessage(failed, kind), /至少需要保留一名有效管理员/)
    assert.equal(teams.getState().membership.role, 'admin')
    assert.deepEqual(teams.getState().members, [member()])
    assert.equal(teams.getState().memberMutation, null)
  }
})

await test('a member-not-found error refreshes stale members and explains the conflict', async () => {
  selection.clear()
  signIn()
  const people = [member(), member('worker', 'employee')]
  globalThis.fetch = basicApi({ people })
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ people, remove: () => {
    people.pop()
    return json({ code: 'TEAM_MEMBER_NOT_FOUND', detail: 'missing' }, 404)
  } })
  let failed
  try { await teams.getState().removeMember('worker') } catch (error) { failed = error }
  assert.equal(failed.code, 'TEAM_MEMBER_NOT_FOUND')
  assert.match(teamErrorMessage(failed, 'remove'), /已不在当前团队/)
  assert.deepEqual(teams.getState().members, [member()])
  assert.equal(teams.getState().membership.role, 'admin')
})

await test('disabled role target does not revoke the valid administrator identity', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ patch: () => json({ code: 'ACCOUNT_DISABLED', detail: 'target disabled' }, 403) })
  let failed
  try { await teams.getState().changeMemberRole('worker', 'admin') } catch (error) { failed = error }
  assert.equal(teamErrorMessage(failed, 'role'), '该账号已停用，无法修改角色。')
  assert.equal(teams.getState().membership.role, 'admin')
  assert.equal(teams.getState().membership.team_name, '团队 team-a')
  assert.equal(teams.getState().memberMutation, null)
})

await test('successful self-demotion resolves successfully while dropping admin identity and member data', async () => {
  selection.clear()
  signIn()
  const choices = [membership()]
  const people = [member(), member('other-admin', 'admin')]
  globalThis.fetch = basicApi({ memberships: choices, people })
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ memberships: choices, people, patch: () => {
    choices[0] = membership('team-a', 'employee')
    return json(member('user-1', 'employee'))
  } })
  const result = await teams.getState().changeMemberRole('user-1', 'employee')
  assert.equal(result.value.role, 'employee')
  assert.equal(result.currentScope, false)
  assert.equal(teams.getState().membership.role, 'employee')
  assert.equal(teams.getState().error, '')
  assert.deepEqual(teams.getState().members, [])
  assert.equal(teams.getState().memberMutation, null)
})

await test('successful self-removal resolves successfully and retains the account session', async () => {
  selection.clear()
  signIn()
  const choices = [membership()]
  const people = [member(), member('other-admin', 'admin')]
  globalThis.fetch = basicApi({ memberships: choices, people })
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ memberships: choices, people, remove: () => {
    choices.length = 0
    return new Response(null, { status: 204 })
  } })
  const result = await teams.getState().removeMember('user-1')
  assert.equal(result.currentScope, false)
  assert.equal(teams.getState().membership, null)
  assert.deepEqual(teams.getState().members, [])
  assert.equal(teams.getState().status, 'ready')
  assert.equal(teams.getState().error, '')
  assert.equal(auth.getState().isAuthenticated, true)
  assert.equal(teams.getState().memberMutation, null)
})

await test('self-removal falling back to another team cannot show an old-team success notice there', async () => {
  selection.clear()
  signIn()
  const choices = [membership(), membership('team-b')]
  globalThis.fetch = basicApi({ memberships: choices, people: [member(), member('other-admin', 'admin')] })
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ memberships: choices, remove: () => {
    choices.shift()
    return new Response(null, { status: 204 })
  } })
  const result = await teams.getState().removeMember('user-1')
  assert.equal(result.currentScope, false)
  assert.equal(teams.getState().membership.team_id, 'team-b')
  assert.equal(teams.getState().membership.team_name, '团队 team-b')
  assert.equal(teams.getState().error, '')
})

await test('a successful late role response is isolated from a different account session', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  const pending = deferred()
  let signal
  globalThis.fetch = basicApi({ patch: (_url, init) => { signal = init.signal; return pending.promise } })
  const changing = teams.getState().changeMemberRole('worker', 'admin')
  signIn('user-2')
  globalThis.fetch = basicApi({ memberships: [membership('team-b', 'employee', 'user-2')] })
  await teams.getState().refreshTeams('user-2')
  pending.resolve(json(member('worker', 'admin')))
  assert.equal((await changing).currentScope, false)
  assert.equal(signal.aborted, true)
  assert.equal(teams.getState().membership.user_id, 'user-2')
  assert.equal(teams.getState().membership.team_id, 'team-b')
  assert.deepEqual(teams.getState().members, [])
})

await test('a late removal response cannot refresh or notify a newly selected team', async () => {
  selection.clear()
  signIn()
  const choices = [membership(), membership('team-b', 'employee')]
  globalThis.fetch = basicApi({ memberships: choices })
  await teams.getState().refreshTeams('user-1')
  const pending = deferred()
  const api = basicApi({ memberships: choices, remove: () => pending.promise })
  const requests = []
  globalThis.fetch = (url, init) => { requests.push(url); return api(url, init) }
  const removing = teams.getState().removeMember('worker')
  await teams.getState().selectTeam('team-b')
  const callsAfterSwitch = requests.length
  pending.resolve(new Response(null, { status: 204 }))
  assert.equal((await removing).currentScope, false)
  assert.equal(requests.length, callsAfterSwitch)
  assert.equal(teams.getState().membership.team_id, 'team-b')
  assert.equal(teams.getState().memberMutation, null)
})

await test('post-write refresh supersedes an older identity refresh instead of restoring old privileges', async () => {
  selection.clear()
  signIn()
  const choices = [membership()]
  globalThis.fetch = basicApi({ memberships: choices, people: [member(), member('other-admin', 'admin')] })
  await teams.getState().refreshTeams('user-1')
  const pendingWrite = deferred()
  const pendingOldRead = deferred()
  let reads = 0
  const api = basicApi({ memberships: choices, patch: () => pendingWrite.promise })
  globalThis.fetch = (url, init) => {
    if (url === '/api/team' && ++reads === 1) return pendingOldRead.promise
    return api(url, init)
  }
  const changing = teams.getState().changeMemberRole('user-1', 'employee')
  const oldRefresh = teams.getState().refreshTeams('user-1')
  choices[0] = membership('team-a', 'employee')
  pendingWrite.resolve(json(member('user-1', 'employee')))
  assert.equal((await changing).currentScope, false)
  assert.equal(teams.getState().membership.role, 'employee')
  pendingOldRead.resolve(json([membership()]))
  await oldRefresh
  assert.equal(teams.getState().membership.role, 'employee')
  assert.deepEqual(teams.getState().members, [])
  assert.equal(reads, 2)
})

await test('employees cannot start any member mutation even with a forged login admin role', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi({ memberships: [membership('team-a', 'employee')] })
  await teams.getState().refreshTeams('user-1')
  let writes = 0
  globalThis.fetch = () => { writes++; throw new Error('employee must not write') }
  await assert.rejects(teams.getState().addMember('new@example.invalid'), /管理员/)
  await assert.rejects(teams.getState().changeMemberRole('worker', 'admin'), /管理员/)
  await assert.rejects(teams.getState().removeMember('worker'), /管理员/)
  assert.equal(writes, 0)
})

await test('open-ticket removal conflict explains the required handoff without changing member access', async () => {
  selection.clear()
  signIn()
  const people = [member(), member('worker', 'employee')]
  globalThis.fetch = basicApi({ people })
  await teams.getState().refreshTeams('user-1')
  globalThis.fetch = basicApi({ people, remove: () => json({ code: 'MEMBER_HAS_OPEN_TICKETS', detail: 'open tickets' }, 409) })
  let failed
  try { await teams.getState().removeMember('worker') } catch (error) { failed = error }
  assert.equal(failed.status, 409)
  assert.match(teamErrorMessage(failed, 'remove'), /未完成的处理或审核工单/)
  assert.match(teamErrorMessage(failed, 'remove'), /交接/)
  assert.deepEqual(teams.getState().members, people)
  assert.equal(teams.getState().memberMutation, null)
})

await test('forced read preflight bypasses an older identity request and cannot restore its admin scope', async () => {
  selection.clear()
  signIn()
  globalThis.fetch = basicApi()
  await teams.getState().refreshTeams('user-1')
  const pendingOldRead = deferred()
  let teamReads = 0
  const freshApi = basicApi({ memberships: [membership('team-a', 'employee')] })
  globalThis.fetch = (url, init) => {
    if (url === '/api/team' && ++teamReads === 1) return pendingOldRead.promise
    return freshApi(url, init)
  }
  const oldIdentity = teams.getState().refreshTeams('user-1')
  await teams.getState().refreshTeams('user-1', true)
  assert.equal(teams.getState().membership.role, 'employee')
  pendingOldRead.resolve(json([membership()]))
  await oldIdentity
  assert.equal(teamReads, 2)
  assert.equal(teams.getState().membership.role, 'employee')
  assert.deepEqual(teams.getState().members, [])
})
