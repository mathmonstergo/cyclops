import { useMemo, useRef, useState } from 'react'
import { BookOpen, ClipboardCheck, Loader2, Play, Plus, Search, X } from 'lucide-react'
import type {
  RetrievalEvalCase,
  RetrievalEvalCaseRecord,
  RetrievalEvalRun,
} from '@/api/schemas'
import {
  useRetrievalEvalCases,
  useRunRetrievalEvalCase,
  useSaveRetrievalEvalCase,
} from '@/api/hooks'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/cn'
import { useUi } from '@/store/ui'
import { DocumentDrawer } from './documents/document-drawer'
import { FaqDrawer } from './faqs/faq-drawer'
import { AliasPanel } from './evaluation/alias-panel'
import { EvaluationBatchPanel } from './evaluation/batch-panel'
import {
  type EvaluationBatchRunSnapshot,
  selectEvaluationBatchRunState,
} from './evaluation/batch-state'
import { CaseDrawer } from './evaluation/case-drawer'
import { EvaluationCaseList } from './evaluation/case-list'
import { buildEvaluationBatchSummary } from './evaluation/batch-diagnostics'
import { EvaluationResultPanel } from './evaluation/result-panel'
import { createEvaluationRunMutex } from './evaluation/run-mutex'
import {
  buildEvaluationRunPayload,
  CASE_STATUS_OPTIONS,
  displayStrategyLabel,
  type EvaluationRunOverrides,
  formatPercent,
  selectEvaluationRun,
  storeEvaluationRunOverride,
  type EvaluationStrategy,
} from './evaluation/helpers'

