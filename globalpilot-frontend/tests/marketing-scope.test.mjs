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
const marketing = await import('../src/api/marketing.ts')
const { createBrief, listBriefs } = await import('../src/api/briefs.ts')
const { listHardwareBrands } = await import('../src/api/documents.ts')
const { getBriefMemories } = await import('../src/api/memories.ts')
const { resolveMarketingBriefs } = await import('../src/pages/Marketing/useMarketingScope.ts')
const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})
const workflow = {
  brief_id: 'brief-a', checkpoint_id: 'head-a', status: 'awaiting_review',
  revision_count: 0, content: null, strategy: 'Original strategy', final_output: null,
}
const stream = (value = workflow) => new Response(JSON.stringify({ type: 'done', workflow: value }) + '\n', {
  headers: { 'Content-Type': 'application/x-ndjson' },
})
configureAuth({ getAccessToken: () => 'scoped-marketing-token', refreshAccessToken: async () => null, onUnauthorized() {} })

function inspect(url, init) {
  assert.equal(init.credentials, 'include')
  assert.equal(init.headers.get('Authorization'), 'Bearer scoped-marketing-token')
  return new URL(url, 'http://test')
}

const briefRecord = (id, overrides = {}) => ({
  id, user_id: 'user-a', team_id: 'team-a', brand_id: 'brand-a',
  goal: 'Reach new customers', selling_points: 'Compact and portable', region: 'eu-na',
  user_persona: 'Commuters', social_media: ['instagram'], purchase_channels: null,
  competitor_url: null, created_at: '2026-11-05T00:00:00Z', updated_at: '2026-11-05T00:00:00Z',
  ...overrides,
})
const chooseBrief = (records, overrides = {}) => resolveMarketingBriefs(records, {
  userId: 'user-a', teamId: 'team-a', requestedId: null, savedId: null, ...overrides,
})

await test('Research brand loading scopes the actual HTTP request and preserves legacy callers', async () => {
  const controller = new AbortController()
  const calls = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    calls.push({ path: parsed.pathname, team: parsed.searchParams.get('team_id'), signal: init.signal })
    return json([{ id: 'brand-a', team_id: 'team / a', name: 'Wearables', created_at: '2026-11-05' }])
  }
  assert.equal((await listHardwareBrands('team / a', controller.signal))[0].team_id, 'team / a')
  await listHardwareBrands()
  assert.deepEqual(calls, [
    { path: '/api/brands', team: 'team / a', signal: controller.signal },
    { path: '/api/brands', team: null, signal: undefined },
  ])
})

await test('a newly created location Brief opens ahead of saved history even before its workflow is listed', () => {
  const saved = briefRecord('saved')
  const fresh = briefRecord('fresh')
  const records = [saved]
  const result = chooseBrief(records, { savedId: saved.id, locationBrief: fresh })
  assert.equal(result.selectedId, fresh.id)
  assert.deepEqual(result.records.map((item) => item.id), ['fresh', 'saved'])
  assert.deepEqual(records, [saved], 'selection does not mutate the server result')
  assert.equal(result.error, '')
})

await test('an explicit URL Brief takes precedence over a location receipt and saved history', () => {
  const result = chooseBrief([briefRecord('url'), briefRecord('saved')], {
    requestedId: 'url', savedId: 'saved', locationBrief: briefRecord('fresh'),
  })
  assert.equal(result.selectedId, 'url')
})

await test('a remembered Brief cannot override the saved account and team selection', () => {
  const result = chooseBrief([briefRecord('selected')], {
    savedId: 'selected', rememberedBrief: briefRecord('remembered'),
  })
  assert.equal(result.selectedId, 'selected')
  assert.deepEqual(result.records.map((item) => item.id), ['remembered', 'selected'])
})

await test('a valid remembered receipt remains usable when an unstarted Brief is absent from history', () => {
  const result = chooseBrief([], { rememberedBrief: briefRecord('unstarted') })
  assert.equal(result.selectedId, 'unstarted')
})

await test('location and remembered receipts from a different actor or team never enter the current list', () => {
  const result = chooseBrief([briefRecord('own')], {
    locationBrief: briefRecord('foreign-user', { user_id: 'user-b' }),
    rememberedBrief: briefRecord('foreign-team', { team_id: 'team-b' }),
  })
  assert.equal(result.selectedId, 'own')
  assert.deepEqual(result.records.map((item) => item.id), ['own'])
})

