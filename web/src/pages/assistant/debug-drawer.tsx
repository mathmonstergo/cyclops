// 右侧 Debug 抽屉：显示当前会话最近一条助手消息的完整流程（step list + 命中切片明细）。
// 默认关闭；从顶栏的「流程详情」按钮或助手气泡上的「查看 N 条来源」唤起。
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  FileText,
  Loader2,
} from 'lucide-react'
import {
  Drawer,
  DrawerBody,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer'
import { cn } from '@/lib/cn'
import { confidenceLabel, intentLabel, tr } from '@/lib/labels'
import { dur, ease } from '@/lib/motion'
import { useAssistant, type ChatMessage } from '@/store/assistant'
import type { AssistantSource } from '@/api/schemas'
import type { AssistantStepEvent } from '@/lib/sse-assistant'
import {
  buildAssistantSourceTargetKey,
  findAssistantSourceTargetIndex,
  type AssistantSourceTarget,
} from './source-target'

// 流程详情抽屉；关键约束是来源 chip 可指定消息和来源卡片进行滚动高亮。
export function DebugDrawer({
  open,
  onOpenChange,
  conversationId,
  sourceTarget,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  conversationId: string
  sourceTarget?: AssistantSourceTarget | null
}) {
  const conv = useAssistant((s) => s.conversations[conversationId])
  const assistantMessages = useMemo(
    () => conv?.messages.filter((m) => m.role === 'assistant') ?? [],
    [conv?.messages],
  )
  const targetAsst = sourceTarget?.message_id
    ? assistantMessages.find((m) => m.id === sourceTarget.message_id)
    : undefined
  const lastAsst = targetAsst ?? assistantMessages.at(-1)

  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      <AnimatePresence>
        {open && (
          <DrawerContent width={560}>
            <DrawerHeader>
              <div>
                <DrawerTitle>流程详情</DrawerTitle>
                <p className="mt-1 text-[12px] text-(--color-text-muted)">
                  最近一次回答的 RAG 链路 · 步骤耗时 · 命中切片
                </p>
              </div>
            </DrawerHeader>
            <DrawerBody>
              {!lastAsst ? (
                <EmptyState />
              ) : (
                <div className="flex flex-col gap-6">
                  <StepsBlock msg={lastAsst} />
                  <SourcesBlock
                    sources={lastAsst.sources || []}
                    sourceTarget={sourceTarget}
                    open={open}
                  />
                </div>
              )}
            </DrawerBody>
          </DrawerContent>
        )}
      </AnimatePresence>
    </Drawer>
  )
}

// 抽屉空态；关键约束是只提示当前无问答记录，不引导额外流程。
function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      <Activity className="size-6 text-(--color-text-faint)" />
      <div className="text-[13px] text-(--color-text-muted)">还没有问答记录</div>
      <div className="text-[11px] text-(--color-text-faint)">发送一个问题后这里会展示完整流程</div>
    </div>
  )
}

// 格式化步骤摘要；关键约束是把后端英文意图/置信字段转成操作员可读中文。
function prettySummary(step: AssistantStepEvent): string {
  const raw = step.summary || ''
  if (step.step_id === 'intent_detection' && raw.includes('/')) {
    const [intent, conf] = raw.split('/').map((s) => s.trim())
    return `${tr(intentLabel, intent, intent)} · 置信 ${tr(confidenceLabel, conf, conf)}`
  }
  return raw
}

