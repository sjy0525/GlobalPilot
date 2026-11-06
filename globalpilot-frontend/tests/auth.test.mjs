// Node 24: execute the real TypeScript store/request code with fake HTTP responses.
import { registerHooks } from 'node:module'
import test from 'node:test'
import assert from 'node:assert/strict'
registerHooks({ resolve(specifier, context, nextResolve) {
  try { return nextResolve(specifier, context) } catch (error) {
    if (specifier.startsWith('.') && !specifier.match(/\.[a-z]+$/)) return nextResolve(`${specifier}.ts`, context)
    throw error
  }
} })
const { apiJson } = await import('../src/api/http.ts')
let sequence = 0
let removed = []
globalThis.window = { localStorage: {
  removeItem: (key) => removed.push(key),
  getItem: () => { throw new Error('Must not read stored tokens') },
  setItem: () => { throw new Error('Must not persist tokens') },
} }
const session = (access_token = 'access-new') => ({ access_token, token_type: 'bearer', user: { id: '1', email: 'a@b.com', name: 'A' } })
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
async function store() { return (await import(`../src/store/authStore.ts?test=${++sequence}`)).useAuthStore }
function deferred() { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

await test('login keeps only access token in memory and includes cookie credentials', async () => {
  removed = []
  const auth = await store()
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/auth/login')
    assert.equal(init.credentials, 'include')
    assert.equal(init.headers.get('Authorization'), null)
    return json(session())
  }
  await auth.getState().loginWithEmail('a@b.com', 'password')
  assert.equal(auth.getState().token, 'access-new')
  assert.equal('refreshToken' in auth.getState(), false)
  assert.deepEqual(removed, ['gp-auth'])
})

await test('page reload starts without a token and shares the cookie restore request', async () => {
  const auth = await store()
  assert.equal(auth.getState().token, null)
  assert.equal(auth.getState().ready, false)
  let calls = 0
  globalThis.fetch = async (url, init) => {
    calls++
    assert.equal(url, '/api/auth/refresh')
    assert.equal(init.body, undefined)
    assert.equal(init.credentials, 'include')
    assert.equal(init.headers.get('Authorization'), null)
    return json(session())
  }
  await Promise.all([auth.getState().restoreSession(), auth.getState().restoreSession()])
  assert.equal(calls, 1)
  assert.equal(auth.getState().isAuthenticated, true)
})

await test('business requests use memory Bearer token; concurrent 401s share refresh and retry', async () => {
  const auth = await store()
  auth.setState({ token: 'expired', user: session().user, isAuthenticated: true, ready: true })
  let refreshes = 0
  let successes = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/auth/refresh') { refreshes++; return json(session()) }
    if (init.headers.get('Authorization') === 'Bearer expired') return json({ detail: 'expired' }, 401)
    assert.equal(init.headers.get('Authorization'), 'Bearer access-new')
    successes++
    return json({ ok: true })
  }
  await Promise.all([apiJson('/api/briefs'), apiJson('/api/briefs')])
  assert.equal(refreshes, 1)
  assert.equal(successes, 2)
})

await test('missing cookie ends bootstrap unauthenticated without recursive refresh', async () => {
  const auth = await store()
  let calls = 0
  globalThis.fetch = async () => { calls++; return json({ detail: 'login required' }, 401) }
  await auth.getState().restoreSession()
  assert.equal(calls, 1)
  assert.equal(auth.getState().ready, true)
  assert.equal(auth.getState().isAuthenticated, false)
})

await test('logout calls server, clears memory, and late refresh cannot log back in', async () => {
  const auth = await store()
  const pending = deferred()
  globalThis.fetch = async (url, init) => {
    if (url === '/api/auth/refresh') return pending.promise
    assert.equal(url, '/api/auth/logout')
    assert.equal(init.credentials, 'include')
    return new Response(null, { status: 204 })
  }
  const restoring = auth.getState().restoreSession()
  await auth.getState().logout()
  pending.resolve(json(session()))
  await restoring
  assert.equal(auth.getState().token, null)
  assert.equal(auth.getState().isAuthenticated, false)
})

await test('logout failure is surfaced rather than pretending HttpOnly cookie was deleted', async () => {
  const auth = await store()
  auth.setState({ token: 'access', user: session().user, isAuthenticated: true, ready: true })
  globalThis.fetch = async () => { throw new Error('offline') }
  await assert.rejects(auth.getState().logout())
  assert.equal(auth.getState().isAuthenticated, true)
})

await test('late bootstrap cannot replace a newer login', async () => {
  const auth = await store()
  const pending = deferred()
  globalThis.fetch = async (url) => url === '/api/auth/refresh' ? pending.promise : json(session('new-login'))
  const restoring = auth.getState().restoreSession()
  await auth.getState().loginWithEmail('a@b.com', 'password')
  pending.resolve(json(session('old-session')))
  await restoring
  assert.equal(auth.getState().token, 'new-login')
})

await test('expired refresh cookie clears memory without an infinite 401 retry', async () => {
  const auth = await store()
  auth.setState({ token: 'expired', user: session().user, isAuthenticated: true, ready: true })
  let calls = 0
  globalThis.fetch = async () => { calls++; return json({ detail: 'expired' }, 401) }
  await assert.rejects(apiJson('/api/briefs'))
  assert.equal(calls, 2)
  assert.equal(auth.getState().token, null)
  assert.equal(auth.getState().isAuthenticated, false)
})

await test('temporary refresh network failure does not destroy the memory session', async () => {
  const auth = await store()
  auth.setState({ token: 'old-access', user: session().user, isAuthenticated: true, ready: true })
  globalThis.fetch = async (url) => {
    if (url === '/api/auth/refresh') throw new Error('offline')
    return json({ detail: 'expired' }, 401)
  }
  await assert.rejects(apiJson('/api/briefs'))
  assert.equal(auth.getState().token, 'old-access')
})
