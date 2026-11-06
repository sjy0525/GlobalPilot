import { registerHooks } from 'node:module'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

registerHooks({
  resolve(specifier, context, nextResolve) {
    try { return nextResolve(specifier, context) } catch (error) {
      if (specifier.startsWith('.') && !specifier.match(/\.[a-z]+$/)) {
        return nextResolve(`${specifier}.ts`, context)
      }
      throw error
    }
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.tsx')) {
      const source = ts.transpileModule(readFileSync(new URL(url), 'utf8'), {
        compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
      }).outputText
      return { format: 'module', source, shortCircuit: true }
    }
    return nextLoad(url, context)
  },
})

const { isGraphNode, streamStartWorkflow } = await import('../src/api/marketing.ts')
const { NODE_COPY, FALLBACK_SEQUENCE, meterTarget, nodeToPipeline } = await import('../src/pages/Marketing/pipeline.ts')
const { ResearchCard } = await import('../src/pages/Marketing/Research.tsx')

const research = { status: 'completed', summary: 'A campus library lends chargers.',
  source_urls: ['https://library.example.edu/chargers'], uncertainties: ['One campus only.'] }
const validation = { passed: true, issues: [] }

await test('research node has a readable stage and runs before strategy', () => {
  assert.equal(isGraphNode('research_agent'), true)
  assert.equal(nodeToPipeline('research_agent'), 'knowledge')
  assert.match(NODE_COPY.research_agent.title, /Research/)
  assert.ok(meterTarget('research_agent', 'running', undefined, true) <
    meterTarget('generate_strategy', 'running', undefined, true))
  assert.ok(FALLBACK_SEQUENCE.indexOf('research_agent') < FALLBACK_SEQUENCE.indexOf('generate_strategy'))
})

await test('real NDJSON parser delivers research and validation before strategy', async () => {
  const workflow = { brief_id: 'research-brief', status: 'awaiting_review', research,
    research_validation: validation, content: null, strategy: 'Plan', final_output: null,
    checkpoint_id: 'checkpoint-research', revision_count: 0 }
  const frames = [
    { type: 'node', node: 'research_agent', phase: 'running' },
    { type: 'node', node: 'research_agent', phase: 'done', research, research_validation: validation },
    { type: 'node', node: 'generate_strategy', phase: 'running' },
    { type: 'done', workflow },
  ]
  globalThis.fetch = async () => new Response(frames.map(frame => JSON.stringify(frame)).join('\n') + '\n', {
    headers: { 'Content-Type': 'application/x-ndjson' },
  })
  const received = []
  const result = await streamStartWorkflow('research-brief', event => received.push(event))
  assert.equal(received[0].node, 'research_agent')
  assert.equal(received[1].phase, 'done')
  assert.deepEqual(received[1].research, research)
  assert.deepEqual(received[1].research_validation, validation)
  assert.deepEqual(result, workflow)
})

await test('report card shows sources and limitations without requiring raw audit JSON', () => {
  const html = renderToStaticMarkup(createElement(ResearchCard, { report: research, validation }))
  assert.match(html, /Research evidence checked/)
  assert.match(html, /A campus library lends chargers/)
  assert.match(html, /href="https:\/\/library.example.edu\/chargers"/)
  assert.match(html, /rel="noopener noreferrer"/)
  assert.match(html, /One campus only/)
})

await test('failed or absent review is explicit and duplicate issues appear once', () => {
  const report = { ...research, status: 'insufficient', uncertainties: ['Need local evidence.'] }
  const html = renderToStaticMarkup(createElement(ResearchCard, { report,
    validation: { passed: false, issues: ['Need local evidence.'] } }))
  assert.match(html, /Research has gaps/)
  assert.match(html, /did not pass evidence checks/)
  assert.equal(html.match(/Need local evidence\./g).length, 1)
  const noReview = renderToStaticMarkup(createElement(ResearchCard, { report: research }))
  assert.match(noReview, /did not pass evidence checks/)
  assert.doesNotMatch(noReview, /Research evidence checked/)
})

await test('report card renders no-search results and rejects unsafe source links', () => {
  const html = renderToStaticMarkup(createElement(ResearchCard, { validation, report: {
    status: 'not_needed', summary: 'Brand facts were enough.',
    source_urls: ['javascript:alert(1)', 'data:text/html,bad', 'broken url'], uncertainties: [],
  } }))
  assert.match(html, /Existing brand facts were enough/)
  assert.match(html, /No verified external sources were used/)
  assert.doesNotMatch(html, /href=/)
})