// 展示 RAG 处理步骤；关键约束是保留 step 顺序和后端耗时。
function StepsBlock({ msg }: { msg: ChatMessage }) {
  const steps = msg.steps || []
  return (
    <section>
      <SectionTitle title="处理步骤" count={steps.length} />
      {steps.length === 0 ? (
        <div className="text-[12px] text-(--color-text-faint)">尚无步骤</div>
      ) : (
        <ol className="flex flex-col gap-2">
          {steps.map((s, i) => (
            <li
              key={`${s.step_id}-${i}`}
              className="rounded-(--radius-control) border border-(--color-border-soft) bg-(--color-surface) px-3 py-2"
            >
              <div className="flex items-center gap-2 text-[12px]">
                {s.status === 'failed' ? (
                  <AlertTriangle className="size-3.5 text-(--color-danger)" />
                ) : s.status === 'running' ? (
                  <Loader2 className="size-3.5 animate-spin text-(--color-primary-hi)" />
                ) : (
                  <CheckCircle2 className="size-3.5 text-(--color-success)" />
                )}
                <span className="text-(--color-text)">{s.title || s.step_id}</span>
                <span className="ml-auto font-mono text-[10px] text-(--color-text-faint)">
                  {typeof s.duration_ms === 'number' ? `${s.duration_ms}ms` : ''}
                </span>
              </div>
              {prettySummary(s) && (
                <div className="mt-1 text-[12px] text-(--color-text-muted)">{prettySummary(s)}</div>
              )}
              <ExtraTags step={s} />
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

// 渲染步骤调试标签；关键约束是只展示已序列化的安全调试字段。
function ExtraTags({ step }: { step: AssistantStepEvent }) {
  const tags: string[] = []
  if (step.analysis && typeof step.analysis === 'object') {
    const intentRaw = (step.analysis as { intent?: string }).intent
    const conf = (step.analysis as { confidence?: number | string }).confidence
    if (intentRaw) tags.push(`意图=${tr(intentLabel, intentRaw, intentRaw)}`)
    if (conf !== undefined) {
      const confZh = typeof conf === 'string' ? tr(confidenceLabel, conf, String(conf)) : String(conf)
      tags.push(`置信=${confZh}`)
    }
    const rewrite = (step.analysis as { query_rewrite?: string }).query_rewrite
    if (rewrite) tags.push(`改写="${rewrite}"`)
  }
  if (Array.isArray(step.documents) && step.documents.length > 0) {
    tags.push(`命中=${step.documents.length}`)
  }
  if (typeof step.top_k === 'number') tags.push(`top_k=${step.top_k}`)
  if (typeof step.vector_count === 'number') tags.push(`向量=${step.vector_count}`)
  if (typeof step.keyword_count === 'number') tags.push(`关键词=${step.keyword_count}`)
  if (typeof step.dimensions === 'number') tags.push(`维度=${step.dimensions}`)
  if (tags.length === 0) return null
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {tags.map((t) => (
        <span
          key={t}
          className="rounded-(--radius-control) bg-(--color-surface-2) px-1.5 py-0.5 font-mono text-[10px] text-(--color-text-muted)"
        >
          {t}
        </span>
      ))}
    </div>
  )
}

// 展示命中来源列表；关键约束是按目标来源滚动到具体卡片并短暂高亮。
function SourcesBlock({
  sources,
  sourceTarget,
  open,
}: {
  sources: AssistantSource[]
  sourceTarget?: AssistantSourceTarget | null
  open: boolean
}) {
  const sourceRefs = useRef<Record<string, HTMLElement | null>>({})
  const [highlightedKey, setHighlightedKey] = useState<string | null>(null)
  const targetIndex = useMemo(
    () => findAssistantSourceTargetIndex(sources, sourceTarget),
    [sources, sourceTarget],
  )

  useEffect(() => {
    if (!open || targetIndex < 0) return undefined
    const source = sources[targetIndex]
    if (!source) return undefined
    const key = buildAssistantSourceTargetKey(source)
    const timeout = window.setTimeout(() => {
      sourceRefs.current[key]?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setHighlightedKey(null)
      window.requestAnimationFrame(() => setHighlightedKey(key))
    }, 120)
    return () => window.clearTimeout(timeout)
  }, [open, sources, targetIndex])

  useEffect(() => {
    if (!highlightedKey) return undefined
    const timeout = window.setTimeout(() => setHighlightedKey(null), 1300)
    return () => window.clearTimeout(timeout)
  }, [highlightedKey])

  const registerSourceRef = (key: string, node: HTMLElement | null): void => {
    sourceRefs.current[key] = node
  }

  return (
    <section>
      <SectionTitle title="命中切片" count={sources.length} />
      {sources.length === 0 ? (
        <div className="text-[12px] text-(--color-text-faint)">未检索到来源</div>
      ) : (
        <div className="flex flex-col gap-2">
          {sources.map((src, i) => {
            const key = buildAssistantSourceTargetKey(src)
            return (
              <SourceCard
                key={`${key}-${i}`}
                refKey={key}
                src={src}
                index={i}
                highlighted={highlightedKey === key}
                registerRef={registerSourceRef}
              />
            )
          })}
        </div>
      )}
    </section>
  )
}

// 单个来源卡片；关键约束是保留溯源字段并支持定位高亮。
function SourceCard({
  src,
  index,
  refKey,
  highlighted,
  registerRef,
}: {
  src: AssistantSource
  index: number
  refKey: string
  highlighted: boolean
  registerRef: (key: string, node: HTMLElement | null) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const contentId = useId()
  const isFaq = src.source_type === 'faq'
  const title = src.source_title || `来源 ${index + 1}`
  const fullText = src.content
  const score = src.score
  const channels = src.retrieval_channels || []
  const page = src.page_start
  const hasMore = fullText.length > 200

  return (
    <article
      ref={(node) => registerRef(refKey, node)}
      className={cn(
        'rounded-(--radius-control) border border-(--color-border) bg-(--color-surface)',
        'transition-colors',
        hasMore && 'hover:border-(--color-primary)/30',
        highlighted && 'assistant-flash-highlight',
      )}
    >
      <button
        type="button"
        disabled={!hasMore}
        aria-expanded={hasMore ? expanded : undefined}
        aria-controls={hasMore ? contentId : undefined}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-[12px] enabled:cursor-pointer disabled:cursor-default"
      >
        {channels.map((c) => (
          <span
            key={c}
            className={cn(
              'shrink-0 rounded-(--radius-control) px-1.5 py-0.5 font-mono text-[10px]',
              c === 'parent_context'
                ? 'bg-(--color-warning)/15 text-(--color-warning)'
                : 'bg-(--color-surface-2) text-(--color-text-muted)',
            )}
          >
            {c}
          </span>
        ))}
        <span
          className={cn(
            'shrink-0 rounded-(--radius-control) px-1.5 py-0.5 font-mono text-[10px]',
            isFaq
              ? 'bg-(--color-primary-soft) text-(--color-primary-hi)'
              : 'bg-(--color-surface-2) text-(--color-text-muted)',
          )}
        >
          {isFaq ? 'FAQ' : '文档'}
        </span>
        <FileText className="size-3.5 shrink-0 text-(--color-text-faint)" />
        <span className="min-w-0 flex-1 truncate text-(--color-text)" title={title}>
          {title}
        </span>
        {typeof page === 'number' && (
          <span className="shrink-0 font-mono text-[10px] text-(--color-text-faint)">
            p.{page}
          </span>
        )}
        {score !== undefined && (
          <span className="shrink-0 font-mono text-[10px] text-(--color-text-faint)">
            {score.toFixed(3)}
          </span>
        )}
        {hasMore && (
          <ChevronDown
            className={cn(
              'size-3.5 shrink-0 text-(--color-text-faint) transition-transform',
              expanded && 'rotate-180',
            )}
          />
        )}
      </button>
      {fullText && (
        <div id={contentId}>
          <AnimatePresence initial={false} mode="wait">
            {expanded ? (
              <motion.div
                key="full"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: dur.base, ease: ease.out }}
                className="overflow-hidden"
              >
              {isFaq && src.question && (
                <div className="border-t border-(--color-border-soft) px-3 py-2 text-[12px] leading-[1.7]">
                  <div className="mb-1 text-[10px] uppercase tracking-wider text-(--color-text-faint)">
                    问题
                  </div>
                  <div className="text-(--color-text) whitespace-pre-wrap break-words">
                    {src.question}
                  </div>
                </div>
              )}
              {fullText && (
                <div className="border-t border-(--color-border-soft) px-3 py-2 text-[12px] leading-[1.7]">
                  {isFaq && (
                    <div className="mb-1 text-[10px] uppercase tracking-wider text-(--color-text-faint)">
                      答案
                    </div>
                  )}
                  <div className="text-(--color-text-muted) whitespace-pre-wrap break-words">
                    {fullText}
                  </div>
                </div>
              )}
              </motion.div>
            ) : (
              <div
                key="preview"
                className="line-clamp-3 px-3 pb-2 text-[12px] leading-[1.6] text-(--color-text-muted)"
              >
                {fullText}
              </div>
            )}
          </AnimatePresence>
        </div>
      )}
    </article>
  )
}

// 区块标题；关键约束是用统一计数徽标表达列表规模。
function SectionTitle({ title, count }: { title: string; count: number }) {
  return (
    <h3 className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-(--color-text-faint)">
      <span>{title}</span>
      <span className="rounded-(--radius-control) bg-(--color-surface-2) px-1.5 py-0.5 font-mono text-[10px] text-(--color-text-muted)">
        {count}
      </span>
    </h3>
  )
}
