import { useEffect, useMemo, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import {
  Bot,
  Check,
  ChevronLeft,
  ChevronRight,
  Edit3,
  EyeOff,
  Eye,
  ListFilter,
  Loader2,
  MessageCircleQuestion,
  Save,
  Waypoints,
  X,
} from 'lucide-react'
import type { ImportChunk } from '@/api/schemas'
import { Button } from '@/components/ui/button'
import { TONE_COLOR, embedDotTone, type DotTone } from '@/components/ui/status-dot-utils'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Textarea } from '@/components/ui/textarea'
import { SourceBlockPreview } from '@/components/shared/source-block-preview'
import {
  useEmbedImportChunk,
  useCreateKgExtractionJob,
  useToggleImportChunkDisabled,
  useUpdateImportChunk,
} from '@/api/hooks'
import { toast } from '@/components/ui/toast'
import { useUi } from '@/store/ui'
import { cn } from '@/lib/cn'
import { ease, dur } from '@/lib/motion'
import { embeddingStatusLabel, tr } from '@/lib/labels'
import { useHorizontalWheelScroll } from '@/lib/use-horizontal-wheel-scroll'
import { formatChunkPageLocator, formatKgExtractionResult } from './kg-actions'
import { CopyIdButton } from './copy-id-button'
import { HoverTooltip, HoverTooltipTrigger } from './hover-tooltip'

export function ChunkBrowser({ fileId, chunks, fileDisabled }: { fileId: string; chunks: ImportChunk[]; fileDisabled?: boolean }) {
  const {
    currentChunkIndex,
    setCurrentChunkIndex,
    chunkEditMode,
    setChunkEditMode,
    openImportChunkId,
    setOpenImportChunkId,
  } = useUi()
  const toggleDisabled = useToggleImportChunkDisabled()

  useEffect(() => {
    // 文件切换时复位
    setCurrentChunkIndex(0)
  }, [fileId, setCurrentChunkIndex])

  useEffect(() => {
    if (!openImportChunkId) return
    const targetIndex = chunks.findIndex((item) => item.id === openImportChunkId)
    if (targetIndex >= 0) {
      setCurrentChunkIndex(targetIndex)
      setOpenImportChunkId(null)
    }
  }, [chunks, openImportChunkId, setCurrentChunkIndex, setOpenImportChunkId])

  const safeIdx = Math.min(currentChunkIndex, Math.max(0, chunks.length - 1))
  const chunk = chunks[safeIdx]

  if (!chunks.length) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-[13px] text-(--color-text-faint)">
        这份文档还没有切片；先点上方「开始解析」。
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 切片导航：横向滚动 chips，与外部按钮组、抽屉头部对齐到 24px 左缘。key=fileId 让状态筛选随文件切换复位 */}
      <ChunkNav
        key={fileId}
        chunks={chunks}
        activeIndex={safeIdx}
        onJump={setCurrentChunkIndex}
        fileDisabled={fileDisabled}
      />
      {/* 切片元信息与操作 */}
      <ChunkToolbar
        chunk={chunk}
        fileDisabled={fileDisabled}
        editMode={chunkEditMode}
        onEditToggle={() => setChunkEditMode(!chunkEditMode)}
        onToggleDisabled={() =>
          toggleDisabled.mutate({ id: chunk.id, is_disabled: !chunk.is_disabled })
        }
      />
      {/* 切片正文：所有内容统一 px-6 与上方对齐 */}
      <ChunkContent
        key={chunk.id}
        chunk={chunk}
        fileId={fileId}
        editMode={chunkEditMode}
        onExitEdit={() => setChunkEditMode(false)}
      />
    </div>
  )
}

