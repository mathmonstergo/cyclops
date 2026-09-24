import { MoreHorizontal, Pencil, PlayCircle } from 'lucide-react'
import type { KeyboardEvent } from 'react'
import type { RetrievalEvalCase } from '@/api/schemas'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/cn'
import {
  formatDateTime,
  formatMetric,
  formatPercent,
  displayStrategyLabel,
  type EvaluationRunOverrides,
  type EvaluationStrategy,
  selectEvaluationRun,
  summarizeExpected,
} from './helpers'

// 渲染评测用例列表；负责加载、错误、空态和可键盘选择的行交互。
export function EvaluationCaseList({
  items,
  activeId,
  isPending,
  isError,
  onRetry,
  onSelect,
  onEdit,
  strategy,
  runOverrides,
}: {
  items: RetrievalEvalCase[]
  activeId: string | null
  isPending: boolean
  isError: boolean
  onRetry: () => void
  onSelect: (id: string) => void
  onEdit: (item: RetrievalEvalCase) => void
  strategy: EvaluationStrategy
  runOverrides: EvaluationRunOverrides
}) {
  if (isPending) {
    return (
      <div className="space-y-2 p-3">
        {Array.from({ length: 8 }).map((_, index) => (
          <div key={index} className="skeleton h-20" />
        ))}
      </div>
    )
  }

  if (isError) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center">
        <div>
          <div className="text-[13px] text-(--color-text-muted)">评测用例加载失败</div>
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 text-[12px] text-(--color-primary-hi) hover:underline"
          >
            重试
          </button>
        </div>
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center">
        <div className="-mt-24">
          <PlayCircle className="mx-auto size-6 text-(--color-text-faint)" />
          <div className="mt-2 text-[13px] text-(--color-text-muted)">暂无评测用例</div>
          <div className="mt-1 text-[11px] text-(--color-text-faint)">新建用例后可运行单条检索验收</div>
        </div>
      </div>
    )
  }

  return (
    <div className="p-2">
      {items.map((item) => (
        <CaseRow
          key={item.id}
          item={item}
          active={item.id === activeId}
          onSelect={onSelect}
          onEdit={onEdit}
          strategy={strategy}
          runOverrides={runOverrides}
        />
      ))}
    </div>
  )
}

// 单条用例行；主点击负责选择，嵌套编辑按钮通过 stopPropagation 打开抽屉。
function CaseRow({
  item,
  active,
  onSelect,
  onEdit,
  strategy,
  runOverrides,
}: {
  item: RetrievalEvalCase
  active: boolean
  onSelect: (id: string) => void
  onEdit: (item: RetrievalEvalCase) => void
  strategy: EvaluationStrategy
  runOverrides: EvaluationRunOverrides
}) {
  const run = selectEvaluationRun(item, runOverrides, strategy)
  const metrics = run?.metrics
  const expectedSummary = summarizeExpected(item)
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onSelect(item.id)
    }
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect(item.id)}
      onKeyDown={onKeyDown}
      className={cn(
        'group mb-1 rounded-(--radius-control) border px-3 py-2 text-left transition-colors',
        active
          ? 'border-(--color-primary)/45 bg-(--color-primary-soft)'
          : 'border-transparent hover:border-(--color-border) hover:bg-(--color-surface-2)',
      )}
    >
      <div className="flex items-start gap-2">
        <span
          className={cn(
            'mt-1.5 inline-block size-2 rounded-full',
            item.status === 'active' ? 'bg-(--color-success)' : 'bg-(--color-text-faint)',
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-[540] text-(--color-text)" title={item.question}>
            {item.question}
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {(item.tags || []).slice(0, 2).map((tag) => (
              <Badge key={tag} tone="primary" className="max-w-24 truncate">
                {tag}
              </Badge>
            ))}
            <Badge tone={expectedSummary === '待设置期望命中' ? 'warning' : 'muted'}>
              {expectedSummary}
            </Badge>
          </div>
        </div>
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            onEdit(item)
          }}
          className="inline-flex size-7 shrink-0 items-center justify-center rounded-(--radius-control) text-(--color-text-faint) opacity-0 transition-opacity hover:bg-(--color-surface-3) hover:text-(--color-text) group-hover:opacity-100"
          title="编辑用例"
        >
          <Pencil className="size-3.5" />
        </button>
      </div>
      <div className="mt-2 grid grid-cols-4 gap-1 font-mono text-[10px] text-(--color-text-faint)">
        <span>R@K {formatPercent(metrics?.recall_at_k)}</span>
        <span>MRR {formatMetric(metrics?.mrr)}</span>
        <span>Top1 {formatPercent(metrics?.hit_rate_at_1)}</span>
        <span className="truncate text-right">{formatDateTime(run?.created_at)}</span>
      </div>
      <div className="mt-1 flex items-center gap-1 text-[10px] text-(--color-text-faint)">
        <MoreHorizontal className="size-3" />
        <span className="truncate">{displayStrategyLabel(run?.strategy)}</span>
      </div>
    </div>
  )
}
