// Node 24 executes the real TypeScript request and workflow code without a server.
import { registerHooks } from 'node:module'
import test from 'node:test'
import assert from 'node:assert/strict'

registerHooks({ resolve(specifier, context, nextResolve) {
  try { return nextResolve(specifier, context) } catch (error) {
    if (specifier.startsWith('.') && !specifier.match(/\.[a-z]+$/)) {
      return nextResolve(`${specifier}.ts`, context)
    }
    throw error
  }
} })

const { ApiError, parseApiResponse, readNdjson } = await import('../src/api/http.ts')
const { streamReviewWorkflow, streamStartWorkflow } = await import('../src/api/marketing.ts')

const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})
const ndjson = (body) => new Response(body, {
  status: 200,
  headers: { 'Content-Type': 'application/x-ndjson' },
})

await test('HTTP errors preserve the response status, code, and detail', async () => {
  await assert.rejects(
    parseApiResponse(json({ detail: '当前不在待审流程', code: 'WORKFLOW_CONFLICT' }, 409)),
    error => error instanceof ApiError && error.status === 409 &&
      error.code === 'WORKFLOW_CONFLICT' && error.message === '当前不在待审流程',
  )
})

await test('legacy HTTP validation errors still extract readable messages without a code', async () => {
  await assert.rejects(
    parseApiResponse(json({ detail: [{ msg: 'Feedback is required' }] }, 422)),
    error => error instanceof ApiError && error.status === 422 &&
      error.code === undefined && error.message === 'Feedback is required',
  )
})

await test('structured revision format conflicts expose message and concrete issues', async () => {
  await assert.rejects(
    parseApiResponse(json({ detail: { message: '改稿不符合完整文案格式', issues: ['正文过长', '缺少标签'] }, code: 'CONFLICT' }, 409)),
    error => error instanceof ApiError && error.status === 409 && error.code === 'CONFLICT' &&
      error.message === '改稿不符合完整文案格式 正文过长 缺少标签',
  )
})

await test('structured parent review failures show review errors without dumping internal payload', async () => {
  await assert.rejects(
    parseApiResponse(json({ detail: {
      message: '改稿未通过完整营销文案检查',
      review: { errors: ['产品事实缺少支持'], facts: [{ evidence: 'internal record' }] },
      private: 'must not be exposed',
    } }, 409)),
    error => error.message === '改稿未通过完整营销文案检查 产品事实缺少支持' && !error.message.includes('internal'),
  )
})

await test('unrecognized structured error bodies do not leak arbitrary fields', async () => {
  await assert.rejects(
    parseApiResponse(json({ detail: { private: 'secret upstream payload' } }, 502)),
    error => error.status === 502 && error.message === 'Request failed.',
  )
})

await test('NDJSON ignores malformed JSON and proxy padding but delivers valid frames', async () => {
  const values = []
  await readNdjson(ndjson('{invalid}\n{     }\n{"type":"done"}\n'), value => values.push(value))
  assert.deepEqual(values, [{}, { type: 'done' }])
})

await test('NDJSON propagates callback errors, including SyntaxError', async () => {
  for (const failure of [new ApiError('文案尚未通过自审', 409, 'REVIEW_FAILED'), new SyntaxError('callback failure')]) {
    await assert.rejects(
      readNdjson(ndjson('{"type":"error"}\n'), () => { throw failure }),
      error => error === failure,
    )
  }
})

await test('a final NDJSON frame without a newline also propagates callback errors', async () => {
  const failure = new ApiError('任务已删除', 404, 'BRIEF_NOT_FOUND')
  await assert.rejects(
    readNdjson(ndjson('{"type":"error"}'), () => { throw failure }),
    error => error === failure,
  )
})

await test('a callback failure cancels the remaining stream and releases its reader', async () => {
  let cancelled = false
  const response = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('{"type":"error"}\n'))
    },
    cancel() { cancelled = true },
  }))
  const failure = new ApiError('需要重新审核', 409, 'REVIEW_REQUIRED')
  await assert.rejects(readNdjson(response, () => { throw failure }), error => error === failure)
  assert.equal(cancelled, true)
  assert.equal(response.body.locked, false)
})

