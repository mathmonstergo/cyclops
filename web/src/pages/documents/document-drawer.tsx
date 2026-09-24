import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { useQueryClient } from '@tanstack/react-query'
import {
  Check,
  ChevronDown,
  Download,
  Loader2,
  Play,
  PowerOff,
  Power,
  Trash2,
  Wand2,
  Waypoints,
} from 'lucide-react'
import {
  Drawer,
  DrawerBody,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer'
import { DRAWER_WIDTH_MEDIUM } from '@/components/ui/drawer-constants'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { StatusDot, type DotTone } from '@/components/ui/status-dot'
import { Skeleton } from '@/components/ui/skeleton'
import {
  useDeleteImportFile,
  useEmbedImportFile,
  useFilePendingTasks,
  useGenerateImportFileQuestions,
  useImportFileChunks,
  useImportFileParseStatus,
  useStartImportParseJob,
  useToggleImportFileDisabled,
  type ParseStatusResponse,
} from '@/api/hooks'
import { toast } from '@/components/ui/toast'
import { embeddingStatusLabel, parseStateLabel, tr } from '@/lib/labels'
import { dur, ease } from '@/lib/motion'
import { ChunkBrowser } from './chunk-browser'
import {
  DOCUMENT_CHUNKER_OPTIONS,
  type DocumentChunkerType,
  documentChunkerLabel,
  requireDocumentChunkerType,
} from './chunker-options'
import { CopyIdButton } from './copy-id-button'
import { HoverTooltipTrigger } from './hover-tooltip'

interface Props {
  fileId: string | null
  onClose: () => void
}

export function DocumentDrawer({ fileId, onClose }: Props) {
  const open = !!fileId
  const activeFileId = fileId
  return (
    <AnimatePresence>
      {open && activeFileId && (
        <Drawer key={activeFileId} open={open} onOpenChange={(o) => !o && onClose()}>
          <DrawerContent width={DRAWER_WIDTH_MEDIUM}>
            <DrawerInner fileId={activeFileId} onClose={onClose} />
          </DrawerContent>
        </Drawer>
      )}
    </AnimatePresence>
  )
}

function DrawerInner({ fileId, onClose }: { fileId: string; onClose: () => void }) {
  const qc = useQueryClient()
  // 单文件数据源：parse-status 接口返回完整 file 记录 + 进度，且能精确轮询
  const statusQ = useImportFileParseStatus(fileId, {
    refetchInterval: (q) =>
      q.state.data?.file?.status === 'processing' ? 1500 : false,
  })
  const status = statusQ.data
  const file = status?.file
  const [selectedChunkerOverride, setSelectedChunkerOverride] = useState<DocumentChunkerType | null>(null)
  const [chunkerOpen, setChunkerOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  const isParsing = file?.status === 'processing'
  const chunksQ = useImportFileChunks(fileId, {
    refetchInterval: isParsing ? 3000 : undefined,
  })

  // 跨抽屉持久化的"任务在跑"探测：即使抽屉关闭再打开，只要 mutation 还在跑就显示 spinner
  const pending = useFilePendingTasks(fileId)

  // Drawer 只负责终态文档缓存和 toast；KG 刷新由不会受组件卸载影响的 parse queryFn 负责。
  const prevStatusRef = useRef<string | undefined>(file?.status)
  useEffect(() => {
    const prev = prevStatusRef.current
    const cur = file?.status
    if (prev === 'processing' && cur && cur !== 'processing') {
      qc.invalidateQueries({ queryKey: ['import-chunks', fileId] })
      qc.invalidateQueries({ queryKey: ['import-files'] })
      if (cur === 'failed') {
        toast.error(`解析失败：${file?.error || '未知错误'}`)
      } else if (cur === 'needs_review' || cur === 'completed') {
        toast.success(`「${file?.original_name || ''}」解析完成`)
      }
    }
    prevStatusRef.current = cur
  }, [file?.status, file?.error, file?.original_name, fileId, qc])

  const parseJob = useStartImportParseJob()
  const embed = useEmbedImportFile()
  const generate = useGenerateImportFileQuestions()
  const toggleDisabled = useToggleImportFileDisabled()
  const del = useDeleteImportFile()

  const isParsed = !!file && ['needs_review', 'completed'].includes(file.status)
  // 该文档下有多少切片「非绿」：未禁用且未索引/过期/失败/部分，即点 Embedding 会实际处理的数量。
  // 用于在 Embedding 按钮上显示数字徽章，并在全绿时禁用按钮。
  const nonGreenCount = (chunksQ.data?.items || []).filter(
    (c) => !c.is_disabled && c.embedding_status !== 'ready',
  ).length

  const fireMessages = (messages?: string[]) => (messages || []).forEach((m) => toast(m))

  const onParse = async () => {
    if (!file) return
    try {
      const chunkerType = selectedChunkerOverride ?? requireDocumentChunkerType(file.chunker_type)
      await parseJob.mutateAsync({ id: fileId, chunker_type: chunkerType })
      toast('已开始解析')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }
  const onEmbed = async () => {
    try {
      const r = await embed.mutateAsync(fileId)
      toast.success(`已生成 ${r.count || 0} 个切片 embedding`)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }
  const onGenerate = async () => {
    try {
      const r = await generate.mutateAsync({ id: fileId })
      fireMessages(r.messages)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }
  const onToggleDisabled = () => {
    if (!file) return
    toggleDisabled.mutate({ id: fileId, is_disabled: !file.is_disabled })
  }
  const onDelete = async () => {
    try {
      const r = await del.mutateAsync(fileId)
      fireMessages(r.messages)
      setDeleteOpen(false)
      onClose()
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  if (!file) return <DrawerInnerSkeleton />
  const selectedChunker = selectedChunkerOverride ?? requireDocumentChunkerType(file.chunker_type)
  return (
    <>
      <DrawerHeader>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1.5 pr-8">
            <DrawerTitle className="min-w-0 max-w-[min(34rem,calc(100%-5rem))] truncate">
              {file.original_name}
            </DrawerTitle>
            <CopyIdButton label="文件ID" value={fileId} className="mt-0.5" />
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[12px] text-(--color-text-muted)">
            <Badge tone="muted">{file.file_type}</Badge>
            <Badge tone="muted">{file.parser}</Badge>
            <Badge tone="primary">{documentChunkerLabel(file.chunker_type)}</Badge>
            <StatusDot
              tone={fileEmbedDotTone(file.embedding_summary, file.is_disabled)}
              label={file.is_disabled ? '已禁用' : tr(embeddingStatusLabel, file.embedding_summary?.status, '未索引')}
            />
          </div>
        </div>
      </DrawerHeader>

      {/* 操作按钮组 */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-(--color-border) px-6 py-3">
        <Popover open={chunkerOpen} onOpenChange={setChunkerOpen}>
          <PopoverTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              className="cursor-pointer"
              disabled={pending.parse || isParsing}
              aria-label="选择解析后的切块策略"
            >
              <span className="text-(--color-text-faint)">Chunker</span>
              {documentChunkerLabel(selectedChunker)}
              <ChevronDown className="size-3.5 text-(--color-text-faint)" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-44 p-1.5">
            <div className="px-1.5 pb-1.5 text-[11px] text-(--color-text-faint)">切块策略</div>
            <div className="flex flex-col">
              {DOCUMENT_CHUNKER_OPTIONS.map((option) => {
                const checked = selectedChunker === option.value
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => {
                      setSelectedChunkerOverride(option.value)
                      setChunkerOpen(false)
                    }}
                    className="flex cursor-pointer items-center gap-2 rounded-(--radius-control) px-2 py-1.5 text-[12px] text-(--color-text) hover:bg-(--color-surface-2)"
                  >
                    <span className="flex size-3.5 shrink-0 items-center justify-center">
                      {checked && <Check className="size-3.5 text-(--color-primary-hi)" />}
                    </span>
                    <span>{option.label}</span>
                  </button>
                )
              })}
            </div>
          </PopoverContent>
        </Popover>
        <Button onClick={onParse} disabled={pending.parse || isParsing}>
          {isParsing || pending.parse ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Play className="size-3.5" />
          )}
          {isParsing ? '解析中…' : pending.parse ? '提交中…' : '开始解析'}
        </Button>
        <HoverTooltipTrigger
          content={
            nonGreenCount > 0
              ? `为 ${nonGreenCount} 个非绿切片生成向量（已索引的自动跳过）`
              : '所有切片均已索引，无需重新生成'
          }
          disabled={!isParsed || pending.embed || (!!chunksQ.data && nonGreenCount === 0)}
        >
          <Button
            variant="primary"
            className="relative cursor-pointer"
            onClick={onEmbed}
            disabled={!isParsed || pending.embed || (!!chunksQ.data && nonGreenCount === 0)}
          >
            {pending.embed ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Waypoints className="size-3.5" />
            )}
            Embedding
            {nonGreenCount > 0 && !pending.embed && (
              <span className="absolute -right-1 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-(--radius-control) bg-(--color-warning)/20 px-1 font-mono text-[10px] text-(--color-warning)">
                {nonGreenCount}
              </span>
            )}
          </Button>
        </HoverTooltipTrigger>
        <HoverTooltipTrigger content="生成假设问题" disabled={!isParsed || pending.questions}>
          <Button
            size="icon"
            className="cursor-pointer"
            onClick={onGenerate}
            disabled={!isParsed || pending.questions}
            aria-label="生成假设问题"
          >
            {pending.questions ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Wand2 className="size-3.5" />
            )}
          </Button>
        </HoverTooltipTrigger>
        <div className="ml-auto" />
        <HoverTooltipTrigger content="下载原文件">
          <Button asChild variant="ghost" size="icon" aria-label="下载原文件">
            <a
              href={`/api/import/files/${encodeURIComponent(fileId)}/download`}
              target="_blank"
              rel="noreferrer"
              className="cursor-pointer"
            >
              <Download className="size-3.5" />
            </a>
          </Button>
        </HoverTooltipTrigger>
        <HoverTooltipTrigger content={file.is_disabled ? '启用文档' : '禁用文档'}>
          <Button
            variant="ghost"
            size="icon"
            className="cursor-pointer"
            onClick={onToggleDisabled}
            aria-label={file.is_disabled ? '启用文档' : '禁用文档'}
          >
            {file.is_disabled ? <Power className="size-3.5" /> : <PowerOff className="size-3.5" />}
          </Button>
        </HoverTooltipTrigger>
        <HoverTooltipTrigger content="删除文档">
          <Button
            variant="danger"
            size="icon"
            className="cursor-pointer"
            onClick={() => setDeleteOpen(true)}
            aria-label="删除文档"
          >
            <Trash2 className="size-3.5" />
          </Button>
        </HoverTooltipTrigger>
      </div>

      {/* 任务区：解析进度条 + 其他后台任务 */}
      <AnimatePresence initial={false}>
        {(isParsing || pending.embed || pending.questions) && status && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: dur.base, ease: ease.out }}
            className="shrink-0 overflow-hidden border-b border-(--color-border)"
          >
            <TaskPanel
              parseStatus={isParsing ? status : null}
              embedPending={pending.embed}
              questionsPending={pending.questions}
            />
          </motion.div>
        )}
      </AnimatePresence>

      <DrawerBody className="!p-0">
        {chunksQ.isPending ? (
          <div className="space-y-2 p-6">
            <Skeleton className="h-6 w-1/3" />
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        ) : (
          <ChunkBrowser fileId={fileId} chunks={chunksQ.data?.items || []} fileDisabled={file.is_disabled} />
        )}
      </DrawerBody>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>删除文档</DialogTitle>
            <DialogDescription>
              确认删除「{file.original_name || fileId}」？删除后相关切片和向量也会移除。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteOpen(false)}>
              取消
            </Button>
            <Button variant="danger" onClick={() => void onDelete()} disabled={del.isPending}>
              {del.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function TaskPanel({
  parseStatus,
  embedPending,
  questionsPending,
}: {
  parseStatus: ParseStatusResponse | null
  embedPending: boolean
  questionsPending: boolean
}) {
  return (
    <div className="bg-(--color-surface-2) px-6 py-3 flex flex-col gap-2.5">
      {parseStatus && <ParseProgressRow status={parseStatus} />}
      {embedPending && (
        <TaskRow label="正在生成 embedding" hint="对所有切片向量化，可关闭抽屉，过会儿回来查看" />
      )}
      {questionsPending && (
        <TaskRow label="正在生成假设问题" hint="对每个切片 LLM 生成 3-5 条问题，需要几十秒到几分钟" />
      )}
    </div>
  )
}

function ParseProgressRow({ status }: { status: ParseStatusResponse }) {
  const percent = Math.max(0, Math.min(100, status.percent || 0))
  const stage =
    (status.progress?.stage as string | undefined) ||
    (status.progress?.message as string | undefined) ||
    tr(parseStateLabel, status.state, status.state)
  const current = status.progress?.current as number | undefined
  const total = status.progress?.total as number | undefined
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between text-[12px] text-(--color-text-muted)">
        <div className="flex items-center gap-2">
          <Loader2 className="size-3.5 animate-spin text-(--color-primary-hi)" />
          <span className="text-(--color-text)">解析中</span>
          <span className="text-(--color-text-faint)">· {stage}</span>
          {current !== undefined && total !== undefined && (
            <span className="font-mono text-[11px] text-(--color-text-faint)">
              {current}/{total}
            </span>
          )}
        </div>
        <span className="font-mono text-[11px] text-(--color-text-faint)">{percent}%</span>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-(--color-surface-3)">
        <motion.div
          className="h-full bg-(--color-primary)"
          initial={false}
          animate={{ width: `${percent}%` }}
          transition={{ duration: dur.base, ease: ease.out }}
        />
      </div>
    </div>
  )
}

function TaskRow({ label, hint }: { label: string; hint: string }) {
  return (
    <div className="flex items-center gap-2 text-[12px] text-(--color-text-muted)">
      <Loader2 className="size-3.5 animate-spin text-(--color-primary-hi)" />
      <span className="text-(--color-text)">{label}</span>
      <span className="text-(--color-text-faint)">· {hint}</span>
    </div>
  )
}

function DrawerInnerSkeleton() {
  return (
    <div className="space-y-3 p-6">
      {/* 加载态也要有 DrawerTitle，否则 Radix 在 file 拉取完成前会报"DialogContent 缺 DialogTitle" */}
      <DrawerTitle className="sr-only">文档详情</DrawerTitle>
      <Skeleton className="h-6 w-1/3" />
      <Skeleton className="h-4 w-1/2" />
      <div className="space-y-2 pt-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    </div>
  )
}

// 文件层圆点三态：文件被禁用 → 灰，覆盖一切；整份已嵌入且无过期/失败（summary.status==='ready'）→ 绿；其余（未生成/部分/过期/失败）→ 黄。
function fileEmbedDotTone(summary: { status?: string } | undefined, disabled: boolean): DotTone {
  if (disabled) return 'muted'
  if (summary?.status === 'ready') return 'ready'
  return 'warning'
}
