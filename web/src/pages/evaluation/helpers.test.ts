import test from 'node:test'
import assert from 'node:assert/strict'
import type {
  RetrievalEvalCase,
  RetrievalEvalItem,
  RetrievalEvalRun,
  RetrievalEvalStrategyId,
} from '@/api/schemas'
import {
  type EvaluationRunOverrides,
  buildEvaluationRunPayload,
  candidateExcerpt,
  candidateLocationLabel,
  candidateScore,
  candidateSourceLabel,
  displayStrategyLabel,
  evaluationCandidateState,
  selectEvaluationRun,
  sourceTypeLabel,
  storeEvaluationRunOverride,
  summarizeExpected,
} from './helpers.ts'

// 构造封闭的当前评测候选，所有合法测试数据必须显式满足 v2 DTO。
function makeRetrievalEvalItem(
  overrides: Partial<RetrievalEvalItem>,
): RetrievalEvalItem {
  return {
    id: 'kc_faq_default',
    source_id: 'faq_default',
    source_type: 'faq',
    source_chunk_id: null,
    parent_chunk_id: null,
    chunk_level: 'chunk',
    source_title: '默认 FAQ',
    section_path: [],
    page_start: null,
    page_end: null,
    block_type: 'faq',
    content: '默认正文',
    channels: ['vector'],
    fused_score: 0.5,
    vector_score: 0.5,
    keyword_score: null,
    kg_score: null,
    kg_matches: [],
    ...overrides,
  }
}

// 构造最小当前契约运行快照；strategy 与 use_kg 必须保持一致。
function makeEvaluationRun(
  id: string,
  strategy: RetrievalEvalStrategyId,
): RetrievalEvalRun {
  const useKg = strategy === 'retrieval_hybrid_v1_kg_debug'
  return {
    id,
    case_id: 'eval_1',
    strategy,
    retrieved_items: [],
    metrics: {},
    analysis: {
      contract_version: 2,
      use_kg: useKg,
      kg_fact_count: 0,
      kg_expanded_candidate_count: 0,
      kg_facts: [],
    },
  }
}

test('builds the only canonical payload for each evaluation strategy', () => {
  assert.deepEqual(buildEvaluationRunPayload('baseline'), {})
  assert.deepEqual(buildEvaluationRunPayload('kg_debug'), { use_kg: true })
})

test('distinguishes not-run, empty-candidate, and ready result states', () => {
  const emptyRun = makeEvaluationRun('run_empty', 'retrieval_hybrid_v1')
  const readyRun: RetrievalEvalRun = {
    ...emptyRun,
    retrieved_items: new Array<RetrievalEvalItem>(1),
  }

  assert.equal(evaluationCandidateState(null), 'not_run')
  assert.equal(evaluationCandidateState(emptyRun), 'empty_candidates')
  assert.equal(evaluationCandidateState(readyRun), 'ready')
})

test('stores baseline and KG debug overrides independently for the same case', () => {
  const baselineRun = makeEvaluationRun('run_baseline_new', 'retrieval_hybrid_v1')
  const kgRun = makeEvaluationRun('run_kg_new', 'retrieval_hybrid_v1_kg_debug')
  let overrides: EvaluationRunOverrides = {}

  overrides = storeEvaluationRunOverride(overrides, baselineRun)
  overrides = storeEvaluationRunOverride(overrides, kgRun)

  assert.equal(overrides.eval_1?.baseline?.id, 'run_baseline_new')
  assert.equal(overrides.eval_1?.kg_debug?.id, 'run_kg_new')
})

test('rejects an unknown persisted strategy instead of treating it as KG debug', () => {
  const unknownRun = {
    ...makeEvaluationRun('run_unknown', 'retrieval_hybrid_v1'),
    strategy: 'retrieval_hybrid_legacy',
  } as unknown as RetrievalEvalRun

  assert.throws(
    () => storeEvaluationRunOverride({}, unknownRun),
    /unknown retrieval evaluation strategy/,
  )
})