// 切片正文按 chunk id 初始化草稿，避免在 effect 中同步外部数据到本地编辑态。
function ChunkContent({
  chunk,
  fileId,
  editMode,
  onExitEdit,
}: {
  chunk: ImportChunk
  fileId: string
  editMode: boolean
  onExitEdit: () => void
}) {
  const update = useUpdateImportChunk()
  const [draft, setDraft] = useState(chunk.source_text || '')

  return (
    <div className="min-h-0 flex-1 overflow-y-auto scroll-thin px-6 py-5">
      <motion.div
        key={chunk.id + (editMode ? '-edit' : '-view')}
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: dur.base, ease: ease.out }}
        className="flex flex-col gap-4"
      >
        {chunk.questions?.length > 0 && <QuestionsBlock questions={chunk.questions} />}
        {editMode ? (
          <div className="flex flex-col gap-2">
            <Textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={14} />
            <div className="flex items-center justify-end gap-2">
              <span className="mr-auto text-[11px] text-(--color-text-faint)">
                保存后切片 embedding 会变 stale，需要重新生成
              </span>
              <Button
                variant="ghost"
                onClick={() => {
                  setDraft(chunk.source_text || '')
                  onExitEdit()
                }}
              >
                取消
              </Button>
              <Button
                variant="primary"
                disabled={update.isPending || draft === chunk.source_text}
                onClick={async () => {
                  try {
                    await update.mutateAsync({ id: chunk.id, source_text: draft })
                    toast.success('切片已保存，向量已标记过期，记得重新生成')
                    onExitEdit()
                  } catch (e) {
                    toast.error((e as Error).message || '保存失败')
                  }
                }}
              >
                <Save className="size-3.5" />
                保存切片
              </Button>
            </div>
          </div>
        ) : chunk.source_blocks?.length ? (
          <SourceBlockPreview blocks={chunk.source_blocks} fileId={fileId} />
        ) : (
          <pre className="whitespace-pre-wrap text-[13px] text-(--color-text)">
            {chunk.source_text}
          </pre>
        )}
      </motion.div>
    </div>
  )
}

