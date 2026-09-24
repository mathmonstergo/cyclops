import type {
  RetrievalEvalCase,
  RetrievalEvalItem,
  RetrievalEvalRun,
  RetrievalEvalRunPayload,
  RetrievalEvalStrategyId,
} from '@/api/schemas'

export type EvaluationStrategy = 'baseline' | 'kg_debug'
export type EvaluationCandidateState = 'not_run' | 'empty_candidates' | 'ready'
export type EvaluationRunOverrides = Record<
  string,
  Partial<Record<EvaluationStrategy, RetrievalEvalRun>>
>

const EVALUATION_STRATEGY_IDS: Record<EvaluationStrategy, RetrievalEvalStrategyId> = {
  baseline: 'retrieval_hybrid_v1',
  kg_debug: 'retrieval_hybrid_v1_kg_debug',
}

export const CASE_STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'active', label: '启用' },
  { value: 'disabled', label: '禁用' },
]

export const CASE_FORM_STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: 'active', label: '启用' },
  { value: 'disabled', label: '禁用' },
]

// 将页面策略映射为唯一 API payload；baseline 不发送开关，KG debug 只发送真实布尔值。
export function buildEvaluationRunPayload(
  strategy: EvaluationStrategy,
): RetrievalEvalRunPayload {
  return strategy === 'kg_debug' ? { use_kg: true } : {}
}

// 按 case + strategy 保存刚完成的运行；同一用例的两种策略互不覆盖。
export function storeEvaluationRunOverride(
  current: EvaluationRunOverrides,
  run: RetrievalEvalRun,
): EvaluationRunOverrides {
  let strategy: EvaluationStrategy
  if (run.strategy === 'retrieval_hybrid_v1') {
    strategy = 'baseline'
  } else if (run.strategy === 'retrieval_hybrid_v1_kg_debug') {
    strategy = 'kg_debug'
  } else {
    throw new Error(`unknown retrieval evaluation strategy: ${String(run.strategy)}`)
  }
  return {
    ...current,
    [run.case_id]: {
      ...current[run.case_id],
      [strategy]: run,
    },
  }
}

// 只读取当前 UI 策略的运行；缺失时返回 null，禁止退回另一策略造成误比较。
export function selectEvaluationRun(
  evalCase: RetrievalEvalCase,
  overrides: EvaluationRunOverrides,
  strategy: EvaluationStrategy,
): RetrievalEvalRun | null {
  const override = overrides[evalCase.id]?.[strategy]
  if (override) return override
  const strategyId = EVALUATION_STRATEGY_IDS[strategy]
  return evalCase.latest_runs.find((run) => run.strategy === strategyId) || null
}

// 区分当前策略未运行、已运行无候选和已有候选，避免空数组掩盖真实运行状态。
export function evaluationCandidateState(
  run: RetrievalEvalRun | null,
): EvaluationCandidateState {
  if (!run) return 'not_run'
  return run.retrieved_items.length === 0 ? 'empty_candidates' : 'ready'
}

// 将换行或中英文逗号分隔的输入拆成干净数组，用于 source/chunk/tag 字段。
export function splitListInput(value: string): string[] {
  return value
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

// 将数组字段还原为多行输入文本，保持编辑抽屉可读。
export function joinListInput(values: string[] | undefined | null): string {
  return Array.isArray(values) ? values.join('\n') : ''
}

// 格式化百分比指标；未知值统一显示占位，避免误读为 0%。
export function formatPercent(value: number | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--'
  return `${Math.round(value * 100)}%`
}

// 格式化小数指标；MRR 等指标固定两位便于横向比较。
export function formatMetric(value: number | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--'
  return value.toFixed(2)
}

// 格式化数量指标；未知值使用占位以区分真实 0。
export function formatCount(value: number | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--'
  return String(value)
}

// 格式化候选分数；保留三位小数兼顾排序辨识和表格宽度。
export function formatScore(value: number | undefined | null): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '--'
  return value.toFixed(3)
}

// 格式化运行时间；后端字符串无法解析时原样展示，避免吞掉诊断信息。
export function formatDateTime(value: string | undefined): string {
  if (!value) return '未运行'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

// 读取候选融合分数；关键约束是 v2 必填字段缺失时不得用其他通道静默修复。
export function candidateScore(item: RetrievalEvalItem): number {
  return item.fused_score
}

// 判断候选是否命中期望；chunk 期望优先于 source 期望，保持评测口径明确。
export function isExpectedHit(item: RetrievalEvalItem, evalCase: RetrievalEvalCase): boolean {
  const expectedChunkIds = evalCase.expected_chunk_ids || []
  if (expectedChunkIds.length > 0) return expectedChunkIds.includes(item.id)
  return (evalCase.expected_source_ids || []).includes(item.source_id)
}

// 汇总期望标注数量，供列表行用短文案展示。
export function summarizeExpected(evalCase: RetrievalEvalCase): string {
  const chunkCount = evalCase.expected_chunk_ids?.length || 0
  const sourceCount = evalCase.expected_source_ids?.length || 0
  if (chunkCount > 0) return `${chunkCount} 个期望切片`
  if (sourceCount > 0) return `${sourceCount} 个期望来源`
  return '待设置期望命中'
}

// 候选来源标题优先展示业务可读字段，缺失时才退回内部来源 id。
export function candidateSourceLabel(item: RetrievalEvalItem): string {
  const title = String(item.source_title || '').trim()
  if (title) return title
  return item.source_id || '--'
}

// 候选位置合并页码、章节、审核切片 id，FAQ 则显示来源类型。
export function candidateLocationLabel(item: RetrievalEvalItem): string {
  const parts: string[] = []
  const hasStartPage = item.page_start !== null && item.page_start !== undefined
  const hasEndPage = item.page_end !== null && item.page_end !== undefined
  if (hasStartPage) {
    parts.push(
      hasEndPage && item.page_end !== item.page_start
        ? `页 ${item.page_start}-${item.page_end}`
        : `页 ${item.page_start}`,
    )
  }
  if (item.section_path?.length) parts.push(item.section_path.join(' > '))
  if (item.source_chunk_id) parts.push(`审核切片 ${item.source_chunk_id}`)
  if (item.block_type && item.block_type !== 'faq') parts.push(item.block_type)
  if (parts.length > 0) return parts.join(' · ')
  if (item.source_type === 'faq') return 'FAQ'
  return '--'
}

// 候选摘要优先使用答案/正文；FAQ content 为空时也能展示可读答案。
export function candidateExcerpt(item: RetrievalEvalItem): string {
  const content = String(item.content || '').trim()
  if (content) return content
  return '--'
}

// 将内部 source_type 转成工作台读得懂的短标签。
export function sourceTypeLabel(value: string): string {
  if (value === 'faq') return 'FAQ'
  if (value === 'document') return '文档'
  return value || '--'
}

// 将内部策略名转成中文展示；未知策略保留原值，便于排查。
export function displayStrategyLabel(value: string | undefined | null): string {
  if (!value) return '未运行'
  if (value === 'retrieval_hybrid_v1') return '混合检索 v1'
  if (value === 'retrieval_hybrid_v1_kg_debug') return 'KG 调试'
  return value
}

// 将召回通道短码转成中文徽章，避免候选表暴露 raw channel。
export function retrievalChannelLabel(value: string): string {
  if (value === 'vector') return '向量'
  if (value === 'keyword') return '关键词'
  if (value === 'kg') return 'KG'
  if (value === 'fused') return '融合'
  return value || '--'
}