test('selects only the saved run for the active evaluation strategy', () => {
  const persistedBaseline = makeEvaluationRun('run_baseline_saved', 'retrieval_hybrid_v1')
  const persistedKg = makeEvaluationRun('run_kg_saved', 'retrieval_hybrid_v1_kg_debug')
  const overrideBaseline = makeEvaluationRun('run_baseline_override', 'retrieval_hybrid_v1')
  const evalCase = {
    id: 'eval_1',
    question: '报告导出失败怎么办？',
    intent: null,
    expected_source_ids: [],
    expected_chunk_ids: [],
    tags: [],
    note: null,
    status: 'active',
    latest_runs: [persistedBaseline, persistedKg],
  } as RetrievalEvalCase
  const overrides: EvaluationRunOverrides = {
    eval_1: { baseline: overrideBaseline },
  }

  assert.equal(selectEvaluationRun(evalCase, overrides, 'baseline')?.id, 'run_baseline_override')
  assert.equal(selectEvaluationRun(evalCase, overrides, 'kg_debug')?.id, 'run_kg_saved')
  assert.equal(
    selectEvaluationRun(
      { ...evalCase, latest_runs: [persistedBaseline] } as RetrievalEvalCase,
      {},
      'kg_debug',
    ),
    null,
  )
})

test('candidate helpers use canonical FAQ source title and content fields', () => {
  const item = makeRetrievalEvalItem({
    id: 'kc_faq_1',
    source_id: 'faq_1',
    source_title: '报告导出失败怎么办？',
    content: '先检查账号权限，再重新生成报告。',
  })

  assert.equal(candidateSourceLabel(item), '报告导出失败怎么办？')
  assert.equal(candidateExcerpt(item), '先检查账号权限，再重新生成报告。')
  assert.equal(candidateLocationLabel(item), 'FAQ')
})

test('candidate helpers keep document page and section trace readable', () => {
  const item = makeRetrievalEvalItem({
    id: 'kc_doc_child_1',
    source_id: 'imp_1',
    source_type: 'document',
    source_chunk_id: 'chunk_1',
    source_title: '售后手册.pdf',
    section_path: ['售后', '报告导出'],
    page_start: 3,
    page_end: 4,
    block_type: 'text',
    content: '报告导出失败时，先检查账号权限和网络状态。',
    channels: ['vector', 'keyword'],
  })

  assert.equal(candidateSourceLabel(item), '售后手册.pdf')
  assert.equal(candidateLocationLabel(item), '页 3-4 · 售后 > 报告导出 · 审核切片 chunk_1 · text')
  assert.equal(candidateExcerpt(item), '报告导出失败时，先检查账号权限和网络状态。')
})

test('candidate helpers expose empty canonical fields without alternate DTO repair paths', () => {
  const item = makeRetrievalEvalItem({
    id: 'kc_doc_child_legacy',
    source_id: 'imp_legacy',
    source_type: 'document',
    source_title: null,
    content: '',
  })

  assert.equal(candidateSourceLabel(item), 'imp_legacy')
  assert.equal(candidateExcerpt(item), '--')
})

test('candidate location keeps zero-based pages visible', () => {
  const item = makeRetrievalEvalItem({
    id: 'kc_doc_cover',
    source_id: 'imp_1',
    source_type: 'document',
    source_chunk_id: 'chunk_cover',
    page_start: 0,
    page_end: 0,
    channels: ['kg'],
    kg_score: 0.7,
  })

  assert.equal(candidateLocationLabel(item), '页 0 · 审核切片 chunk_cover')
})

test('candidate score reads only the mandatory fused score', () => {
  const valid = makeRetrievalEvalItem({ fused_score: 0, vector_score: 0.9 })
  const malformed = {
    ...valid,
    fused_score: undefined,
    vector_score: 0.9,
    keyword_score: 0.8,
  } as unknown as RetrievalEvalItem

  assert.equal(candidateScore(valid), 0)
  assert.equal(candidateScore(malformed), undefined)
})

test('evaluation display labels hide internal English where possible', () => {
  assert.equal(displayStrategyLabel('retrieval_hybrid_v1'), '混合检索 v1')
  assert.equal(displayStrategyLabel('retrieval_hybrid_v1_kg_debug'), 'KG 调试')
  assert.equal(sourceTypeLabel('document'), '文档')
  assert.equal(sourceTypeLabel('faq'), 'FAQ')
  assert.equal(summarizeExpected({ expected_chunk_ids: ['chunk_1'] } as never), '1 个期望切片')
  assert.equal(summarizeExpected({ expected_source_ids: ['faq_1', 'faq_2'] } as never), '2 个期望来源')
  assert.equal(summarizeExpected({ expected_source_ids: [], expected_chunk_ids: [] } as never), '待设置期望命中')
})