// 效果验收工作台主页面；对齐智能问答页的左栏宽度和主面板 header 位置。
export default function EvaluationPage() {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [strategy, setStrategy] = useState<EvaluationStrategy>('baseline')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [aliasOpen, setAliasOpen] = useState(false)
  const [editingCase, setEditingCase] = useState<RetrievalEvalCase | null>(null)
  const [runOverrides, setRunOverrides] = useState<EvaluationRunOverrides>({})
  const [caseOverrides, setCaseOverrides] = useState<Record<string, RetrievalEvalCaseRecord>>({})
  const [batchRunSnapshot, setBatchRunSnapshot] = useState<EvaluationBatchRunSnapshot | null>(null)
  const [runActivity, setRunActivity] = useState<
    { mode: 'single'; caseId: string } | { mode: 'batch' } | null
  >(null)
  const runMutexRef = useRef(createEvaluationRunMutex())
  const { openFaqId, setOpenFaqId, openImportFileId, setOpenImportFileId } = useUi()
  const params = useMemo(
    () => ({ status: status || undefined, limit: 100, offset: 0 }),
    [status],
  )
  const casesQuery = useRetrievalEvalCases(params)
  const runCase = useRunRetrievalEvalCase()
  const saveCase = useSaveRetrievalEvalCase()
  const items = useMemo(
    () =>
      (casesQuery.data?.items || []).map((item) =>
        caseOverrides[item.id] ? mergeEvalCase(item, caseOverrides[item.id]) : item,
      ),
    [casesQuery.data?.items, caseOverrides],
  )

  const filteredItems = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return items
    return items.filter((item) => {
      const tags = (item.tags || []).join(' ').toLowerCase()
      return (
        item.question.toLowerCase().includes(needle) ||
        tags.includes(needle) ||
        (item.intent || '').toLowerCase().includes(needle)
      )
    })
  }, [items, query])

  const effectiveSelectedId =
    selectedId && filteredItems.some((item) => item.id === selectedId)
      ? selectedId
      : filteredItems[0]?.id || null
  const selectedCase = filteredItems.find((item) => item.id === effectiveSelectedId) || null
  const selectedRun = selectedCase
    ? selectEvaluationRun(selectedCase, runOverrides, strategy)
    : null
  const effectiveRun = selectedRun
  const stats = useMemo(
    () => computeStats(items, strategy, runOverrides),
    [items, runOverrides, strategy],
  )
  const batchCases = useMemo(
    () => filteredItems.filter((item) => item.status === 'active'),
    [filteredItems],
  )
  const batchSummary = useMemo(
    () => buildEvaluationBatchSummary(filteredItems, strategy, runOverrides),
    [filteredItems, runOverrides, strategy],
  )
  const batchRunState = selectEvaluationBatchRunState(batchRunSnapshot, strategy)
  const isRunActive = runActivity !== null
  const runningSelected =
    runActivity?.mode === 'single' && runActivity.caseId === selectedCase?.id

  // 打开新建用例抽屉；主页面不固定承载编辑表单。
  const openNewCase = () => {
    setEditingCase(null)
    setDrawerOpen(true)
  }

  // 打开已有用例编辑抽屉，避免右侧常驻编辑栏挤压结果区。
  const openEditCase = (item: RetrievalEvalCase) => {
    setEditingCase(item)
    setDrawerOpen(true)
  }

  // 运行单条评测，并用本地 override 立即刷新详情区的最近运行结果。
  const handleRun = async (caseId: string) => {
    const lease = runMutexRef.current.tryAcquire()
    if (lease === null) return
    const runStrategy = strategy
    setRunActivity({ mode: 'single', caseId })
    try {
      const run = await runCase.mutateAsync({
        caseId,
        payload: buildEvaluationRunPayload(runStrategy),
      })
      setRunOverrides((current) => storeEvaluationRunOverride(current, run))
      toast.success('评测运行完成')
    } catch (error) {
      toast.error((error as Error).message || '运行评测失败')
    } finally {
      runMutexRef.current.release(lease)
      setRunActivity(null)
    }
  }

  // 顺序运行当前筛选范围内的启用用例；MVP 不创建持久化批次，只刷新 latest run。
  const handleRunBatch = async () => {
    if (batchCases.length === 0) return
    const lease = runMutexRef.current.tryAcquire()
    if (lease === null) return
    const batchStrategy = strategy
    const runPayload = buildEvaluationRunPayload(batchStrategy)
    let succeeded = 0
    let failed = 0
    setRunActivity({ mode: 'batch' })
    setBatchRunSnapshot({
      strategy: batchStrategy,
      runState: {
        status: 'running',
        total: batchCases.length,
        completed: 0,
        succeeded: 0,
        failed: 0,
        currentQuestion: batchCases[0]?.question,
      },
    })
    try {
      for (const [index, item] of batchCases.entries()) {
        setBatchRunSnapshot({
          strategy: batchStrategy,
          runState: {
            status: 'running',
            total: batchCases.length,
            completed: index,
            succeeded,
            failed,
            currentQuestion: item.question,
          },
        })
        try {
          const run = await runCase.mutateAsync({ caseId: item.id, payload: runPayload })
          succeeded += 1
          setRunOverrides((current) => storeEvaluationRunOverride(current, run))
        } catch (error) {
          failed += 1
          toast.error(`${item.question}：${(error as Error).message || '运行失败'}`)
        } finally {
          const completed = index + 1
          setBatchRunSnapshot({
            strategy: batchStrategy,
            runState: {
              status: completed === batchCases.length ? 'done' : 'running',
              total: batchCases.length,
              completed,
              succeeded,
              failed,
              currentQuestion: batchCases[index + 1]?.question,
            },
          })
        }
      }
      if (failed > 0) {
        toast.error(`批量运行完成：成功 ${succeeded}，失败 ${failed}`)
      } else {
        toast.success(`批量运行完成：${succeeded} 个用例`)
      }
    } finally {
      runMutexRef.current.release(lease)
      setRunActivity(null)
    }
  }

  // 保存后选中新用例；列表刷新由 mutation 的 query invalidation 负责。
  const handleSaved = (item: RetrievalEvalCaseRecord) => {
    setCaseOverrides((current) => ({ ...current, [item.id]: item }))
    setSelectedId(item.id)
  }

  // 从运行候选直接写入期望命中，避免用户手填内部 source/chunk id。
  const handleMarkExpected = async (
    candidate: RetrievalEvalRun['retrieved_items'][number],
    level: 'source' | 'chunk',
  ) => {
    if (!selectedCase) return
    const nextSourceIds =
      level === 'source'
        ? appendUnique(selectedCase.expected_source_ids || [], candidate.source_id)
        : []
    const nextChunkIds =
      level === 'chunk'
        ? appendUnique(selectedCase.expected_chunk_ids || [], candidate.id)
        : []
    try {
      const saved = await saveCase.mutateAsync({
        id: selectedCase.id,
        question: selectedCase.question,
        intent: selectedCase.intent,
        expected_source_ids: nextSourceIds,
        expected_chunk_ids: nextChunkIds,
        tags: selectedCase.tags,
        note: selectedCase.note,
        status: selectedCase.status,
      })
      const merged = mergeEvalCase(selectedCase, saved)
      setCaseOverrides((current) => ({ ...current, [merged.id]: merged }))
      toast.success(level === 'source' ? '已设为期望来源' : '已设为期望切片')
    } catch (error) {
      toast.error((error as Error).message || '保存期望命中失败')
    }
  }

  // 从评测候选打开已有 FAQ/文档抽屉；文档候选同时带上目标审核切片 id。
  const handleOpenCandidate = (candidate: RetrievalEvalRun['retrieved_items'][number]) => {
    if (candidate.source_type === 'faq') {
      setOpenFaqId(candidate.source_id)
      return
    }
    if (candidate.source_type === 'document') {
      setOpenImportFileId(candidate.source_id, candidate.source_chunk_id || null)
    }
  }

  return (
    <div className="flex h-full min-h-0">
      <aside className="flex h-full w-[240px] shrink-0 flex-col border-r border-(--color-border) bg-(--color-surface)">
        <div className="flex shrink-0 items-center gap-2 border-b border-(--color-border) px-3 py-2.5">
          <ClipboardCheck className="size-4 text-(--color-primary-hi)" />
          <div className="min-w-0 flex-1 text-[13px] font-[540] text-(--color-text)">
            评测用例
          </div>
          <Button variant="primary" size="sm" onClick={openNewCase} className="ml-auto">
            <Plus className="size-3.5" />
            新建
          </Button>
        </div>
        <div className="shrink-0 space-y-2 border-b border-(--color-border) p-2">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-(--color-text-faint)" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜问题 / 标签 / 意图…"
              className="pl-7"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery('')}
                className="absolute right-1 top-1/2 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded text-(--color-text-faint) hover:text-(--color-text)"
              >
                <X className="size-3" />
              </button>
            )}
          </div>
          <SegmentedFilter value={status} onChange={setStatus} options={CASE_STATUS_OPTIONS} />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
          <EvaluationCaseList
            items={filteredItems}
            activeId={effectiveSelectedId}
            isPending={casesQuery.isPending}
            isError={casesQuery.isError}
            onRetry={() => void casesQuery.refetch()}
            onSelect={setSelectedId}
            onEdit={openEditCase}
            strategy={strategy}
            runOverrides={runOverrides}
          />
        </div>
        <div className="shrink-0 border-t border-(--color-border) px-3 py-2 text-[11px] text-(--color-text-faint)">
          {filteredItems.length} / {casesQuery.data?.total ?? items.length} 个用例
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center gap-2 border-b border-(--color-border) bg-(--color-surface) px-5 py-3">
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <div className="truncate text-[14px] text-(--color-text)" title={selectedCase?.question}>
                {selectedCase?.question || '效果验收'}
              </div>
              {selectedCase && (
                <Badge tone={selectedCase.status === 'active' ? 'success' : 'muted'}>
                  {selectedCase.status === 'active' ? '启用' : '禁用'}
                </Badge>
              )}
            </div>
            <div className="mt-0.5 truncate text-[11px] text-(--color-text-faint)">
              用例 {casesQuery.data?.total ?? items.length} · 命中率 {formatPercent(stats.averageRecall)} · 待补期望 {stats.missingExpected}
              {effectiveRun?.strategy ? ` · ${displayStrategyLabel(effectiveRun.strategy)}` : ''}
            </div>
          </div>
          <Button variant="outline" size="sm" onClick={() => setAliasOpen(true)}>
            <BookOpen className="size-3.5" />
            别名词典
          </Button>
          <StrategyControl
            value={strategy}
            onChange={setStrategy}
            disabled={isRunActive}
          />
          <Button
            variant="primary"
            size="sm"
            onClick={() => selectedCase && void handleRun(selectedCase.id)}
            disabled={!selectedCase || isRunActive || selectedCase.status !== 'active'}
            title={selectedCase?.status === 'active' ? '运行单条评测' : '禁用用例不可运行'}
          >
            {runningSelected ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
            {runningSelected ? '运行中…' : '运行单条'}
          </Button>
        </header>

        <div className="min-h-0 flex-1">
          <div className="flex h-full min-h-0 flex-col">
            <EvaluationBatchPanel
              summary={batchSummary}
              runState={batchRunState}
              batchCaseCount={batchCases.length}
              runDisabled={isRunActive}
              onRunBatch={() => void handleRunBatch()}
              onSelectCase={setSelectedId}
            />
            <div className="min-h-0 flex-1">
              <EvaluationResultPanel
                evalCase={selectedCase}
                run={selectedRun}
                onMarkExpected={handleMarkExpected}
                onOpenCandidate={handleOpenCandidate}
                markingExpected={saveCase.isPending}
              />
            </div>
          </div>
        </div>
      </main>

      <AliasPanel open={aliasOpen} onOpenChange={setAliasOpen} />

      <CaseDrawer
        open={drawerOpen}
        item={editingCase}
        onOpenChange={setDrawerOpen}
        onSaved={handleSaved}
      />
      <FaqDrawer
        faqId={openFaqId}
        onClose={() => setOpenFaqId(null)}
        onCreated={(id) => setOpenFaqId(id)}
      />
      <DocumentDrawer
        fileId={openImportFileId}
        onClose={() => setOpenImportFileId(null)}
      />
    </div>
  )
}

