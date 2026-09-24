import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Copy,
  FileText,
  MessageSquareText,
} from 'lucide-react'
import type { RetrievalEvalCase, RetrievalEvalRun } from '@/api/schemas'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/cn'
import {
  candidateExcerpt,
  candidateLocationLabel,
  candidateScore,
  candidateSourceLabel,
  displayStrategyLabel,
  evaluationCandidateState,
  formatCount,
  formatDateTime,
  formatMetric,
  formatPercent,
  formatScore,
  isExpectedHit,
  retrievalChannelLabel,
  sourceTypeLabel,
} from './helpers'

// 运行结果详情面板；只展示当前用例和最近一次/刚完成的运行结果。
export function EvaluationResultPanel({
  evalCase,
  run,
  onMarkExpected,
  onOpenCandidate,
  markingExpected,
}: {
  evalCase: RetrievalEvalCase | null
  run: RetrievalEvalRun | null
  onMarkExpected?: (
    item: RetrievalEvalRun['retrieved_items'][number],
    level: 'source' | 'chunk',
  ) => void
  onOpenCandidate?: (item: RetrievalEvalRun['retrieved_items'][number]) => void
  markingExpected?: boolean
}) {
  if (!evalCase) {
    return (
      <div className="flex h-full items-center justify-center text-center">
        <div className="-mt-24">
          <Activity className="mx-auto size-7 text-(--color-text-faint)" />
          <div className="mt-3 text-[14px] text-(--color-text-muted)">选择评测用例</div>
          <div className="mt-1 text-[12px] text-(--color-text-faint)">运行后查看指标、轨迹和候选来源</div>
        </div>
      </div>
    )
  }

  const metrics = run?.metrics
  const analysis = run?.analysis
  const items = run?.retrieved_items || []
  const candidateState = evaluationCandidateState(run)

  return (
    <div className="h-full overflow-y-auto scroll-thin px-5 py-4">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[12px] text-(--color-text-faint)">
        <span>最近运行：{formatDateTime(run?.created_at)}</span>
        {run?.strategy && <span>策略：{displayStrategyLabel(run.strategy)}</span>}
      </div>
        <MetricGrid
          recall={metrics?.recall_at_k}
          mrr={metrics?.mrr}
          top1={metrics?.hit_rate_at_1}
          vectorCount={analysis?.vector_count}
          keywordCount={analysis?.keyword_count}
        />
        <TraceBlock run={run} />
        <CandidateTable
          evalCase={evalCase}
          items={items}
          state={candidateState}
          onMarkExpected={onMarkExpected}
          onOpenCandidate={onOpenCandidate}
          markingExpected={markingExpected}
        />
    </div>
  )
}

// 指标条保持五列稳定，避免 Recall/MRR/Top1 数值变化导致布局跳动。
function MetricGrid({
  recall,
  mrr,
  top1,
  vectorCount,
  keywordCount,
}: {
  recall?: number
  mrr?: number
  top1?: number
  vectorCount?: number
  keywordCount?: number
}) {
  const metrics = [
    { label: 'Recall@K', value: formatPercent(recall), title: 'TopK 候选是否找回期望来源或切片', tone: recall && recall > 0 ? 'success' : 'muted' },
    { label: 'MRR', value: formatMetric(mrr), title: 'Mean Reciprocal Rank：正确候选越靠前分数越高', tone: mrr && mrr > 0 ? 'success' : 'muted' },
    { label: 'Top1', value: formatPercent(top1), title: '第一名是否就是期望命中', tone: top1 && top1 > 0 ? 'success' : 'muted' },
    { label: '向量候选', value: formatCount(vectorCount), title: '向量召回返回的候选数量', tone: 'muted' },
    { label: '关键词候选', value: formatCount(keywordCount), title: '关键词召回返回的候选数量', tone: 'muted' },
  ] as const

  return (
    <div className="grid grid-cols-5 gap-2">
      {metrics.map((metric) => (
        <div
          key={metric.label}
          title={metric.title}
          className="rounded-(--radius-control) border border-(--color-border) bg-(--color-surface) px-3 py-3"
        >
          <div className="text-[11px] text-(--color-text-faint)">{metric.label}</div>
          <div className="mt-2 flex items-center gap-1.5">
            <span className="font-mono text-[20px] leading-none text-(--color-text)">
              {metric.value}
            </span>
            {metric.tone === 'success' && <CheckCircle2 className="size-3.5 text-(--color-success)" />}
          </div>
        </div>
      ))}
    </div>
  )
}