await test('workflow stream failures preserve the real status, code, and detail', async () => {
  globalThis.fetch = async () => ndjson(JSON.stringify({
    type: 'error', status: 409, code: 'CONTENT_REVIEW_NOT_PASSED', detail: '文案尚未通过模型自审，请先退回修改',
  }) + '\n')
  await assert.rejects(
    streamReviewWorkflow('brief-id', 'approved', 'checkpoint-0', null),
    error => error instanceof ApiError && error.status === 409 &&
      error.code === 'CONTENT_REVIEW_NOT_PASSED' && error.message === '文案尚未通过模型自审，请先退回修改',
  )
})

await test('legacy stream failures retain their detail and default to 500', async () => {
  globalThis.fetch = async () => ndjson('{"type":"error","detail":"Workflow failed."}\n')
  await assert.rejects(
    streamStartWorkflow('brief-id'),
    error => error instanceof ApiError && error.status === 500 &&
      error.code === undefined && error.message === 'Workflow failed.',
  )
})

await test('invalid status values in stream errors safely fall back to 500', async () => {
  for (const status of [200, 399, 600, 409.5, '409', null]) {
    globalThis.fetch = async () => ndjson(JSON.stringify({ type: 'error', status, detail: 'Failure' }) + '\n')
    await assert.rejects(streamStartWorkflow('brief-id'), error => error instanceof ApiError && error.status === 500)
  }
})

await test('workflow errors before streaming preserve the actual HTTP status and code', async () => {
  globalThis.fetch = async () => json({ detail: '营销任务不存在', code: 'BRIEF_NOT_FOUND' }, 404)
  await assert.rejects(
    streamStartWorkflow('brief-id'),
    error => error instanceof ApiError && error.status === 404 &&
      error.code === 'BRIEF_NOT_FOUND' && error.message === '营销任务不存在',
  )
})

await test('successful workflow streams continue returning their done result', async () => {
  const workflow = { brief_id: 'brief-id', status: 'no_knowledge', revision_count: 0,
    content: null, strategy: null, final_output: '品牌知识库里暂时没有产品的相关信息' }
  globalThis.fetch = async () => ndjson('{invalid}\n' + JSON.stringify({ type: 'done', workflow }))
  assert.deepEqual(await streamStartWorkflow('brief-id'), workflow)
})

await test('review sends the checkpoint displayed to the user', async () => {
  let sent
  const workflow = { brief_id: 'brief-id', checkpoint_id: 'checkpoint-next', status: 'completed',
    revision_count: 0, content: null, strategy: null, final_output: 'Done' }
  globalThis.fetch = async (_url, init) => {
    sent = JSON.parse(init.body)
    return ndjson(JSON.stringify({ type: 'done', workflow }) + '\n')
  }
  await streamReviewWorkflow('brief-id', 'approved', 'checkpoint-seen', null)
  assert.equal(sent.expected_checkpoint_id, 'checkpoint-seen')
  assert.equal(sent.action, 'approved')
})

await test('reloading an interrupted workflow recovers execution after a 409 snapshot', async () => {
  const { loadOrStartWorkflow } = await import('../src/api/marketing.ts')
  const paths = []
  const workflow = { brief_id: 'recover-brief', checkpoint_id: 'recovered', status: 'completed',
    revision_count: 0, content: null, strategy: null, final_output: 'Done' }
  globalThis.fetch = async url => {
    paths.push(String(url))
    return paths.length === 1
      ? json({ detail: '流程等待恢复', code: 'CONFLICT' }, 409)
      : ndjson(JSON.stringify({ type: 'done', workflow }) + '\n')
  }
  assert.deepEqual(await loadOrStartWorkflow('recover-brief'), workflow)
  assert.equal(paths.length, 2)
  assert.ok(paths[1].endsWith('/api/briefs/workflow/stream'))
})

await test('document deletion sends the version displayed to the user', async () => {
  const { deleteBrandDocument } = await import('../src/api/documents.ts')
  let sent
  globalThis.fetch = async (url, init) => {
    sent = { url: String(url), method: init.method }
    return json({ id: 'document', deleted_chunks: 2, version: 7 })
  }
  await deleteBrandDocument('brand', 'document', 7)
  assert.equal(sent.method, 'DELETE')
  assert.equal(new URL(sent.url, 'http://test').searchParams.get('expected_version'), '7')
})