// 评测策略分段控件只暴露 baseline 与显式 KG debug，不影响正式问答默认策略。
function StrategyControl({
  value,
  onChange,
  disabled,
}: {
  value: EvaluationStrategy
  onChange: (value: EvaluationStrategy) => void
  disabled: boolean
}) {
  const options: { value: EvaluationStrategy; label: string }[] = [
    { value: 'baseline', label: '基线' },
    { value: 'kg_debug', label: 'KG 调试' },
  ]
  return (
    <div className="flex items-center gap-0.5 rounded-(--radius-control) border border-(--color-border) bg-(--color-surface-2) p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          disabled={disabled}
          aria-pressed={value === option.value}
          className={cn(
            'cursor-pointer rounded-(--radius-control) px-2.5 py-1 text-[12px] transition-colors disabled:cursor-not-allowed disabled:opacity-50',
            value === option.value
              ? 'bg-(--color-primary) text-white'
              : 'text-(--color-text-muted) hover:text-(--color-text)',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

// 合并服务端保存后的用例快照；运行历史只以列表接口的 latest_runs 为准。
function mergeEvalCase(
  current: RetrievalEvalCase,
  saved: RetrievalEvalCaseRecord,
): RetrievalEvalCase {
  return {
    ...current,
    ...saved,
    latest_runs: current.latest_runs,
  }
}

// 追加唯一 id；空值不写入，避免候选缺字段时污染评测口径。
function appendUnique(values: string[], value: string | undefined | null): string[] {
  const clean = String(value || '').trim()
  if (!clean || values.includes(clean)) return values
  return [...values, clean]
}

// 统计顶部工具栏指标；只读用例快照，不在这里触发数据请求。
function computeStats(
  items: RetrievalEvalCase[],
  strategy: EvaluationStrategy,
  runOverrides: EvaluationRunOverrides,
): {
  averageRecall?: number
  missingExpected: number
} {
  let recallSum = 0
  let recallCount = 0
  let missingExpected = 0
  for (const item of items) {
    if ((item.expected_source_ids?.length || 0) === 0 && (item.expected_chunk_ids?.length || 0) === 0) {
      missingExpected += 1
    }
    const recall = selectEvaluationRun(item, runOverrides, strategy)?.metrics?.recall_at_k
    if (typeof recall === 'number') {
      recallSum += recall
      recallCount += 1
    }
  }
  return {
    averageRecall: recallCount > 0 ? recallSum / recallCount : undefined,
    missingExpected,
  }
}

// 状态筛选分段控件；约束为单选，宽度按 240px 左栏收敛。
function SegmentedFilter({
  value,
  onChange,
  options,
}: {
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <div className="grid grid-cols-3 gap-1 rounded-(--radius-control) border border-(--color-border) bg-(--color-surface-2) p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={cn(
            'rounded-(--radius-control) px-2.5 py-1 text-[12px] transition-colors',
            value === option.value
              ? 'bg-(--color-primary) text-white'
              : 'text-(--color-text-muted) hover:text-(--color-text)',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
