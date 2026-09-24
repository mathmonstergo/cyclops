import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const knowledgeGraphPageSource = readFileSync(
  new URL('../KnowledgeGraphPage.tsx', import.meta.url),
  'utf8',
)
const faqDrawerSource = readFileSync(new URL('../faqs/faq-drawer.tsx', import.meta.url), 'utf8')
const chunkBrowserSource = readFileSync(
  new URL('../documents/chunk-browser.tsx', import.meta.url),
  'utf8',
)

test('routes KG extraction through the owning FAQ and document chunk screens only', () => {
  // KG 审核页只负责候选审核；抽取必须从持有精确来源 ID 的业务页面直接发起。
  assert.doesNotMatch(knowledgeGraphPageSource, /useCreateKgExtractionJob/)
  assert.doesNotMatch(knowledgeGraphPageSource, /ExtractionJobPopover/)
  assert.doesNotMatch(knowledgeGraphPageSource, /输入完整 FAQ ID/)
  assert.match(
    faqDrawerSource,
    /\.mutateAsync\(\{(?=[^}]*source_type:\s*'faq')(?=[^}]*source_id:\s*faqId)[^}]*\}\)/,
  )
  assert.match(
    chunkBrowserSource,
    /\.mutateAsync\(\{(?=[^}]*source_type:\s*'document_chunk')(?=[^}]*source_id:\s*chunk\.id)[^}]*\}\)/,
  )
})