function ChunkNav({
  chunks,
  activeIndex,
  onJump,
  fileDisabled,
}: {
  chunks: ImportChunk[]
  activeIndex: number
  onJump: (i: number) => void
  fileDisabled?: boolean
}) {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<HTMLButtonElement>(null)
  useHorizontalWheelScroll(scrollerRef)
  // 状态筛选：空集 = 不筛选（全显）；key 为 embedding 状态值或特殊键 'disabled'。
  const [filter, setFilter] = useState<Set<string>>(() => new Set())

  // 当前 chip 滚动到可视区
  useEffect(() => {
    const el = activeRef.current
    if (!el || !scrollerRef.current) return
    el.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' })
  }, [activeIndex])

  // 命中筛选的切片下标（保留绝对 #编号，未命中的不渲染）
  const visibleIndices = useMemo(
    () => chunks.map((_, i) => i).filter((i) => chunkMatchesFilter(filter, chunks[i], fileDisabled)),
    [chunks, filter, fileDisabled],
  )
  // 上一/下一：在命中筛选的可见集合内移动
  const prevTarget = [...visibleIndices].reverse().find((i) => i < activeIndex)
  const nextTarget = visibleIndices.find((i) => i > activeIndex)

  // 筛选项按数据动态派生：实际出现的 embedding 状态（固定展示顺序）+ 禁用（若有）。
  const statusCounts = useMemo(() => {
    const m = new Map<string, number>()
    for (const c of chunks) {
      const k = c.embedding_status || 'pending'
      m.set(k, (m.get(k) || 0) + 1)
    }
    return m
  }, [chunks])
  const disabledCount = useMemo(
    () => chunks.filter((c) => !!fileDisabled || c.is_disabled).length,
    [chunks, fileDisabled],
  )
  const options = [
    ...FILTER_STATUS_ORDER.filter((s) => statusCounts.has(s)).map((s) => ({
      key: s,
      label: tr(embeddingStatusLabel, s, s),
      tone: embedDotTone(s, false),
      count: statusCounts.get(s) || 0,
    })),
    ...(disabledCount > 0
      ? [{ key: 'disabled', label: '已禁用', tone: 'muted' as DotTone, count: disabledCount }]
      : []),
  ]

  const toggle = (key: string) => {
    const next = new Set(filter)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setFilter(next)
    // 定位：筛选后若当前 chip 不在命中集合，跳到第一个命中的切片
    if (next.size > 0) {
      const vis = chunks.map((_, i) => i).filter((i) => chunkMatchesFilter(next, chunks[i], fileDisabled))
      if (vis.length && !vis.includes(activeIndex)) onJump(vis[0])
    }
  }
  const filtering = filter.size > 0

  return (
    <div className="flex shrink-0 items-center gap-1.5 border-b border-(--color-border) px-6 py-2">
      <span className="shrink-0 text-[11px] uppercase tracking-wider text-(--color-text-faint)">
        切片{' '}
        <span className="text-(--color-text-muted)">
          {filtering ? `${visibleIndices.length} / ${chunks.length}` : chunks.length}
        </span>
      </span>
      <HoverTooltipTrigger content="上一个切片" disabled={prevTarget === undefined}>
        <Button
          variant="ghost"
          size="icon"
          className="size-6 cursor-pointer"
          disabled={prevTarget === undefined}
          aria-label="上一个切片"
          onClick={() => {
            if (prevTarget !== undefined) onJump(prevTarget)
          }}
        >
          <ChevronLeft className="size-3.5" />
        </Button>
      </HoverTooltipTrigger>
      <div
        ref={scrollerRef}
        className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto scroll-thin scroll-smooth pb-0.5"
      >
        {visibleIndices.length === 0 ? (
          <span className="px-1 text-[11px] text-(--color-text-faint)">没有匹配的切片</span>
        ) : (
          visibleIndices.map((i) => {
            const c = chunks[i]
            const isActive = i === activeIndex
            // 禁用（文件层或切片层任一）置灰并覆盖；其余按 embedding 状态一色一态。
            const disabled = !!fileDisabled || c.is_disabled
            const tone = embedDotTone(c.embedding_status, disabled)
            const title = c.section_path?.join(' > ') || c.block_type || `切片 #${i + 1}`
            const statusText = disabled
              ? '已禁用'
              : tr(embeddingStatusLabel, c.embedding_status, '未索引')
            const qCount = c.questions?.length || 0
            return (
              <Tooltip key={c.id}>
                <TooltipTrigger asChild>
                  <button
                    ref={isActive ? activeRef : undefined}
                    type="button"
                    onClick={() => onJump(i)}
                    className={cn(
                      'group inline-flex shrink-0 items-center gap-1 rounded-(--radius-control) border px-2 py-1 text-[11px] transition-colors',
                      isActive
                        ? 'border-(--color-primary)/40 bg-(--color-primary-soft) text-(--color-text)'
                        : 'border-transparent text-(--color-text-muted) hover:bg-(--color-surface-2) hover:text-(--color-text)',
                      c.is_disabled && 'opacity-50',
                    )}
                  >
                    <span className="font-mono text-(--color-text-faint) group-hover:text-(--color-text-muted)">
                      #{i + 1}
                    </span>
                    <span className={cn('size-1 rounded-full', TONE_COLOR[tone])} />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="bottom" className="max-w-[260px]">
                  <div className="flex flex-col gap-1">
                    <span className="font-medium break-words">{title}</span>
                    <span className="flex items-center gap-1.5 text-(--color-text-muted)">
                      <span className={cn('size-1.5 shrink-0 rounded-full', TONE_COLOR[tone])} />
                      {statusText}
                    </span>
                    {qCount > 0 && (
                      <span className="text-(--color-text-faint)">{qCount} 个假设问题</span>
                    )}
                  </div>
                </TooltipContent>
              </Tooltip>
            )
          })
        )}
      </div>
      <HoverTooltipTrigger content="下一个切片" disabled={nextTarget === undefined}>
        <Button
          variant="ghost"
          size="icon"
          className="size-6 cursor-pointer"
          disabled={nextTarget === undefined}
          aria-label="下一个切片"
          onClick={() => {
            if (nextTarget !== undefined) onJump(nextTarget)
          }}
        >
          <ChevronRight className="size-3.5" />
        </Button>
      </HoverTooltipTrigger>
      {/* 状态筛选 */}
      <Popover>
        <PopoverTrigger asChild>
          <Button
            variant={filtering ? 'primary' : 'ghost'}
            size="icon"
            className="size-6 shrink-0 cursor-pointer"
            aria-label="按状态筛选切片"
          >
            <ListFilter className="size-3.5" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-52 p-1.5">
          <div className="flex items-center justify-between px-1.5 pb-1.5 text-[11px] text-(--color-text-faint)">
            <span>按状态筛选</span>
            {filtering && (
              <button
                type="button"
                onClick={() => setFilter(new Set())}
                className="text-(--color-primary-hi) hover:underline"
              >
                清除
              </button>
            )}
          </div>
          <div className="flex flex-col">
            {options.map((o) => {
              const checked = filter.has(o.key)
              return (
                <button
                  key={o.key}
                  type="button"
                  onClick={() => toggle(o.key)}
                  className="flex items-center gap-2 rounded-(--radius-control) px-1.5 py-1.5 text-[12px] text-(--color-text) hover:bg-(--color-surface-2)"
                >
                  <span
                    className={cn(
                      'flex size-3.5 shrink-0 items-center justify-center rounded-[4px] border',
                      checked
                        ? 'border-(--color-primary) bg-(--color-primary)'
                        : 'border-(--color-border)',
                    )}
                  >
                    {checked && <Check className="size-2.5 text-(--color-text)" />}
                  </span>
                  <span className={cn('size-1.5 shrink-0 rounded-full', TONE_COLOR[o.tone])} />
                  <span className="flex-1 text-left">{o.label}</span>
                  <span className="text-(--color-text-faint)">{o.count}</span>
                </button>
              )
            })}
          </div>
        </PopoverContent>
      </Popover>
    </div>
  )
}

