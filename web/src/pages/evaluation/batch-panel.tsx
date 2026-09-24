import { AlertTriangle, CheckCircle2, Loader2, Target } from 'lucide-react'
import type { EvaluationBatchSummary, EvaluationCaseDiagnostic, EvaluationDiagnosticReason } from './batch-diagnostics'
import type { EvaluationBatchRunState } from './batch-state'
import { formatMetric, formatPercent } from './helpers'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/cn'

// 批量回归面板展示当前视图的汇总和失败诊断；不持久化批次，只读页面状态。
export function EvaluationBatchPanel({
  summary,
  runState,
  batchCaseCount,
  runDisabled,
  onRunBatch,
  onSelectCase,
}: {
  summary: EvaluationBatchSummary
  runState: EvaluationBatchRunState
  batchCaseCount: number
  runDisabled: boolean
  onRunBatch: () => void
  onSelectCase: (caseId: string) => void
}) {
  const isRunning = runState.status === 'running'
  const issueDiagnostics = summary.diagnostics.filter((item) => item.reason !== 'hit')
  return (
    <section className="border-b border-(--color-border) bg-(--color-surface) px-5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <Target className="size-4 text-(--color-primary-hi)" />
          <div className="text-[13px] font-[540] text-(--color-text)">批量回归</div>
          <Badge tone="muted">{summary.activeCaseCount} 个启用用例</Badge>
          {runState.status !== 'idle' && (
            <Badge tone={runState.failed > 0 ? 'warning' : 'success'}>
              {runState.completed}/{runState.total}
            </Badge>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={onRunBatch}
          disabled={runDisabled || batchCaseCount === 0}
          title={batchCaseCount > 0 ? '顺序运行当前筛选范围内的启用用例' : '当前筛选范围没有启用用例'}
        >
          {isRunning ? <Loader2 className="size-3.5 animate-spin" /> : <Target className="size-3.5" />}
          {isRunning ? '批量运行中' : `运行 ${batchCaseCount} 个启用用例`}
        </Button>
      </div>

      {isRunning && (
        <div className="mt-2 flex items-center gap-2 text-[11px] text-(--color-text-faint)">
          <div className="h-1.5 w-36 overflow-hidden rounded-full bg-(--color-surface-3)">
            <div
              className="h-full rounded-full bg-(--color-primary)"
              style={{ width: `${runProgressPercent(runState)}%` }}
            />
          </div>
          <span className="truncate">
            成功 {runState.succeeded} · 失败 {runState.failed}
            {runState.currentQuestion ? ` · 当前：${runState.currentQuestion}` : ''}
          </span>
        </div>
      )}

      <div className="mt-3 grid grid-cols-6 gap-2">
        <BatchMetric label="Recall@K" title="TopK 是否找回期望来源或切片" value={formatPercent(summary.averageRecall)} tone={summary.averageRecall && summary.averageRecall > 0 ? 'success' : 'muted'} />
        <BatchMetric label="MRR" title="Mean Reciprocal Rank：正确候选越靠前分数越高" value={formatMetric(summary.averageMrr)} tone={summary.averageMrr && summary.averageMrr > 0 ? 'success' : 'muted'} />
        <BatchMetric label="Top1" title="第一名是否就是期望命中" value={formatPercent(summary.top1Rate)} tone={summary.top1Rate && summary.top1Rate > 0 ? 'success' : 'muted'} />
        <BatchMetric label="命中" value={String(summary.hitCount)} tone="success" />
        <BatchMetric label="未召回" value={String(summary.missedCount)} tone={summary.missedCount > 0 ? 'warning' : 'muted'} />
        <BatchMetric label="待标注" value={String(summary.missingExpectedCount)} tone={summary.missingExpectedCount > 0 ? 'warning' : 'muted'} />
      </div>

      <div className="mt-3 flex flex-wrap gap-1.5">
        <ReasonBadge reason="low_rank" count={summary.lowRankCount} />
        <ReasonBadge reason="granularity_mismatch" count={summary.granularityMismatchCount} />
        <ReasonBadge reason="empty_candidates" count={summary.emptyCandidateCount} />
        <ReasonBadge reason="not_run" count={summary.notRunCount} />
      </div>

      {issueDiagnostics.length > 0 && (
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
          {issueDiagnostics.slice(0, 8).map((item) => (
            <button
              key={item.caseId}
              type="button"
              onClick={() => onSelectCase(item.caseId)}
              className="min-w-[220px] rounded-(--radius-control) border border-(--color-border-soft) bg-(--color-surface-2) px-2.5 py-2 text-left transition-colors hover:border-(--color-primary-soft) hover:bg-(--color-surface-3)"
            >
              <div className="flex items-center gap-1.5">
                {item.reason === 'low_rank' ? (
                  <CheckCircle2 className="size-3.5 text-(--color-warning)" />
                ) : (
                  <AlertTriangle className="size-3.5 text-(--color-warning)" />
                )}
                <span className="text-[11px] text-(--color-text-muted)">
                  {reasonLabel(item.reason)}
                  {item.rank ? ` · 第 ${item.rank} 名` : ''}
                </span>
              </div>
              <div className="mt-1 truncate text-[12px] text-(--color-text)" title={item.question}>
                {item.question}
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

// 指标小块使用固定高度和等宽数值，避免批量运行过程中布局跳动。
function BatchMetric({ label, title, value, tone }: { label: string; title?: string; value: string; tone: 'success' | 'warning' | 'muted' }) {
  return (
    <div
      className="rounded-(--radius-control) border border-(--color-border-soft) bg-(--color-surface-2) px-2.5 py-2"
      title={title}
    >
      <div className="text-[10px] text-(--color-text-faint)">{label}</div>
      <div
        className={cn(
          'mt-1 font-mono text-[16px] leading-none',
          tone === 'success' && 'text-(--color-success)',
          tone === 'warning' && 'text-(--color-warning)',
          tone === 'muted' && 'text-(--color-text)',
        )}
      >
        {value}
      </div>
    </div>
  )
}

// 失败原因徽章只展示非零项，避免面板噪音过多。
function ReasonBadge({ reason, count }: { reason: EvaluationDiagnosticReason; count: number }) {
  if (count <= 0) return null
  return (
    <Badge tone="warning">
      {reasonLabel(reason)} {count}
    </Badge>
  )
}

// 将诊断枚举转换成工作台短标签。
function reasonLabel(reason: EvaluationCaseDiagnostic['reason']): string {
  const labels: Record<EvaluationCaseDiagnostic['reason'], string> = {
    missing_expected: '未标注',
    not_run: '未运行',
    empty_candidates: '无候选',
    missed: '未召回',
    low_rank: '排序低',
    granularity_mismatch: '粒度不匹配',
    hit: '命中',
  }
  return labels[reason]
}

// 计算运行进度百分比；总数为 0 时返回 0，避免 NaN 样式。
function runProgressPercent(runState: EvaluationBatchRunState): number {
  if (runState.total <= 0) return 0
  return Math.min(100, Math.round((runState.completed / runState.total) * 100))
}