await test('a server list containing another account or team is rejected rather than selecting a draft', () => {
  for (const overrides of [{ user_id: 'user-b' }, { team_id: 'team-b' }]) {
    assert.throws(() => chooseBrief([briefRecord('foreign', overrides)]),
      (error) => error instanceof ApiError && error.status === 403)
  }
})

await test('an inaccessible explicit URL never silently selects or starts a different remembered draft', () => {
  const result = chooseBrief([briefRecord('saved')], {
    requestedId: 'missing', savedId: 'saved', locationBrief: briefRecord('fresh'),
    rememberedBrief: briefRecord('remembered'),
  })
  assert.equal(result.selectedId, '')
  assert.match(result.error, /未找到.*没有访问权限/)
})

await test('a stale receipt does not replace the current server Brief with the same id', () => {
  const current = briefRecord('same', { goal: 'Current server goal' })
  const result = chooseBrief([current], {
    locationBrief: briefRecord('same', { goal: 'Old navigation receipt' }),
    rememberedBrief: briefRecord('same', { goal: 'Old remembered receipt' }),
  })
  assert.equal(result.records.length, 1)
  assert.equal(result.records[0], current)
  assert.equal(result.selectedId, current.id)
})

await test('workflow GET forwards explicit team and brief identities without generating a draft', async () => {
  let calls = 0
  globalThis.fetch = async (url, init) => {
    calls += 1
    const parsed = inspect(url, init)
    assert.equal(parsed.pathname, '/api/briefs/workflow')
    assert.equal(parsed.searchParams.get('brief_id'), 'brief / a')
    assert.equal(parsed.searchParams.get('team_id'), 'team / a')
    assert.equal(init.method, undefined)
    return json(workflow)
  }
  assert.deepEqual(await marketing.getMarketingWorkflow('brief / a', 'team / a'), workflow)
  assert.equal(calls, 1)
})

await test('regular workflow start and review preserve team query, checkpoint, action and feedback', async () => {
  const requests = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    assert.equal(parsed.searchParams.get('team_id'), 'team-a')
    assert.equal(init.method, 'POST')
    requests.push({ path: parsed.pathname, body: JSON.parse(init.body) })
    return json(workflow)
  }
  await marketing.startMarketingWorkflow('brief-a', 'team-a')
  await marketing.reviewMarketingWorkflow('brief-a', 'rejected', 'head-shown', '  Please shorten it.  ', 'team-a')
  assert.deepEqual(requests, [
    { path: '/api/briefs/workflow', body: { brief_id: 'brief-a' } },
    { path: '/api/briefs/workflow/review', body: { brief_id: 'brief-a', action: 'rejected', expected_checkpoint_id: 'head-shown', feedback: 'Please shorten it.' } },
  ])
})

await test('NDJSON start and review forward team scope through their real stream parser', async () => {
  const requests = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    assert.equal(parsed.searchParams.get('team_id'), 'team & a')
    assert.equal(init.headers.get('Accept'), 'application/x-ndjson')
    requests.push({ path: parsed.pathname, body: JSON.parse(init.body) })
    return stream()
  }
  assert.deepEqual(await marketing.streamStartWorkflow('brief-a', undefined, 'team & a'), workflow)
  assert.deepEqual(await marketing.streamReviewWorkflow('brief-a', 'approved', 'head-shown', null, undefined, 'team & a'), workflow)
  assert.equal(requests[0].path, '/api/briefs/workflow/stream')
  assert.deepEqual(requests[1], {
    path: '/api/briefs/workflow/review/stream',
    body: { brief_id: 'brief-a', action: 'approved', expected_checkpoint_id: 'head-shown', feedback: null },
  })
})

await test('loading an existing workflow only reads the scoped current checkpoint', async () => {
  const requests = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    requests.push({ path: parsed.pathname, team: parsed.searchParams.get('team_id'), method: init.method })
    return json(workflow)
  }
  let starts = 0
  assert.deepEqual(await marketing.loadOrStartWorkflow('brief-a', undefined, () => { starts += 1 }, 'team-a'), workflow)
  assert.equal(starts, 0)
  assert.deepEqual(requests, [{ path: '/api/briefs/workflow', team: 'team-a', method: undefined }])
})

