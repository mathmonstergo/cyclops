import type { RetrievalEvalCase, RetrievalEvalItem } from '@/api/schemas'
import {
  type EvaluationRunOverrides,
  type EvaluationStrategy,
  selectEvaluationRun,
} from './helpers.ts'

export type EvaluationDiagnosticReason =
  | 'missing_expected'
  | 'not_run'
  | 'empty_candidates'
  | 'missed'
  | 'low_rank'
  | 'granularity_mismatch'
  | 'hit'

export type EvaluationCaseDiagnostic = {
  caseId: string
  question: string
  reason: EvaluationDiagnosticReason
  expectedLevel: 'source' | 'chunk' | 'none'
  rank?: number
  recall?: number
  mrr?: number
  top1?: number
}

export type EvaluationBatchSummary = {
  caseCount: number
  activeCaseCount: number
  labeledCaseCount: number
  runCount: number
  hitCount: number
  missedCount: number
  lowRankCount: number
  granularityMismatchCount: number
  emptyCandidateCount: number
  notRunCount: number
  missingExpectedCount: number
  averageRecall?: number
  averageMrr?: number
  top1Rate?: number
  diagnostics: EvaluationCaseDiagnostic[]
}

// 汇总当前策略的最新运行；关键约束是不跨策略回退或混算指标。
export function buildEvaluationBatchSummary(
  cases: RetrievalEvalCase[],
  strategy: EvaluationStrategy,
  runOverrides: EvaluationRunOverrides,
): EvaluationBatchSummary {
  const activeCases = cases.filter((item) => item.status === 'active')
  const diagnostics = activeCases.map((item) =>
    diagnoseEvaluationCase(item, strategy, runOverrides),
  )
  const labeledDiagnostics = diagnostics.filter((item) => item.expectedLevel !== 'none')
  const selectedRuns = activeCases
    .map((item) => selectEvaluationRun(item, runOverrides, strategy))
    .filter((run) => run !== null)
  return {
    caseCount: cases.length,
    activeCaseCount: activeCases.length,
    labeledCaseCount: labeledDiagnostics.length,
    runCount: selectedRuns.length,
    hitCount: diagnostics.filter((item) => item.reason === 'hit' || item.reason === 'low_rank').length,
    missedCount: diagnostics.filter((item) => item.reason === 'missed').length,
    lowRankCount: diagnostics.filter((item) => item.reason === 'low_rank').length,
    granularityMismatchCount: diagnostics.filter((item) => item.reason === 'granularity_mismatch').length,
    emptyCandidateCount: diagnostics.filter((item) => item.reason === 'empty_candidates').length,
    notRunCount: diagnostics.filter((item) => item.reason === 'not_run').length,
    missingExpectedCount: diagnostics.filter((item) => item.reason === 'missing_expected').length,
    averageRecall: average(labeledDiagnostics.map((item) => item.recall)),
    averageMrr: average(labeledDiagnostics.map((item) => item.mrr)),
    top1Rate: average(labeledDiagnostics.map((item) => item.top1)),
    diagnostics,
  }
}

// 诊断单条用例当前策略的运行；关键约束是未标注和未运行不算作检索失败。
export function diagnoseEvaluationCase(
  evalCase: RetrievalEvalCase,
  strategy: EvaluationStrategy,
  runOverrides: EvaluationRunOverrides,
): EvaluationCaseDiagnostic {
  const expectedLevel = expectedHitLevel(evalCase)
  if (expectedLevel === 'none') {
    return baseDiagnostic(evalCase, 'missing_expected', expectedLevel)
  }

  const run = selectEvaluationRun(evalCase, runOverrides, strategy)
  if (!run) {
    return baseDiagnostic(evalCase, 'not_run', expectedLevel)
  }

  const metrics = {
    recall: numericMetric(run.metrics?.recall_at_k),
    mrr: numericMetric(run.metrics?.mrr),
    top1: numericMetric(run.metrics?.hit_rate_at_1),
  }
  if (run.retrieved_items.length === 0) {
    return { ...baseDiagnostic(evalCase, 'empty_candidates', expectedLevel), ...metrics }
  }

  const rank = findExpectedRank(evalCase, run.retrieved_items)
  if (rank === undefined) {
    return {
      ...baseDiagnostic(evalCase, hasGranularityMismatch(evalCase, run.retrieved_items) ? 'granularity_mismatch' : 'missed', expectedLevel),
      ...metrics,
    }
  }
  return {
    ...baseDiagnostic(evalCase, rank === 1 ? 'hit' : 'low_rank', expectedLevel),
    ...metrics,
    rank,
  }
}

// 判断当前用例使用 source 还是 chunk 粒度；chunk 优先级对齐后端指标口径。
function expectedHitLevel(evalCase: RetrievalEvalCase): 'source' | 'chunk' | 'none' {
  if ((evalCase.expected_chunk_ids || []).length > 0) return 'chunk'
  if ((evalCase.expected_source_ids || []).length > 0) return 'source'
  return 'none'
}

// 找出期望命中的 TopK 排名；未命中时返回 undefined，避免把 0 误读为第一名。
function findExpectedRank(
  evalCase: RetrievalEvalCase,
  items: RetrievalEvalItem[],
): number | undefined {
  const expectedLevel = expectedHitLevel(evalCase)
  const index = items.findIndex((item) => {
    if (expectedLevel === 'chunk') return (evalCase.expected_chunk_ids || []).includes(item.id)
    if (expectedLevel === 'source') return (evalCase.expected_source_ids || []).includes(item.source_id)
    return false
  })
  return index >= 0 ? index + 1 : undefined
}

// 识别 source/chunk 粒度错配；用于提示用户期望粒度可能过细或过粗。
function hasGranularityMismatch(evalCase: RetrievalEvalCase, items: RetrievalEvalItem[]): boolean {
  const expectedChunkIds = evalCase.expected_chunk_ids || []
  if (expectedChunkIds.length > 0) {
    return items.some((item) => (evalCase.expected_source_ids || []).includes(item.source_id))
  }
  return false
}

// 构造诊断基础字段；避免不同分支漏填 case id 和问题。
function baseDiagnostic(
  evalCase: RetrievalEvalCase,
  reason: EvaluationDiagnosticReason,
  expectedLevel: EvaluationCaseDiagnostic['expectedLevel'],
): EvaluationCaseDiagnostic {
  return {
    caseId: evalCase.id,
    question: evalCase.question,
    reason,
    expectedLevel,
  }
}

// 读取指标数值；非数值保持 undefined，防止平均值把未知当作 0。
function numericMetric(value: unknown): number | undefined {
  return typeof value === 'number' && !Number.isNaN(value) ? value : undefined
}

// 计算平均值；空数组返回 undefined，UI 再决定占位展示。
function average(values: Array<number | undefined>): number | undefined {
  const clean = values.filter((value): value is number => typeof value === 'number')
  if (clean.length === 0) return undefined
  return clean.reduce((sum, value) => sum + value, 0) / clean.length
}
