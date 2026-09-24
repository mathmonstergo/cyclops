import assert from 'node:assert/strict'
import test from 'node:test'
import type { AssistantSource } from '@/api/schemas'
import {
  buildAssistantSourceGroupTarget,
  buildAssistantSourceTargetKey,
  findAssistantSourceTargetIndex,
} from './source-target.ts'

// 构造封闭的当前 AssistantSource；测试覆盖字段时不得依赖缺省兼容形状。
function makeAssistantSource(overrides: Partial<AssistantSource>): AssistantSource {
  return {
    id: 'kc_faq_default',
    source_id: 'faq_default',
    source_type: 'faq',
    source_chunk_id: null,
    parent_chunk_id: null,
    chunk_level: 'chunk',
    source_title: '默认来源',
    section_path: [],
    page_start: null,
    page_end: null,
    block_type: 'faq',
    source_offsets: {},
    content: '默认正文',
    question: '默认问题',
    answer: '默认答案',
    category: null,
    tags: [],
    source_date: null,
    confidence: null,
    status: 'usable',
    score: 0.5,
    metadata: {},
    ...overrides,
  }
}

test('builds stable target keys for faq and document sources', () => {
  assert.equal(
    buildAssistantSourceTargetKey(makeAssistantSource({
      source_type: 'faq',
      source_id: 'faq_1',
      id: 'kc_faq_1',
    })),
    'faq:faq_1:kc_faq_1',
  )
  assert.equal(
    buildAssistantSourceTargetKey(makeAssistantSource({
      source_type: 'document',
      source_id: 'imp_1',
      source_chunk_id: 'chunk_1',
      id: 'kc_chunk_1',
    })),
    'document:imp_1:chunk_1',
  )
})

test('source group target selects first matching drawer source', () => {
  const sources = [
    makeAssistantSource({ source_type: 'faq', source_id: 'faq_1', id: 'kc_faq_1' }),
    makeAssistantSource({
      source_type: 'document',
      source_title: '手册.pdf',
      source_id: 'imp_1',
      source_chunk_id: 'c1',
    }),
    makeAssistantSource({
      source_type: 'document',
      source_title: '手册.pdf',
      source_id: 'imp_1',
      source_chunk_id: 'c2',
    }),
  ]

  const target = buildAssistantSourceGroupTarget(sources[1])

  assert.equal(findAssistantSourceTargetIndex(sources, target), 1)
})

test('rejects a legacy source that omits canonical source_id', () => {
  assert.throws(
    () =>
      buildAssistantSourceTargetKey({
        source_type: 'faq',
        id: 'kc_faq_legacy',
        chunk_id: 'legacy_chunk',
      } as unknown as AssistantSource),
    /assistant source requires canonical source_id/,
  )
})
