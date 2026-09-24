import { useMemo, useState, type ReactNode } from 'react'
import { AnimatePresence } from 'framer-motion'
import {
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  FileSearch,
  GitBranch,
  Loader2,
  Network,
  RefreshCw,
  Search,
  X,
} from 'lucide-react'
import {
  useConfirmKgEntity,
  useConfirmKgRelation,
  useKgEntities,
  useKgRelations,
  useKgSubgraph,
  useSetKgEntityStatus,
  useSetKgRelationStatus,
} from '@/api/hooks'
import type { KgEntity, KgEvidence, KgRelation } from '@/api/schemas'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Drawer,
  DrawerBody,
  DrawerContent,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer'
import { DRAWER_WIDTH_COMPACT } from '@/components/ui/drawer-constants'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/cn'
import { useUi } from '@/store/ui'
import { CopyIdButton } from './documents/copy-id-button'
import { DocumentDrawer } from './documents/document-drawer'
import { FaqDrawer } from './faqs/faq-drawer'
import {
  clampKgPage,
  confidencePercent,
  entityMatchesQuery,
  evidenceSourceTarget,
  evidenceSummary,
  kgCandidateConfirmBlockedReason,
  kgRelationConfirmBlockedReason,
  kgReviewStatusLabel,
  kgStatusTone,
  relationMatchesQuery,
  relationTitle,
} from './kg/helpers'

type KgTab = 'entities' | 'relations'
type SelectedKgItem =
  | { kind: 'entity'; item: KgEntity }
  | { kind: 'relation'; item: KgRelation }
  | null

const PAGE_SIZE = 30

const STATUS_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'needs_review', label: '待审核' },
  { value: 'usable', label: '已确认' },
  { value: 'disabled', label: '已停用' },
]