// 切片工具栏集中处理编辑、停用、embedding 与显式 document_chunk KG 抽取。
function ChunkToolbar({
  chunk,
  fileDisabled,
  editMode,
  onEditToggle,
  onToggleDisabled,
}: {
  chunk: ImportChunk
  fileDisabled?: boolean
  editMode: boolean
  onEditToggle: () => void
  onToggleDisabled: () => void
}) {
  const embedChunk = useEmbedImportChunk()
  const createKgJob = useCreateKgExtractionJob()
  const isEmbedding =
    embedChunk.isPending && embedChunk.variables === chunk.id
  const isExtractingKg =
    createKgJob.isPending && createKgJob.variables?.source_id === chunk.id
  const cannotExtractKg = editMode || !!fileDisabled || chunk.is_disabled
  // 切片向量需要刷新的两种状态：编辑后被自动标 stale；上次生成失败。
  const needsEmbed = chunk.embedding_status === 'stale' || chunk.embedding_status === 'failed'

  const pageLocator = formatChunkPageLocator(chunk)
  const sectionLeaf = chunk.section_path?.length
    ? chunk.section_path[chunk.section_path.length - 1]
    : null
  return (
    <div className="flex shrink-0 items-center justify-between gap-2 border-b border-(--color-border-soft) bg-(--color-surface) px-6 py-1">
      {/* 状态已统一到滚轴圆点 + hover，这里只留段落 / 页码等定位信息，保持工具栏清爽 */}
      <div className="flex min-w-0 flex-1 items-center gap-2 text-[12px] text-(--color-text-muted)">
        {sectionLeaf && (
          <HoverTooltip content={chunk.section_path?.join(' > ') || sectionLeaf}>
            <span className="max-w-[8em] truncate text-(--color-text)">
              {sectionLeaf}
            </span>
          </HoverTooltip>
        )}
        {pageLocator && (
          <span className="text-(--color-text-faint)">
            {sectionLeaf ? '· ' : ''}
            {pageLocator}
          </span>
        )}
        <CopyIdButton label="切片ID" value={chunk.id} />
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <HoverTooltipTrigger
          content={
            cannotExtractKg
              ? '切片可用且非编辑状态时才能抽取 KG 候选'
              : '从当前切片抽取 KG 候选'
          }
          disabled={cannotExtractKg || isExtractingKg}
        >
          <Button
            variant="outline"
            size="sm"
            className="h-6 cursor-pointer gap-1 px-1.5 text-[11px]"
            disabled={cannotExtractKg || isExtractingKg}
            onClick={async () => {
              try {
                // 当前上下文已明确是切片，不通过 ID 形状推断来源类型。
                const result = await createKgJob.mutateAsync({
                  source_id: chunk.id,
                  source_type: 'document_chunk',
                })
                toast.success(formatKgExtractionResult(result))
              } catch (e) {
                toast.error((e as Error).message || 'KG 抽取失败')
              }
            }}
          >
            {isExtractingKg ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Bot className="size-3" />
            )}
            KG 抽取
          </Button>
        </HoverTooltipTrigger>
        <HoverTooltipTrigger
          content={needsEmbed ? '切片原文有改动，向量已 stale' : '重新生成该切片向量'}
          disabled={isEmbedding || editMode}
        >
          <Button
            variant={needsEmbed ? 'primary' : 'ghost'}
            size="sm"
            className="h-6 cursor-pointer gap-1 px-1.5 text-[11px]"
            disabled={isEmbedding || editMode}
            onClick={async () => {
              try {
                const res = await embedChunk.mutateAsync(chunk.id)
                const count = res?.count ?? 0
                toast.success(`已重新生成切片向量（${count} 条）`)
              } catch (e) {
                toast.error((e as Error).message || '重新生成失败')
              }
            }}
          >
            {isEmbedding ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Waypoints className="size-3" />
            )}
            Embedding
          </Button>
        </HoverTooltipTrigger>
        <HoverTooltipTrigger content={chunk.is_disabled ? '启用切片' : '禁用切片'}>
          <Button
            variant={chunk.is_disabled ? 'default' : 'ghost'}
            size="icon"
            className="size-6 cursor-pointer"
            onClick={onToggleDisabled}
            aria-label={chunk.is_disabled ? '启用切片' : '禁用切片'}
          >
            {chunk.is_disabled ? <Eye className="size-3" /> : <EyeOff className="size-3" />}
          </Button>
        </HoverTooltipTrigger>
        <HoverTooltipTrigger content={editMode ? '退出编辑' : '编辑原文'}>
          <Button
            variant={editMode ? 'primary' : 'ghost'}
            size="icon"
            className="size-6 cursor-pointer"
            onClick={onEditToggle}
            aria-label={editMode ? '退出编辑' : '编辑原文'}
          >
            {editMode ? <X className="size-3" /> : <Edit3 className="size-3" />}
          </Button>
        </HoverTooltipTrigger>
      </div>
    </div>
  )
}

