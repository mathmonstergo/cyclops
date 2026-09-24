import { useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Bot,
  Loader2,
  Save,
  Wand2,
  Waypoints,
  X,
} from 'lucide-react'
import {
  Drawer,
  DrawerBody,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer'
import { DRAWER_WIDTH_COMPACT } from '@/components/ui/drawer-constants'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { StatusDot } from '@/components/ui/status-dot'
import { embedDotTone } from '@/components/ui/status-dot-utils'
import { Skeleton } from '@/components/ui/skeleton'
import { TagInput } from '@/components/shared/tag-input'
import { toast } from '@/components/ui/toast'
import {
  useEmbedFaq,
  useCreateKgExtractionJob,
  useFaq,
  useOptimizeFaq,
  useSaveFaq,
} from '@/api/hooks'
import { useUi } from '@/store/ui'
import { cn } from '@/lib/cn'
import { confidenceLabel, embeddingStatusLabel, faqStatusLabel, tr } from '@/lib/labels'
import type { Faq } from '@/api/schemas'
import { formatKgExtractionResult } from '../documents/kg-actions'
import { canExtractFaqKg } from './kg-actions'

interface Props {
  faqId: string | null
  onClose: () => void
  onCreated?: (id: string) => void
}

interface FaqDraft {
  question: string
  answer: string
  question_variants: string[]
  tags: string[]
  category: string
  status: string
  confidence: string
}

const EMPTY_DRAFT: FaqDraft = {
  question: '',
  answer: '',
  question_variants: [],
  tags: [],
  category: '',
  status: 'usable',
  confidence: 'medium',
}

export function FaqDrawer({ faqId, onClose, onCreated }: Props) {
  const open = !!faqId
  return (
    <AnimatePresence>
      {open && (
        <Drawer key={faqId} open={open} onOpenChange={(o) => !o && onClose()}>
          <DrawerContent width={DRAWER_WIDTH_COMPACT}>
            <DrawerInner faqId={faqId} onClose={onClose} onCreated={onCreated} />
          </DrawerContent>
        </Drawer>
      )}
    </AnimatePresence>
  )
}

function DrawerInner({
  faqId,
  onClose,
  onCreated,
}: {
  faqId: string
  onClose: () => void
  onCreated?: (id: string) => void
}) {
  const isNew = faqId === 'new'
  const { data: faq, isPending } = useFaq(isNew ? null : faqId)

  const baseline = useMemo<FaqDraft>(() => faqToDraft(faq, isNew), [faq, isNew])

  if (isPending && !isNew) return <DrawerInnerSkeleton />

  return (
    <FaqEditor
      key={faqId}
      faqId={faqId}
      isNew={isNew}
      faq={faq}
      baseline={baseline}
      onClose={onClose}
      onCreated={onCreated}
    />
  )
}

// FAQ 编辑器在数据就绪后挂载，用 baseline 初始化本地草稿，避免 effect 覆盖编辑中内容。
function FaqEditor({
  faqId,
  isNew,
  faq,
  baseline,
  onClose,
  onCreated,
}: {
  faqId: string
  isNew: boolean
  faq?: Faq
  baseline: FaqDraft
  onClose: () => void
  onCreated?: (id: string) => void
}) {
  const save = useSaveFaq()
  const embed = useEmbedFaq()
  const createKgJob = useCreateKgExtractionJob()
  const optimize = useOptimizeFaq()
  const { setFaqDirty } = useUi()

  const [draft, setDraft] = useState<FaqDraft>(baseline)

  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(baseline), [draft, baseline])
  const needsEmbed = !isNew && faq?.embedding_status !== 'ready'
  const canExtractKg = canExtractFaqKg({ isNew, dirty, status: faq?.status })

  useEffect(() => {
    setFaqDirty(dirty)
    return () => setFaqDirty(false)
  }, [dirty, setFaqDirty])

  const onSave = async () => {
    if (!draft.question.trim()) {
      toast.error('请填写问题')
      return
    }
    if (!draft.answer.trim()) {
      toast.error('请填写答案')
      return
    }
    try {
      const payload: Partial<Faq> = isNew
        ? { ...draft }
        : { id: faqId, ...draft }
      const saved = await save.mutateAsync(payload)
      toast.success(isNew ? '已创建 FAQ' : '已保存修改')
      if (isNew && saved?.id) {
        onCreated?.(saved.id)
      } else {
        // 编辑态保存成功后自动关闭抽屉；新建态留开以便接着生成 embedding。
        onClose()
      }
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const onEmbed = async () => {
    if (isNew) {
      toast.error('请先保存再生成 embedding')
      return
    }
    try {
      await embed.mutateAsync(faqId)
      toast.success('已生成 embedding')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const onOptimize = async () => {
    if (!draft.question.trim() || !draft.answer.trim()) {
      toast.error('优化前需要先填问题和答案')
      return
    }
    try {
      const r = await optimize.mutateAsync({
        question: draft.question,
        answer: draft.answer,
      })
      setDraft((d) => ({
        ...d,
        question: r.question?.trim() || d.question,
        answer: r.answer?.trim() || d.answer,
        question_variants: r.question_variants?.length ? r.question_variants : d.question_variants,
        tags: r.tags?.length ? r.tags : d.tags,
      }))
      toast.success('已应用 AI 优化建议')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  // 从已保存 usable FAQ 发起显式异步 KG 任务；终态失败由 hook 原样抛出。
  const onExtractKg = async () => {
    if (!canExtractKg) {
      toast.error('请先保存修改，并确保 FAQ 状态为可用')
      return
    }
    try {
      const job = await createKgJob.mutateAsync({ source_type: 'faq', source_id: faqId })
      toast.success(formatKgExtractionResult(job))
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  return (
    <>
      <DrawerHeader>
        <div className="min-w-0 flex-1">
          <DrawerTitle className="flex items-center gap-2">
            {isNew ? (
              <span className="text-(--color-text)">新建 FAQ</span>
            ) : (
              <>
                <span className="font-mono text-[12px] text-(--color-text-faint)">
                  {faqId.slice(0, 12)}
                </span>
              </>
            )}
            {dirty && (
              <Badge tone="warning" className="text-[10px]">
                未保存
              </Badge>
            )}
          </DrawerTitle>
          {faq && !isNew && (
            <div className="mt-1.5 flex items-center gap-2 text-[11px] text-(--color-text-muted)">
              <StatusDot
                tone={embedDotTone(faq.embedding_status, faq.status === 'disabled')}
                label={
                  faq.status === 'disabled'
                    ? '已禁用'
                    : tr(embeddingStatusLabel, faq.embedding_status, '未索引')
                }
              />
              <span className="text-(--color-text-faint)">
                · 创建 {formatTime(faq.created_at)}
              </span>
              <span className="text-(--color-text-faint)">
                · 修改 {formatTime(faq.updated_at)}
              </span>
            </div>
          )}
        </div>
        <ShimmerButton onClick={onOptimize} loading={optimize.isPending}>
          AI 优化
        </ShimmerButton>
      </DrawerHeader>

      <DrawerBody className="!p-0">
        <div className="px-6 py-5 pb-24 flex flex-col gap-5">
          <Field label="问题" required>
            <Input
              value={draft.question}
              onChange={(e) => setDraft({ ...draft, question: e.target.value })}
              className="h-10 text-[14px]"
              placeholder="用户实际提问形式"
            />
          </Field>

          <Field label={`相似问法 ${draft.question_variants.length > 0 ? `(${draft.question_variants.length})` : ''}`}>
            <TagInput
              value={draft.question_variants}
              onChange={(v) => setDraft({ ...draft, question_variants: v })}
              placeholder="同义提问，回车 / 逗号添加"
            />
          </Field>

          <Field label="答案" required>
            <Textarea
              value={draft.answer}
              onChange={(e) => setDraft({ ...draft, answer: e.target.value })}
              rows={8}
              className="!font-sans text-[14px] leading-[1.65]"
              placeholder="给客服 / 用户的标准答复"
            />
          </Field>

          <Field label="分类">
            <Input
              value={draft.category}
              onChange={(e) => setDraft({ ...draft, category: e.target.value })}
              placeholder="如：账户 / 物流"
            />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="状态">
              <PillSelect
                value={draft.status}
                options={[
                  { value: 'usable', label: tr(faqStatusLabel, 'usable') },
                  { value: 'needs_review', label: tr(faqStatusLabel, 'needs_review') },
                  { value: 'disabled', label: tr(faqStatusLabel, 'disabled') },
                ]}
                onChange={(v) => setDraft({ ...draft, status: v })}
              />
            </Field>
            <Field label="置信度">
              <PillSelect
                value={draft.confidence}
                options={[
                  { value: 'high', label: tr(confidenceLabel, 'high') },
                  { value: 'medium', label: tr(confidenceLabel, 'medium') },
                  { value: 'low', label: tr(confidenceLabel, 'low') },
                ]}
                onChange={(v) => setDraft({ ...draft, confidence: v })}
              />
            </Field>
          </div>

          <Field label="标签">
            <TagInput
              value={draft.tags}
              onChange={(v) => setDraft({ ...draft, tags: v })}
              placeholder="关键词，便于检索 / 归类"
            />
          </Field>
        </div>
      </DrawerBody>

      {/* sticky 底部操作栏 + 玻璃感 */}
      <div className="shrink-0 border-t border-(--color-border) bg-(--color-surface)/85 backdrop-blur-md px-6 py-3 flex items-center gap-2">
        <span className="text-[11px] text-(--color-text-faint)">
          {dirty ? '有未保存的修改' : isNew ? '新建一条 FAQ' : '所有修改已保存'}
        </span>
        <div className="ml-auto" />
        <Button
          variant="ghost"
          size="icon"
          className="size-7 cursor-pointer"
          onClick={onClose}
          title="关闭"
          aria-label="关闭"
        >
          <X className="size-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="cursor-pointer"
          onClick={onExtractKg}
          disabled={!canExtractKg || createKgJob.isPending}
          title={
            canExtractKg
              ? '从当前 FAQ 抽取 KG 候选'
              : '仅已保存、无未保存修改且状态可用的 FAQ 可抽取'
          }
        >
          {createKgJob.isPending ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Bot className="size-3.5" />
          )}
          KG 抽取
        </Button>
        <Button
          variant={needsEmbed ? 'primary' : 'ghost'}
          size="sm"
          className="cursor-pointer"
          onClick={onEmbed}
          disabled={isNew || embed.isPending}
          title={isNew ? '保存后才能生成 FAQ 向量' : '生成 FAQ 向量'}
        >
          {embed.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <Waypoints className="size-3.5" />}
          Embedding
        </Button>
        <Button variant="primary" onClick={onSave} disabled={save.isPending || !dirty}>
          {save.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
          {isNew ? '创建' : '保存修改'}
        </Button>
      </div>
    </>
  )
}

// 把服务端 FAQ 记录规整成编辑草稿；新建态和数据缺失时使用空草稿。
function faqToDraft(faq: Faq | undefined, isNew: boolean): FaqDraft {
  if (isNew || !faq) return EMPTY_DRAFT
  return {
    question: faq.question || '',
    answer: faq.answer || '',
    question_variants: faq.question_variants || [],
    tags: faq.tags || [],
    category: faq.category || '',
    status: faq.status || 'usable',
    confidence: faq.confidence || 'medium',
  }
}

function Field({
  label,
  required,
  children,
}: {
  label: string
  required?: boolean
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[11px] uppercase tracking-[0.14em] text-(--color-text-faint)">
        {label} {required && <span className="text-(--color-danger)">*</span>}
      </label>
      {children}
    </div>
  )
}

function PillSelect({
  value,
  options,
  onChange,
}: {
  value: string
  options: { value: string; label: string }[]
  onChange: (v: string) => void
}) {
  return (
    <div className="flex flex-wrap gap-1 rounded-(--radius-control) bg-(--color-surface-2) border border-(--color-border) p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={cn(
            'min-w-0 flex-1 whitespace-nowrap rounded-(--radius-control) px-2 py-1 text-[12px] transition-colors',
            value === opt.value
              ? 'bg-(--color-surface-3) text-(--color-text)'
              : 'text-(--color-text-muted) hover:text-(--color-text)',
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function ShimmerButton({
  onClick,
  loading,
  children,
}: {
  onClick: () => void
  loading: boolean
  children: React.ReactNode
}) {
  return (
    <motion.button
      whileTap={{ scale: 0.97 }}
      onClick={onClick}
      disabled={loading}
      className="group relative inline-flex items-center gap-1.5 overflow-hidden rounded-(--radius-control) border border-(--color-primary)/40 bg-(--color-primary-soft) px-3 py-1.5 text-[12px] font-[500] text-(--color-primary-hi) disabled:opacity-60"
    >
      {/* shimmer 扫光 */}
      <span
        aria-hidden
        className="pointer-events-none absolute inset-y-0 -left-1/2 w-1/2 -skew-x-12 bg-gradient-to-r from-transparent via-white/15 to-transparent transition-transform duration-[1200ms] ease-out group-hover:translate-x-[300%]"
      />
      {loading ? (
        <Loader2 className="size-3.5 animate-spin" />
      ) : (
        <Wand2 className="size-3.5" />
      )}
      {loading ? '优化中…' : children}
    </motion.button>
  )
}

function DrawerInnerSkeleton() {
  return (
    <div className="space-y-3 p-6">
      {/* 加载态也要有 DrawerTitle，否则 Radix 在 faq 拉取完成前会报"DialogContent 缺 DialogTitle" */}
      <DrawerTitle className="sr-only">FAQ 详情</DrawerTitle>
      <Skeleton className="h-6 w-1/3" />
      <Skeleton className="h-4 w-1/2" />
      <div className="space-y-2 pt-4">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    </div>
  )
}

function formatTime(ts?: string | null) {
  if (!ts) return ''
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`
  return d.toLocaleDateString('zh-CN')
}