// 运行轨迹区当前只展示 hybrid retrieval 单步，命名上为后续 agentic trace 预留。
function TraceBlock({ run }: { run: RetrievalEvalRun | null }) {
  const analysis = run?.analysis
  return (
    <section className="mt-4 rounded-(--radius-control) border border-(--color-border) bg-(--color-surface) p-3">
      <div className="mb-3 flex items-center gap-2 text-[12px] font-[540] text-(--color-text)">
        <Activity className="size-3.5 text-(--color-primary-hi)" />
        运行轨迹 / 分析
        {run?.strategy && <Badge tone="primary">{displayStrategyLabel(run.strategy)}</Badge>}
      </div>
      {!run ? (
        <div className="text-[12px] text-(--color-text-faint)">当前用例尚未运行</div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <TraceField label="意图" value={analysis?.intent} />
            <TraceField label="置信度" value={analysis?.confidence} />
            <TraceField label="查询改写" value={analysis?.query_rewrite || analysis?.query} />
          </div>
          <div className="flex items-center gap-2 rounded-(--radius-control) border border-(--color-border-soft) bg-(--color-surface-2) px-3 py-2 text-[12px]">
            <span className="inline-flex size-5 items-center justify-center rounded-full bg-(--color-primary-soft) text-[11px] text-(--color-primary-hi)">
              1
            </span>
            <span className="text-(--color-text)">混合检索</span>
            <span className="ml-auto font-mono text-[10px] text-(--color-text-faint)">
              向量 {formatCount(analysis?.vector_count)}
            </span>
            <span className="font-mono text-[10px] text-(--color-text-faint)">
              关键词 {formatCount(analysis?.keyword_count)}
            </span>
            {analysis?.use_kg && (
              <>
                <span className="font-mono text-[10px] text-(--color-text-faint)">
                  KG fact {analysis.kg_fact_count}
                </span>
                <span className="font-mono text-[10px] text-(--color-text-faint)">
                  展开 {analysis.kg_expanded_candidate_count}
                </span>
              </>
            )}
            <CheckCircle2 className="size-3.5 text-(--color-success)" />
          </div>
          {analysis?.use_kg && analysis.kg_facts.length > 0 && (
            <div className="space-y-1 rounded-(--radius-control) border border-(--color-border-soft) bg-(--color-surface-2) p-2">
              {analysis.kg_facts.slice(0, 6).map((fact) => (
                <div
                  key={fact.fact_chunk_id}
                  className="flex min-w-0 items-center gap-2 text-[10px] text-(--color-text-faint)"
                >
                  <span className="shrink-0">#{fact.fact_rank}</span>
                  <span className="min-w-0 flex-1 truncate font-mono">{fact.fact_id}</span>
                  <span>{fact.expanded_candidate_ids.length} 个原始候选</span>
                </div>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-1">
            {(analysis?.query_terms || []).map((term) => (
              <Badge key={term} tone="muted">
                {term}
              </Badge>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

// 轨迹字段使用截断展示，完整内容交给 title，避免长 query rewrite 撑开布局。
function TraceField({ label, value }: { label: string; value?: string }) {
  return (
    <div className="rounded-(--radius-control) border border-(--color-border-soft) bg-(--color-surface-2) px-2.5 py-2">
      <div className="text-[10px] text-(--color-text-faint)">{label}</div>
      <div className="mt-1 truncate text-[12px] text-(--color-text-muted)" title={value || '--'}>
        {value || '--'}
      </div>
    </div>
  )
}

// 候选来源表按期望命中做弱标记，帮助定位资料、切块或别名问题。
function CandidateTable({
  evalCase,
  items,
  state,
  onMarkExpected,
  onOpenCandidate,
  markingExpected,
}: {
  evalCase: RetrievalEvalCase
  items: RetrievalEvalRun['retrieved_items']
  state: ReturnType<typeof evaluationCandidateState>
  onMarkExpected?: (
    item: RetrievalEvalRun['retrieved_items'][number],
    level: 'source' | 'chunk',
  ) => void
  onOpenCandidate?: (item: RetrievalEvalRun['retrieved_items'][number]) => void
  markingExpected?: boolean
}) {
  return (
    <section className="mt-4 rounded-(--radius-control) border border-(--color-border) bg-(--color-surface)">
      <div className="flex items-center gap-3 border-b border-(--color-border) px-3 py-3">
        <div className="text-[12px] font-[540] text-(--color-text)">候选来源</div>
        <Badge tone="muted">Top {items.length}</Badge>
        <div className="ml-auto flex items-center gap-3 text-[11px] text-(--color-text-faint)">
          <LegendDot tone="hit" label="命中期望" />
          <LegendDot tone="miss" label="未命中期望" />
        </div>
      </div>
      {state !== 'ready' ? (
        <div className="flex items-center justify-center gap-2 px-4 py-10 text-[12px] text-(--color-text-faint)">
          <Clock3 className="size-4" />
          {state === 'not_run' ? '当前策略尚未运行' : '本次运行未召回候选'}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1020px] text-left text-[12px]">
            <thead className="border-b border-(--color-border-soft) text-[10px] text-(--color-text-faint)">
              <tr>
                <th className="w-12 px-3 py-2 font-[500]">排序</th>
                <th className="w-16 px-3 py-2 font-[500]">命中</th>
                <th className="w-28 px-3 py-2 font-[500]">类型</th>
                <th className="px-3 py-2 font-[500]">来源</th>
                <th className="px-3 py-2 font-[500]">位置 / 摘要</th>
                <th className="w-20 px-3 py-2 font-[500]">分数</th>
                <th className="px-3 py-2 font-[500]">召回通道</th>
                <th className="w-28 px-3 py-2 font-[500]">查看</th>
                <th className="w-36 px-3 py-2 font-[500]">标注</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => {
                const hit = isExpectedHit(item, evalCase)
                const location = candidateLocationLabel(item)
                const excerpt = candidateExcerpt(item)
                const isExpectedSource = (evalCase.expected_source_ids || []).includes(item.source_id)
                const isExpectedChunk = (evalCase.expected_chunk_ids || []).includes(item.id)
                return (
                  <tr
                    key={`${item.id}-${index}`}
                    className={cn(
                      'border-b border-(--color-border-soft) last:border-0',
                      hit ? 'bg-(--color-success)/[0.035]' : 'hover:bg-(--color-surface-2)',
                    )}
                  >
                    <td className="px-3 py-2 font-mono text-[11px] text-(--color-text-faint)">
                      {index + 1}
                    </td>
                    <td className="px-3 py-2">
                      {hit ? (
                        <CheckCircle2 className="size-3.5 text-(--color-success)" />
                      ) : (
                        <AlertTriangle className="size-3.5 text-(--color-warning)" />
                      )}
                    </td>
                    <td className="px-3 py-2 text-(--color-text-muted)">
                      <div>{sourceTypeLabel(item.source_type)}</div>
                      {item.chunk_level && (
                        <div className="mt-0.5 font-mono text-[10px] text-(--color-text-faint)">
                          {item.chunk_level}
                        </div>
                      )}
                    </td>
                    <td className="max-w-64 px-3 py-2">
                      <div
                        className="truncate text-[12px] text-(--color-text)"
                        title={candidateSourceLabel(item)}
                      >
                        {candidateSourceLabel(item)}
                      </div>
                      <div className="mt-1 space-y-0.5">
                        <CandidateIdRow label="来源 ID" value={item.source_id} />
                        <CandidateIdRow label="切片 ID" value={item.id} />
                      </div>
                    </td>
                    <td className="max-w-80 px-3 py-2">
                      <div className="truncate text-[11px] text-(--color-text-muted)" title={location}>
                        {location}
                      </div>
                      <div className="mt-1 line-clamp-2 text-[12px] leading-5 text-(--color-text)">
                        {excerpt}
                      </div>
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-(--color-text)">
                      {formatScore(candidateScore(item))}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">
                        {(item.channels || []).map((channel) => (
                          <Badge key={channel} tone="muted">
                            {retrievalChannelLabel(channel)}
                          </Badge>
                        ))}
                        {item.kg_matches.length > 0 && (
                          <span className="text-[10px] text-(--color-text-faint)">
                            {item.kg_matches.length} 个 fact
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <Button
                        variant="outline"
                        size="sm"
                        className="cursor-pointer"
                        disabled={!onOpenCandidate || !canOpenCandidate(item)}
                        onClick={() => onOpenCandidate?.(item)}
                        title={candidateOpenTitle(item)}
                      >
                        {item.source_type === 'faq' ? (
                          <MessageSquareText className="size-3.5" />
                        ) : (
                          <FileText className="size-3.5" />
                        )}
                        {item.source_type === 'faq' ? '查看 FAQ' : '查看切片'}
                      </Button>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-col gap-1">
                        <button
                          type="button"
                          disabled={!onMarkExpected || markingExpected || isExpectedSource}
                          onClick={() => onMarkExpected?.(item, 'source')}
                          className={cn(
                            'rounded-(--radius-control) border px-2 py-1 text-[11px] transition-colors disabled:pointer-events-none disabled:opacity-50',
                            isExpectedSource
                              ? 'border-(--color-success)/40 bg-(--color-success)/10 text-(--color-success)'
                              : 'border-(--color-border) text-(--color-text-muted) hover:bg-(--color-surface-2) hover:text-(--color-text)',
                          )}
                        >
                          {isExpectedSource ? '已是期望来源' : '设为期望来源'}
                        </button>
                        <button
                          type="button"
                          disabled={!onMarkExpected || markingExpected || isExpectedChunk}
                          onClick={() => onMarkExpected?.(item, 'chunk')}
                          className={cn(
                            'rounded-(--radius-control) border px-2 py-1 text-[11px] transition-colors disabled:pointer-events-none disabled:opacity-50',
                            isExpectedChunk
                              ? 'border-(--color-success)/40 bg-(--color-success)/10 text-(--color-success)'
                              : 'border-(--color-border) text-(--color-text-muted) hover:bg-(--color-surface-2) hover:text-(--color-text)',
                          )}
                        >
                          {isExpectedChunk ? '已是期望切片' : '设为期望切片'}
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// ID 行只作为排查信息展示；复制动作显式放在 icon 上，避免误触 ID 文本。
function CandidateIdRow({ label, value }: { label: string; value?: string | null }) {
  const clean = String(value || '').trim()
  if (!clean) return null
  return (
    <div className="flex min-w-0 items-center gap-1 text-[10px] text-(--color-text-faint)">
      <span className="shrink-0">{label}</span>
      <span className="truncate font-mono" title={clean}>
        {clean}
      </span>
      <button
        type="button"
        onClick={() => void copyText(clean, label)}
        className="inline-flex size-5 shrink-0 cursor-pointer items-center justify-center rounded-(--radius-control) text-(--color-text-faint) hover:bg-(--color-surface-3) hover:text-(--color-text)"
        title={`复制${label}`}
        aria-label={`复制${label}`}
      >
        <Copy className="size-3" />
      </button>
    </div>
  )
}

// 复制使用浏览器已有 clipboard 能力；失败时 toast 提示，不静默吞掉。
async function copyText(value: string, label: string): Promise<void> {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
    await navigator.clipboard.writeText(value)
    toast.success(`已复制${label}`)
  } catch {
    toast.error(`复制${label}失败`)
  }
}

// 判断候选能否打开既有来源抽屉；文档按导入文件 id，FAQ 按 FAQ id。
function canOpenCandidate(item: RetrievalEvalRun['retrieved_items'][number]): boolean {
  if (item.source_type === 'faq') return !!item.source_id
  if (item.source_type === 'document') return !!item.source_id
  return false
}

// 查看按钮 title 给出具体打开目标，hover 时能理解 icon+短文案。
function candidateOpenTitle(item: RetrievalEvalRun['retrieved_items'][number]): string {
  if (item.source_type === 'faq') return '打开对应 FAQ 抽屉'
  if (item.source_type === 'document') return item.source_chunk_id ? '打开文档抽屉并定位切片' : '打开文档抽屉'
  return '当前来源类型暂不支持打开'
}

// 候选表图例，约束只表达命中/未命中两种评测态。
function LegendDot({ tone, label }: { tone: 'hit' | 'miss'; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={cn(
          'inline-block size-1.5 rounded-full',
          tone === 'hit' ? 'bg-(--color-success)' : 'bg-(--color-warning)',
        )}
      />
      {label}
    </span>
  )
}