await test('missing workflow fallback retains the same explicit team in GET and generation POST', async () => {
  const requests = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    requests.push({ path: parsed.pathname, team: parsed.searchParams.get('team_id'), method: init.method })
    return requests.length === 1 ? json({ detail: '尚未生成营销流程', code: 'WORKFLOW_NOT_FOUND' }, 404) : stream()
  }
  assert.deepEqual(await marketing.loadOrStartWorkflow('fallback-brief', undefined, undefined, 'fallback-team'), workflow)
  assert.deepEqual(requests, [
    { path: '/api/briefs/workflow', team: 'fallback-team', method: undefined },
    { path: '/api/briefs/workflow/stream', team: 'fallback-team', method: 'POST' },
  ])
})

await test('unavailable stream endpoint fallback keeps scope when invoking regular start', async () => {
  const requests = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    requests.push({ path: parsed.pathname, team: parsed.searchParams.get('team_id'), method: init.method })
    if (requests.length === 1) return json({ detail: 'No saved workflow' }, 404)
    if (requests.length === 2) return json({ detail: 'Not Found' }, 404)
    return json(workflow)
  }
  assert.deepEqual(await marketing.loadOrStartWorkflow('legacy-stream-brief', undefined, undefined, 'team-legacy'), workflow)
  assert.deepEqual(requests.map(item => item.team), ['team-legacy', 'team-legacy', 'team-legacy'])
  assert.deepEqual(requests.map(item => item.path), ['/api/briefs/workflow', '/api/briefs/workflow/stream', '/api/briefs/workflow'])
  assert.equal(requests[2].method, 'POST')
})

await test('a scoped workflow GET 503/403 does not trigger generation as a side effect', async () => {
  for (const status of [503, 403]) {
    let calls = 0
    let starts = 0
    globalThis.fetch = async (url, init) => {
      calls += 1
      const parsed = inspect(url, init)
      assert.equal(parsed.searchParams.get('team_id'), 'team-safe')
      assert.equal(init.method, undefined)
      return json({ detail: 'Cannot read this workflow', code: status === 503 ? 'SERVICE_UNAVAILABLE' : 'FORBIDDEN' }, status)
    }
    await assert.rejects(
      marketing.loadOrStartWorkflow('unavailable-brief', undefined, () => { starts += 1 }, 'team-safe'),
      error => error instanceof ApiError && error.status === status,
    )
    assert.equal(calls, 1)
    assert.equal(starts, 0)
  }
})

await test('a late old-scope GET 404/409 cannot start generation after its page scope changes', async () => {
  for (const status of [404, 409]) {
    let current = true
    let release
    let starts = 0
    const requests = []
    globalThis.fetch = async (url, init) => {
      const parsed = inspect(url, init)
      requests.push({ path: parsed.pathname, method: init.method })
      if (init.method) return stream()
      return new Promise(resolve => { release = resolve })
    }
    const pending = marketing.loadOrStartWorkflow(
      `old-scope-${status}`, undefined, () => { starts += 1 }, 'old-team', () => current,
    )
    assert.equal(typeof release, 'function')
    current = false
    release(json({ detail: '旧范围的读取结果已返回' }, status))
    await assert.rejects(pending, error => error instanceof ApiError && error.status === 0 && /取消/.test(error.message))
    assert.equal(starts, 0)
    assert.deepEqual(requests, [{ path: '/api/briefs/workflow', method: undefined }])
  }
})

await test('scoped review stream fallback preserves the original decision and displayed checkpoint', async () => {
  const requests = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    requests.push({ path: parsed.pathname, team: parsed.searchParams.get('team_id'), body: JSON.parse(init.body) })
    return requests.length === 1 ? json({ detail: 'Not Found' }, 405) : json(workflow)
  }
  assert.deepEqual(await marketing.reviewOrStreamWorkflow('brief-a', 'approved', 'head-shown', null, undefined, 'review-team'), workflow)
  assert.equal(requests.length, 2)
  assert.deepEqual(requests.map(item => item.team), ['review-team', 'review-team'])
  assert.deepEqual(requests[0].body, requests[1].body)
  assert.equal(requests[1].body.expected_checkpoint_id, 'head-shown')
})