// 知识图谱审核工作台：沿用项目已有“工具栏 + 单列表 + 右抽屉”结构，避免三栏常驻布局。
export default function KnowledgeGraphPage() {
  const [tab, setTab] = useState<KgTab>('entities')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('needs_review')
  const [typeFilter, setTypeFilter] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<SelectedKgItem>(null)
  const { openFaqId, setOpenFaqId, openImportFileId, setOpenImportFileId } = useUi()
  const offset = (page - 1) * PAGE_SIZE

  const entityQuery = useKgEntities({
    status,
    entity_type: tab === 'entities' ? typeFilter : '',
    limit: PAGE_SIZE,
    offset,
  })
  const relationQuery = useKgRelations({
    status,
    relation_type: tab === 'relations' ? typeFilter : '',
    limit: PAGE_SIZE,
    offset,
  })

  const entityItems = useMemo(
    () => (entityQuery.data?.items ?? []).filter((item) => entityMatchesQuery(item, query)),
    [entityQuery.data?.items, query],
  )
  const relationItems = useMemo(
    () => (relationQuery.data?.items ?? []).filter((item) => relationMatchesQuery(item, query)),
    [relationQuery.data?.items, query],
  )

  const activeData = tab === 'entities' ? entityQuery.data : relationQuery.data
  const total = activeData?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const isPending = tab === 'entities' ? entityQuery.isPending : relationQuery.isPending
  const isError = tab === 'entities' ? entityQuery.isError : relationQuery.isError
  const isFetching = tab === 'entities' ? entityQuery.isFetching : relationQuery.isFetching
  const refetch = tab === 'entities' ? entityQuery.refetch : relationQuery.refetch

  // 仅在当前查询已有真实响应时校正页码；pending 时不能把用户刚选择的新页误归零。
  const clampedPage = clampKgPage(page, total, PAGE_SIZE)
  if (activeData && clampedPage !== page) {
    setPage(clampedPage)
    setSelected(null)
  }

  // 筛选变更时回到第一页；关键约束是保持当前 tab，不重置用户正在看的实体/关系类型。
  const resetPage = () => {
    setPage(1)
    setSelected(null)
  }

  // 翻页同时关闭旧详情，避免上一页记录在新查询期间保持可操作。
  const changePage = (nextPage: number) => {
    setPage(nextPage)
    setSelected(null)
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-(--color-border) px-6 py-3">
        <div className="relative max-w-md flex-1">
          <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-(--color-text-faint)" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索名称、别名、实体类型或关系..."
            className="pl-7"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery('')}
              className="absolute right-1 top-1/2 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded text-(--color-text-faint) hover:text-(--color-text)"
              aria-label="清空搜索"
            >
              <X className="size-3" />
            </button>
          )}
        </div>
        <SegmentedFilter
          value={tab}
          onChange={(value) => {
            setTab(value as KgTab)
            setTypeFilter('')
            resetPage()
          }}
          options={[
            { value: 'entities', label: '实体' },
            { value: 'relations', label: '关系' },
          ]}
        />
        <SegmentedFilter
          value={status}
          onChange={(value) => {
            setStatus(value)
            resetPage()
          }}
          options={STATUS_OPTIONS}
        />
        <Input
          value={typeFilter}
          onChange={(e) => {
            setTypeFilter(e.target.value)
            resetPage()
          }}
          placeholder={tab === 'entities' ? '实体类型' : '关系类型'}
          className="w-36"
        />
        <Button variant="ghost" size="icon" onClick={() => refetch()} disabled={isFetching} title="刷新列表">
          {isFetching ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto scroll-thin px-6 py-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-[13px] text-(--color-text-muted)">
            <Network className="size-4 text-(--color-primary-hi)" />
            <span>
              共 <span className="font-mono text-(--color-text)">{total}</span>{' '}
              {tab === 'entities' ? '个实体' : '条关系'}
            </span>
            {query && (
              <span className="text-(--color-text-faint)">
                当前页匹配 {tab === 'entities' ? entityItems.length : relationItems.length} 条
              </span>
            )}
          </div>
          <span className="text-[11px] text-(--color-text-faint)">
            已确认内容才会投影为可检索 KG chunk
          </span>
        </div>

        {tab === 'entities' ? (
          <EntityTable
            items={entityItems}
            activeId={selected?.kind === 'entity' ? selected.item.id : null}
            isPending={isPending}
            isError={isError}
            onRetry={() => refetch()}
            onSelect={(item) => setSelected({ kind: 'entity', item })}
          />
        ) : (
          <RelationTable
            items={relationItems}
            activeId={selected?.kind === 'relation' ? selected.item.id : null}
            isPending={isPending}
            isError={isError}
            onRetry={() => refetch()}
            onSelect={(item) => setSelected({ kind: 'relation', item })}
          />
        )}

        {total > PAGE_SIZE && (
          <Pagination
            page={page}
            totalPages={totalPages}
            total={total}
            pageSize={PAGE_SIZE}
            loading={isFetching}
            onChange={changePage}
          />
        )}
      </div>

      <KgDetailDrawer selected={selected} onClose={() => setSelected(null)} />
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

// 实体表格：用单列表承载审核候选，点击行才打开右侧抽屉。
function EntityTable({
  items,
  activeId,
  isPending,
  isError,
  onRetry,
  onSelect,
}: {
  items: KgEntity[]
  activeId: string | null
  isPending: boolean
  isError: boolean
  onRetry: () => void
  onSelect: (item: KgEntity) => void
}) {
  if (isPending) return <TableSkeleton />
  if (isError) return <EmptyState action={<Button size="sm" onClick={onRetry}>重试</Button>}>加载实体失败</EmptyState>
  if (!items.length) return <EmptyState>当前筛选下没有实体候选。</EmptyState>

  return (
    <div className="overflow-hidden rounded-(--radius-card) border border-(--color-border) bg-(--color-surface)">
      <div className="grid grid-cols-[1.4fr_0.8fr_1.2fr_0.7fr_0.7fr_0.7fr_0.8fr] gap-3 border-b border-(--color-border) px-4 py-3 text-[11px] text-(--color-text-faint)">
        <span>名称</span>
        <span>类型</span>
        <span>别名</span>
        <span>状态</span>
        <span>置信度</span>
        <span>有效来源</span>
        <span>更新时间</span>
      </div>
      <ul className="divide-y divide-(--color-border)">
        {items.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => onSelect(item)}
              className={cn(
                'grid w-full grid-cols-[1.4fr_0.8fr_1.2fr_0.7fr_0.7fr_0.7fr_0.8fr] gap-3 px-4 py-3 text-left text-[12px] transition-colors',
                activeId === item.id
                  ? 'bg-(--color-primary-soft) ring-1 ring-inset ring-(--color-primary)/35'
                  : 'hover:bg-(--color-surface-2)',
              )}
            >
              <span className="min-w-0">
                <span className="block truncate text-[13px] font-[540] text-(--color-text)">{item.name}</span>
                <span className="mt-0.5 block truncate font-mono text-[10px] text-(--color-text-faint)">
                  {item.id}
                </span>
              </span>
              <span className="truncate text-(--color-text-muted)">{item.entity_type}</span>
              <span className="truncate text-(--color-text-muted)">{item.aliases?.join('，') || '无'}</span>
              <span>
                <Badge tone={kgStatusTone(item.status)}>{kgReviewStatusLabel(item.status)}</Badge>
              </span>
              <span className="font-mono text-(--color-text)">{confidencePercent(item.confidence)}</span>
              <span className="font-mono text-(--color-text-muted)">{item.source_count}</span>
              <span className="font-mono text-(--color-text-faint)">{formatTime(item.updated_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

// 关系列表：以事实三元组为主列，保留状态、证据和追溯入口。
function RelationTable({
  items,
  activeId,
  isPending,
  isError,
  onRetry,
  onSelect,
}: {
  items: KgRelation[]
  activeId: string | null
  isPending: boolean
  isError: boolean
  onRetry: () => void
  onSelect: (item: KgRelation) => void
}) {
  if (isPending) return <TableSkeleton />
  if (isError) return <EmptyState action={<Button size="sm" onClick={onRetry}>重试</Button>}>加载关系失败</EmptyState>
  if (!items.length) return <EmptyState>当前筛选下没有关系候选。</EmptyState>

  return (
    <div className="overflow-hidden rounded-(--radius-card) border border-(--color-border) bg-(--color-surface)">
      <div className="grid grid-cols-[1.2fr_0.75fr_1.2fr_0.7fr_0.7fr_0.8fr] gap-3 border-b border-(--color-border) px-4 py-3 text-[11px] text-(--color-text-faint)">
        <span>头实体</span>
        <span>关系</span>
        <span>尾实体</span>
        <span>状态</span>
        <span>有效证据</span>
        <span>更新时间</span>
      </div>
      <ul className="divide-y divide-(--color-border)">
        {items.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => onSelect(item)}
              className={cn(
                'grid w-full grid-cols-[1.2fr_0.75fr_1.2fr_0.7fr_0.7fr_0.8fr] gap-3 px-4 py-3 text-left text-[12px] transition-colors',
                activeId === item.id
                  ? 'bg-(--color-primary-soft) ring-1 ring-inset ring-(--color-primary)/35'
                  : 'hover:bg-(--color-surface-2)',
              )}
            >
              <EntityCell name={item.head_entity_name} type={item.head_entity_type} />
              <span className="truncate text-(--color-primary-hi)">{item.relation_type}</span>
              <EntityCell name={item.tail_entity_name} type={item.tail_entity_type} />
              <span>
                <Badge tone={kgStatusTone(item.status)}>{kgReviewStatusLabel(item.status)}</Badge>
              </span>
              <span className="font-mono text-(--color-text-muted)">{item.evidence_count}</span>
              <span className="font-mono text-(--color-text-faint)">{formatTime(item.updated_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

// 关系表内的实体单元格，名称和类型分层展示以便快速扫描。
function EntityCell({ name, type }: { name: string; type: string }) {
  return (
    <span className="min-w-0">
      <span className="block truncate text-[13px] font-[540] text-(--color-text)">{name}</span>
      <span className="mt-0.5 block truncate text-[11px] text-(--color-text-faint)">{type}</span>
    </span>
  )
}

// KG 详情抽屉：承载实体/关系详情、证据和审核动作，关闭后主列表恢复完整空间。
function KgDetailDrawer({ selected, onClose }: { selected: SelectedKgItem; onClose: () => void }) {
  const open = !!selected
  return (
    <AnimatePresence>
      {open && selected && (
        <Drawer open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
          <DrawerContent width={DRAWER_WIDTH_COMPACT}>
            {selected.kind === 'entity' ? (
              <EntityDrawer item={selected.item} onClose={onClose} />
            ) : (
              <RelationDrawer item={selected.item} onClose={onClose} />
            )}
          </DrawerContent>
        </Drawer>
      )}
    </AnimatePresence>
  )
}

// 实体详情抽屉：展示别名、描述、证据和已确认后的局部关系。
function EntityDrawer({ item, onClose }: { item: KgEntity; onClose: () => void }) {
  const confirm = useConfirmKgEntity()
  const setStatus = useSetKgEntityStatus()
  const subgraph = useKgSubgraph(item.id, { enabled: item.status === 'usable' })

  const handleConfirm = async () => {
    try {
      await confirm.mutateAsync({
        id: item.id,
        expectedRevision: item.review_revision,
      })
      toast.success('实体已确认并进入 KG 检索投影')
      onClose()
    } catch (error) {
      toast.error((error as Error).message)
    }
  }

  const handleStatus = async (status: string) => {
    try {
      await setStatus.mutateAsync({ id: item.id, status })
      toast.success(`实体已标记为${kgReviewStatusLabel(status)}`)
      onClose()
    } catch (error) {
      toast.error((error as Error).message)
    }
  }

  const pending = confirm.isPending || setStatus.isPending

  return (
    <>
      <DrawerHeader>
        <div className="min-w-0 flex-1">
          <DrawerTitle className="pr-8">{item.name}</DrawerTitle>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-(--color-text-muted)">
            <Badge tone={kgStatusTone(item.status)}>{kgReviewStatusLabel(item.status)}</Badge>
            <span>{item.entity_type}</span>
            <span className="font-mono text-(--color-text-faint)">{item.id}</span>
          </div>
        </div>
      </DrawerHeader>

      <DrawerBody className="space-y-5">
        <DetailSection title="实体详情">
          <DetailRow label="置信度" value={confidencePercent(item.confidence)} />
          <DetailRow label="别名" value={item.aliases?.length ? item.aliases.join('，') : '无'} />
          <DetailRow label="描述" value={item.description || '未提供描述'} multiline />
          <DetailRow label="创建时间" value={formatTime(item.created_at)} />
          <DetailRow label="更新时间" value={formatTime(item.updated_at)} />
        </DetailSection>

        <EvidenceList evidence={item.evidence || []} />

        <DetailSection title="局部关系">
          {item.status !== 'usable' ? (
            <p className="text-[12px] text-(--color-text-muted)">确认实体后可查看已确认局部关系。</p>
          ) : subgraph.isPending ? (
            <Skeleton className="h-20 w-full" />
          ) : subgraph.isError ? (
            <div className="flex items-center justify-between gap-3 text-[12px] text-(--color-danger)">
              <span>加载局部关系失败。</span>
              <Button size="sm" variant="outline" onClick={() => void subgraph.refetch()}>
                重试
              </Button>
            </div>
          ) : subgraph.data?.state === 'connected' ? (
            <div className="space-y-2">
              {subgraph.data.edges.slice(0, 8).map((edge) => {
                const source = subgraph.data?.nodes.find((node) => node.id === edge.source)
                const target = subgraph.data?.nodes.find((node) => node.id === edge.target)
                return (
                  <div key={edge.id} className="rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-2) px-3 py-2">
                    <div className="flex items-center gap-2 text-[12px]">
                      <GitBranch className="size-3.5 text-(--color-primary-hi)" />
                      <span className="truncate text-(--color-text)">{source?.name || edge.source}</span>
                      <span className="text-(--color-primary-hi)">{edge.relation_type}</span>
                      <span className="truncate text-(--color-text)">{target?.name || edge.target}</span>
                    </div>
                    {edge.description && (
                      <p className="mt-1 line-clamp-2 text-[11px] text-(--color-text-muted)">{edge.description}</p>
                    )}
                  </div>
                )
              })}
            </div>
          ) : subgraph.data?.state === 'isolated' ? (
            <p className="text-[12px] text-(--color-text-muted)">暂无已确认邻接关系。</p>
          ) : (
            <Skeleton className="h-20 w-full" />
          )}
        </DetailSection>
      </DrawerBody>

      <DrawerFooter>
        <ReviewActions
          status={item.status}
          pending={pending}
          confirmBlockedReason={kgCandidateConfirmBlockedReason(item)}
          onConfirm={handleConfirm}
          onNeedsReview={() => handleStatus('needs_review')}
          onDisable={() => handleStatus('disabled')}
        />
      </DrawerFooter>
    </>
  )
}

// 关系详情抽屉：展示三元组、关系解释、证据和审核动作。
function RelationDrawer({ item, onClose }: { item: KgRelation; onClose: () => void }) {
  const confirm = useConfirmKgRelation()
  const setStatus = useSetKgRelationStatus()

  const handleConfirm = async () => {
    try {
      await confirm.mutateAsync({
        id: item.id,
        expectedRevision: item.review_revision,
      })
      toast.success('关系已确认并进入 KG 检索投影')
      onClose()
    } catch (error) {
      toast.error((error as Error).message)
    }
  }

  const handleStatus = async (status: string) => {
    try {
      await setStatus.mutateAsync({ id: item.id, status })
      toast.success(`关系已标记为${kgReviewStatusLabel(status)}`)
      onClose()
    } catch (error) {
      toast.error((error as Error).message)
    }
  }

  const pending = confirm.isPending || setStatus.isPending

  return (
    <>
      <DrawerHeader>
        <div className="min-w-0 flex-1">
          <DrawerTitle className="pr-8">{relationTitle(item)}</DrawerTitle>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-(--color-text-muted)">
            <Badge tone={kgStatusTone(item.status)}>{kgReviewStatusLabel(item.status)}</Badge>
            <span className="font-mono text-(--color-text-faint)">{item.id}</span>
          </div>
        </div>
      </DrawerHeader>

      <DrawerBody className="space-y-5">
        <DetailSection title="关系详情">
          <DetailRow label="头实体" value={`${item.head_entity_name}（${item.head_entity_type}）`} />
          <DetailRow label="关系类型" value={item.relation_type} />
          <DetailRow label="尾实体" value={`${item.tail_entity_name}（${item.tail_entity_type}）`} />
          <DetailRow label="置信度" value={confidencePercent(item.confidence)} />
          <DetailRow label="描述" value={item.description || '未提供描述'} multiline />
          <DetailRow label="更新时间" value={formatTime(item.updated_at)} />
        </DetailSection>

        <EvidenceList evidence={item.evidence || []} />
      </DrawerBody>

      <DrawerFooter>
        <ReviewActions
          status={item.status}
          pending={pending}
          confirmBlockedReason={kgRelationConfirmBlockedReason(item)}
          onConfirm={handleConfirm}
          onNeedsReview={() => handleStatus('needs_review')}
          onDisable={() => handleStatus('disabled')}
        />
      </DrawerFooter>
    </>
  )
}

// 审核动作区：确认、退回待审核、停用三种动作保持实体和关系一致。
function ReviewActions({
  status,
  pending,
  confirmBlockedReason,
  onConfirm,
  onNeedsReview,
  onDisable,
}: {
  status: string
  pending: boolean
  confirmBlockedReason: string | null
  onConfirm: () => void
  onNeedsReview: () => void
  onDisable: () => void
}) {
  const canConfirm = confirmBlockedReason === null
  return (
    <div className="flex w-full items-center justify-end gap-2">
      {status !== 'usable' && (
        <Button
          variant="primary"
          onClick={onConfirm}
          disabled={pending || !canConfirm}
          title={canConfirm ? '确认并投影为可检索 KG fact' : confirmBlockedReason}
        >
          {pending ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5" />}
          确认
        </Button>
      )}
      {status !== 'needs_review' && (
        <Button variant="outline" onClick={onNeedsReview} disabled={pending}>
          <FileSearch className="size-3.5" />
          待审核
        </Button>
      )}
      {status !== 'disabled' && (
        <Button variant="danger" onClick={onDisable} disabled={pending}>
          <Ban className="size-3.5" />
          停用
        </Button>
      )}
    </div>
  )
}

// 证据历史：保留全部可追溯记录；失效来源只降级标识，不从历史数组中删除。
function EvidenceList({ evidence }: { evidence: KgEvidence[] }) {
  const { setOpenFaqId, setOpenImportFileId } = useUi()

  // 证据只按后端精确 locator 打开已有来源抽屉，不从 ID 前缀或 metadata 推断。
  const openEvidence = (item: KgEvidence) => {
    const target = evidenceSourceTarget(item)
    if (target?.kind === 'faq') {
      setOpenFaqId(target.sourceId)
      return
    }
    if (target?.kind === 'document') {
      setOpenImportFileId(target.sourceId, target.sourceChunkId)
    }
  }

  return (
    <DetailSection title={`证据历史 (${evidence.length})`}>
      {evidence.length ? (
        <div className="space-y-2">
          {evidence.map((item) => (
            <div key={item.id} className="rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-2) px-3 py-2">
              <div className="mb-1 flex items-center gap-1 text-[11px] text-(--color-text-faint)">
                <span className="min-w-0 flex-1 truncate">{evidenceSummary(item)}</span>
                {!item.is_valid && (
                  <span className="shrink-0 text-[10px] text-(--color-text-faint)">来源已失效</span>
                )}
                <CopyIdButton label="来源ID" value={item.source_id} />
                {item.source_chunk_id && (
                  <CopyIdButton label="切片ID" value={item.source_chunk_id} />
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 cursor-pointer px-2 text-[11px]"
                  onClick={() => openEvidence(item)}
                  disabled={!evidenceSourceTarget(item)}
                >
                  {item.source_type === 'faq' ? '查看 FAQ' : '查看切片'}
                </Button>
              </div>
              <p className="text-[12px] leading-[1.6] text-(--color-text-muted)">{item.excerpt}</p>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[12px] text-(--color-danger)">缺少证据，不建议确认。</p>
      )}
    </DetailSection>
  )
}

// 详情区分组容器，保持抽屉内信息密度和层级一致。
function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-[12px] font-[540] text-(--color-text)">{title}</h3>
      <div className="rounded-(--radius-card) border border-(--color-border) bg-(--color-surface) p-3">
        {children}
      </div>
    </section>
  )
}

// 详情字段行；长描述允许换行，其余字段保持左右对齐便于扫描。
function DetailRow({ label, value, multiline = false }: { label: string; value: string; multiline?: boolean }) {
  return (
    <div className={cn('grid grid-cols-[80px_1fr] gap-3 py-1.5 text-[12px]', multiline && 'items-start')}>
      <span className="text-(--color-text-faint)">{label}</span>
      <span className={cn('min-w-0 text-(--color-text-muted)', multiline ? 'whitespace-pre-wrap leading-[1.6]' : 'truncate')}>
        {value}
      </span>
    </div>
  )
}

// 过滤器使用分段按钮，和 FAQ 页面保持一致的工具型交互。
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
    <div className="flex items-center gap-1 rounded-(--radius-control) border border-(--color-border) bg-(--color-surface-2) p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={cn(
            'rounded-(--radius-control) px-2 py-1 text-[12px] transition-colors',
            value === option.value
              ? 'bg-(--color-surface-3) text-(--color-text)'
              : 'text-(--color-text-muted) hover:text-(--color-text)',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

// 分页器沿用 FAQ 页模式，只做上下页和范围展示。
function Pagination({
  page,
  totalPages,
  total,
  pageSize,
  loading,
  onChange,
}: {
  page: number
  totalPages: number
  total: number
  pageSize: number
  loading: boolean
  onChange: (page: number) => void
}) {
  const start = (page - 1) * pageSize + 1
  const end = Math.min(page * pageSize, total)
  return (
    <div className="mt-5 flex items-center justify-between gap-3 text-[12px] text-(--color-text-muted)">
      <span>
        <span className={cn(loading && 'animate-pulse')}>{start}-{end}</span>
        <span className="mx-1 text-(--color-text-faint)">/</span>
        <span>{total}</span>
      </span>
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          disabled={page <= 1 || loading}
          onClick={() => onChange(Math.max(1, page - 1))}
        >
          <ChevronLeft className="size-3.5" />
          上一页
        </Button>
        <span className="px-2 font-mono text-[12px] text-(--color-text-faint)">
          {page} / {totalPages}
        </span>
        <Button
          variant="ghost"
          size="sm"
          disabled={page >= totalPages || loading}
          onClick={() => onChange(Math.min(totalPages, page + 1))}
        >
          下一页
          <ChevronRight className="size-3.5" />
        </Button>
      </div>
    </div>
  )
}

// 表格加载骨架：保持行高稳定，避免数据加载时布局跳动。
function TableSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 8 }).map((_, index) => (
        <Skeleton key={index} className="h-14 w-full" />
      ))}
    </div>
  )
}

// 空态容器：用于错误和无数据状态，保持列表区域视觉居中。
function EmptyState({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="surface rounded-(--radius-card) px-6 py-12 text-center text-[13px] text-(--color-text-muted)">
      <div className="flex items-center justify-center gap-3">
        <span>{children}</span>
        {action}
      </div>
    </div>
  )
}

// 时间格式化只展示业务审核需要的短时间；缺失时给出稳定占位。
function formatTime(value: string | null | undefined): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
