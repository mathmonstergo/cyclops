import test from 'node:test'
import assert from 'node:assert/strict'
import type {
  RetrievalEvalCase,
  RetrievalEvalItem,
  RetrievalEvalMetrics,
  RetrievalEvalRun,
} from '@/api/schemas'
import { buildEvaluationBatchSummary } from './batch-diagnostics.ts'

// 构造封闭的当前评测候选，避免测试因擦除类型而接受缺字段 DTO。
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

// 构造基线策略运行快照；指标可独立缺失以验证聚合不会交叉过滤。
function makeEvaluationRun(
  id: string,
  caseId: string,
  metrics: RetrievalEvalMetrics,
  retrievedItems: RetrievalEvalItem[] = [],
): RetrievalEvalRun {
  return {
    id,
    case_id: caseId,
    strategy: 'retrieval_hybrid_v1',
    metrics,
    retrieved_items: retrievedItems,
    analysis: {
      contract_version: 2,
      use_kg: false,
      kg_fact_count: 0,
      kg_expanded_candidate_count: 0,
      kg_facts: [],
    },
  }
}

test('does not use the other strategy run for current diagnostics', () => {
  const evalCase: RetrievalEvalCase = {
    id: 'case_strategy_boundary',
    question: '使用基线检索能命中吗？',
    intent: null,
    expected_source_ids: ['faq_1'],
    expected_chunk_ids: [],
    tags: [],
    note: null,
    status: 'active',
    latest_runs: [
      {
        id: 'run_kg_only',
        case_id: 'case_strategy_boundary',
        strategy: 'retrieval_hybrid_v1_kg_debug',
        metrics: { recall_at_k: 1, mrr: 1, hit_rate_at_1: 1 },
        analysis: {
          contract_version: 2,
          use_kg: true,
          kg_fact_count: 1,
          kg_expanded_candidate_count: 1,
          kg_facts: [],
        },
        retrieved_items: [
          makeRetrievalEvalItem({
            id: 'kc_faq_1',
            source_id: 'faq_1',
            channels: ['kg'],
            kg_score: 0.8,
          }),
        ],
      },
    ],
  }

  const summary = buildEvaluationBatchSummary([evalCase], 'baseline', {})

  assert.equal(summary.runCount, 0)
  assert.equal(summary.notRunCount, 1)
  assert.equal(summary.diagnostics[0]?.reason, 'not_run')
})

test('buildEvaluationBatchSummary separates unlabeled, missed, and low-rank cases', () => {
  const cases: RetrievalEvalCase[] = [
    {
      id: 'case_unlabeled',
      question: '如何导出报告？',
      intent: null,
      expected_source_ids: [],
      expected_chunk_ids: [],
      tags: [],
      note: null,
      status: 'active',
      latest_runs: [],
    },
    {
      id: 'case_missed',
      question: '如何重置密码？',
      intent: null,
      expected_source_ids: ['faq_password'],
      expected_chunk_ids: [],
      tags: [],
      note: null,
      status: 'active',
      latest_runs: [{
        id: 'run_missed',
        case_id: 'case_missed',
        strategy: 'retrieval_hybrid_v1',
        metrics: { recall_at_k: 0, mrr: 0, hit_rate_at_1: 0 },
        analysis: {
          contract_version: 2,
          use_kg: false,
          kg_fact_count: 0,
          kg_expanded_candidate_count: 0,
          kg_facts: [],
        },
        retrieved_items: [
          makeRetrievalEvalItem({
            id: 'kc_other',
            source_id: 'faq_other',
            channels: ['keyword'],
          }),
        ],
      }],
    },
    {
      id: 'case_low_rank',
      question: '售后电话是多少？',
      intent: null,
      expected_source_ids: ['faq_after_sale'],
      expected_chunk_ids: [],
      tags: [],
      note: null,
      status: 'active',
      latest_runs: [{
        id: 'run_low_rank',
        case_id: 'case_low_rank',
        strategy: 'retrieval_hybrid_v1',
        metrics: { recall_at_k: 1, mrr: 0.5, hit_rate_at_1: 0 },
        analysis: {
          contract_version: 2,
          use_kg: false,
          kg_fact_count: 0,
          kg_expanded_candidate_count: 0,
          kg_facts: [],
        },
        retrieved_items: [
          makeRetrievalEvalItem({
            id: 'kc_other_2',
            source_id: 'faq_other_2',
            channels: ['vector'],
          }),
          makeRetrievalEvalItem({
            id: 'kc_expected',
            source_id: 'faq_after_sale',
            channels: ['keyword'],
          }),
        ],
      }],
    },
  ]

  const summary = buildEvaluationBatchSummary(cases, 'baseline', {})

  assert.equal(summary.caseCount, 3)
  assert.equal(summary.activeCaseCount, 3)
  assert.equal(summary.labeledCaseCount, 2)
  assert.equal(summary.missingExpectedCount, 1)
  assert.equal(summary.missedCount, 1)
  assert.equal(summary.lowRankCount, 1)
  assert.equal(summary.hitCount, 1)
  assert.equal(summary.averageRecall, 0.5)
  assert.deepEqual(
    summary.diagnostics.map((item) => [item.caseId, item.reason]),
    [
      ['case_unlabeled', 'missing_expected'],
      ['case_missed', 'missed'],
      ['case_low_rank', 'low_rank'],
    ],
  )
})

test('counts selected-strategy runs independently from labels and metric presence', () => {
  const mrrItem = makeRetrievalEvalItem({ id: 'kc_mrr', source_id: 'faq_mrr' })
  const recallItem = makeRetrievalEvalItem({ id: 'kc_recall', source_id: 'faq_recall' })
  const cases: RetrievalEvalCase[] = [
    {
      id: 'case_mrr',
      question: '只记录 MRR 的运行',
      intent: null,
      expected_source_ids: ['faq_mrr'],
      expected_chunk_ids: [],
      tags: [],
      note: null,
      status: 'active',
      latest_runs: [
        makeEvaluationRun('run_mrr', 'case_mrr', { mrr: 0.5, hit_rate_at_1: 1 }, [mrrItem]),
      ],
    },
    {
      id: 'case_recall',
      question: '只记录 Recall 的运行',
      intent: null,
      expected_source_ids: ['faq_recall'],
      expected_chunk_ids: [],
      tags: [],
      note: null,
      status: 'active',
      latest_runs: [
        makeEvaluationRun('run_recall', 'case_recall', { recall_at_k: 1 }, [recallItem]),
      ],
    },
    {
      id: 'case_unlabeled_run',
      question: '已运行但尚未标注',
      intent: null,
      expected_source_ids: [],
      expected_chunk_ids: [],
      tags: [],
      note: null,
      status: 'active',
      latest_runs: [
        makeEvaluationRun('run_unlabeled', 'case_unlabeled_run', { recall_at_k: 0 }),
      ],
    },
  ]

  const summary = buildEvaluationBatchSummary(cases, 'baseline', {})

  assert.equal(summary.runCount, 3)
  assert.equal(summary.missingExpectedCount, 1)
  assert.equal(summary.averageRecall, 1)
  assert.equal(summary.averageMrr, 0.5)
  assert.equal(summary.top1Rate, 1)
})