await test('a scoped review conflict is not retried through a different endpoint', async () => {
  let calls = 0
  globalThis.fetch = async (url, init) => {
    calls += 1
    assert.equal(inspect(url, init).searchParams.get('team_id'), 'review-team')
    return json({ detail: '稿件已更新', code: 'CONFLICT' }, 409)
  }
  await assert.rejects(marketing.reviewOrStreamWorkflow('brief-a', 'approved', 'stale-head', null, undefined, 'review-team'), error => error.status === 409)
  assert.equal(calls, 1)
})

await test('a late old-scope stream endpoint failure cannot retry review through the ordinary POST endpoint', async () => {
  for (const status of [404, 405]) {
    let current = true
    let release
    const paths = []
    globalThis.fetch = async (url, init) => {
      const parsed = inspect(url, init)
      paths.push(parsed.pathname)
      if (!parsed.pathname.endsWith('/stream')) return json(workflow)
      return new Promise(resolve => { release = resolve })
    }
    const pending = marketing.reviewOrStreamWorkflow(
      `old-review-${status}`, 'approved', 'old-head', null, undefined, 'old-team', () => current,
    )
    assert.equal(typeof release, 'function')
    current = false
    release(json({ detail: 'Not Found' }, status))
    await assert.rejects(pending, error => error instanceof ApiError && error.status === 0 && /取消/.test(error.message))
    assert.deepEqual(paths, ['/api/briefs/workflow/review/stream'])
  }
})

await test('saved briefs and memory requests preserve team scope and cancellation signal', async () => {
  const controller = new AbortController()
  const requests = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    assert.equal(parsed.searchParams.get('team_id'), 'team-a')
    assert.equal(init.signal, controller.signal)
    requests.push(parsed.pathname)
    return json(requests.length === 1 ? [] : { items: [], jobs: [] })
  }
  assert.deepEqual(await listBriefs('team-a', controller.signal), [])
  assert.deepEqual(await getBriefMemories('brief / a', controller.signal, 'team-a'), { items: [], jobs: [] })
  assert.deepEqual(requests, ['/api/briefs', '/api/briefs/brief%20%2F%20a/memories'])
})

await test('brief creation supplies team only in the authorized query scope', async () => {
  const payload = { brand_id: 'brand-a', goal: 'Marketing goal', selling_points: '65W', region: 'eu-na', user_persona: 'Students', social_media: ['linkedin'] }
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    assert.equal(parsed.pathname, '/api/briefs')
    assert.equal(parsed.searchParams.get('team_id'), 'team / a')
    assert.equal(init.method, 'POST')
    assert.deepEqual(JSON.parse(init.body), payload)
    return json({ ...payload, id: 'brief-created' })
  }
  assert.equal((await createBrief(payload, 'team / a')).id, 'brief-created')
})

await test('concurrent starts of the same brief in different teams never share an in-flight request', async () => {
  const posts = []
  globalThis.fetch = async (url, init) => {
    const parsed = inspect(url, init)
    if (!init.method) return json({ detail: 'No saved workflow' }, 404)
    posts.push(parsed.searchParams.get('team_id'))
    return stream({ ...workflow, strategy: parsed.searchParams.get('team_id') })
  }
  const [first, second] = await Promise.all([
    marketing.loadOrStartWorkflow('same-brief', undefined, undefined, 'team-one'),
    marketing.loadOrStartWorkflow('same-brief', undefined, undefined, 'team-two'),
  ])
  assert.deepEqual(posts.sort(), ['team-one', 'team-two'])
  assert.equal(first.strategy, 'team-one')
  assert.equal(second.strategy, 'team-two')
})

await test('existing calls without a team parameter remain backward compatible', async () => {
  const paths = []
  globalThis.fetch = async (url, init) => {
    paths.push(String(url))
    assert.equal(new URL(url, 'http://test').searchParams.has('team_id'), false)
    if (String(url).includes('/memories')) return json({ items: [], jobs: [] })
    return json(init.method ? workflow : [])
  }
  await marketing.startMarketingWorkflow('brief-a')
  await listBriefs()
  await getBriefMemories('brief-a')
  assert.deepEqual(paths, ['/api/briefs/workflow', '/api/briefs', '/api/briefs/brief-a/memories'])
})