function QuestionsBlock({ questions }: { questions: string[] }) {
  const list = useMemo(() => questions.filter(Boolean), [questions])
  if (!list.length) return null
  return (
    <details open className="surface rounded-(--radius-control) px-3 py-2 text-[13px]">
      <summary className="cursor-pointer select-none text-(--color-text-muted) [&::-webkit-details-marker]:hidden">
        <MessageCircleQuestion className="mr-1.5 inline size-3.5 -translate-y-px" />
        假设问题 <span className="text-(--color-text-faint)">({list.length})</span>
      </summary>
      <ul className="mt-2 ml-5 list-disc space-y-1 text-(--color-text)">
        {list.map((q, i) => (
          <li key={i}>{q}</li>
        ))}
      </ul>
    </details>
  )
}

// 筛选下拉里 embedding 状态的展示顺序（仅展示数据中实际出现的）。
const FILTER_STATUS_ORDER = ['ready', 'stale', 'failed', 'partial', 'pending']

// 纯匹配：active 为选中的状态键集合（空 = 全显）；'disabled' 命中文件层或切片层禁用。
function chunkMatchesFilter(active: Set<string>, c: ImportChunk, fileDisabled?: boolean) {
  if (active.size === 0) return true
  if (active.has('disabled') && (!!fileDisabled || c.is_disabled)) return true
  return active.has(c.embedding_status || 'pending')
}
