import { kgReviewStatusLabelMap } from '../../lib/labels.ts'
import type { KgEntity, KgEvidence, KgRelation } from '../../api/schemas.ts'

export type EvidenceSourceTarget =
  | { kind: 'faq'; sourceId: string }
  | { kind: 'document'; sourceId: string; sourceChunkId: string }

// 将 KG 审核状态翻译成客服后台可读文案；未知值保留原样方便排查后端新状态。
export function kgReviewStatusLabel(status: string | null | undefined): string {
  if (!status) return ''
  return kgReviewStatusLabelMap[status] || status
}

// 将模型置信度稳定显示为百分比；缺失时不伪造数值。
export function confidencePercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '未标注'
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`
}

// 审核列表收缩后把页码限制在真实范围内，避免最后一页被审空后停留在空页。
export function clampKgPage(page: number, total: number, pageSize: number): number {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  return Math.min(Math.max(1, page), totalPages)
}

// KG 候选确认资格只读后端实时门禁；历史 evidence 数组只用于追溯，不能参与推断。
export function canConfirmKgCandidate(
  candidate: Pick<KgEntity, 'has_valid_evidence'>,
): boolean {
  return kgCandidateConfirmBlockedReason(candidate) === null
}

// 返回实体确认的精确阻塞原因；null 表示当前候选可以确认。
export function kgCandidateConfirmBlockedReason(
  candidate: Pick<KgEntity, 'has_valid_evidence'>,
): string | null {
  return candidate.has_valid_evidence ? null : '缺少有效证据，不能确认'
}

// 关系确认同时要求有效证据和两个 usable 端点，避免前端发出后端必然拒绝的请求。
export function canConfirmKgRelation(
  relation: Pick<
    KgRelation,
    'has_valid_evidence' | 'head_entity_status' | 'tail_entity_status'
  >,
): boolean {
  return kgRelationConfirmBlockedReason(relation) === null
}

// 返回关系确认的精确阻塞原因；证据门禁优先，其次提示先确认两个端点。
export function kgRelationConfirmBlockedReason(
  relation: Pick<
    KgRelation,
    'has_valid_evidence' | 'head_entity_status' | 'tail_entity_status'
  >,
): string | null {
  const evidenceReason = kgCandidateConfirmBlockedReason(relation)
  if (evidenceReason) return evidenceReason
  if (relation.head_entity_status !== 'usable' || relation.tail_entity_status !== 'usable') {
    return '请先确认头尾实体'
  }
  return null
}

// 将证据 locator 映射为已有来源抽屉；文档必须同时具备文件和切片 ID。
export function evidenceSourceTarget(
  evidence: Pick<KgEvidence, 'source_type' | 'source_id' | 'source_chunk_id'>,
): EvidenceSourceTarget | null {
  const sourceId = evidence.source_id.trim()
  if (evidence.source_type === 'faq' && sourceId && evidence.source_chunk_id === null) {
    return { kind: 'faq', sourceId }
  }
  const sourceChunkId = evidence.source_chunk_id?.trim() || ''
  if (evidence.source_type === 'document' && sourceId && sourceChunkId) {
    return { kind: 'document', sourceId, sourceChunkId }
  }
  return null
}

// 整理证据来源摘要；关键约束是保留文件、章节、页码的追溯信息。
export function evidenceSummary(evidence: Pick<KgEvidence, 'source_title' | 'section_path' | 'page_start' | 'page_end'>): string {
  const parts: string[] = []
  if (evidence.source_title) parts.push(evidence.source_title)
  for (const section of evidence.section_path || []) {
    if (section) parts.push(section)
  }
  const hasStartPage = evidence.page_start !== null && evidence.page_start !== undefined
  const hasEndPage = evidence.page_end !== null && evidence.page_end !== undefined
  if (hasStartPage && hasEndPage && evidence.page_start !== evidence.page_end) {
    parts.push(`第 ${evidence.page_start}-${evidence.page_end} 页`)
  } else if (hasStartPage) {
    parts.push(`第 ${evidence.page_start} 页`)
  }
  return parts.join(' / ') || '未标注来源'
}

// 组合关系标题；关键约束是头实体、关系类型、尾实体缺一不可时仍给出稳定占位。
export function relationTitle(
  relation: Pick<KgRelation, 'head_entity_name' | 'relation_type' | 'tail_entity_name'>,
): string {
  const head = relation.head_entity_name || '未知实体'
  const type = relation.relation_type || '关联'
  const tail = relation.tail_entity_name || '未知实体'
  return `${head} - ${type} - ${tail}`
}

// 根据审核状态给 Badge 选择固定色系，避免页面各处状态颜色漂移。
export function kgStatusTone(status: string): 'success' | 'warning' | 'muted' | 'danger' {
  if (status === 'usable') return 'success'
  if (status === 'needs_review') return 'warning'
  if (status === 'disabled') return 'muted'
  return 'muted'
}

// 判断实体是否匹配当前前端搜索词；后端 MVP 暂无搜索参数，因此只筛当前页。
export function entityMatchesQuery(
  entity: { name: string; entity_type: string; aliases?: string[]; description?: string | null },
  query: string,
): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return [entity.name, entity.entity_type, entity.description || '', ...(entity.aliases || [])]
    .join(' ')
    .toLowerCase()
    .includes(q)
}

// 判断关系是否匹配当前前端搜索词；用于同一页内快速缩小候选范围。
export function relationMatchesQuery(
  relation: Pick<KgRelation, 'head_entity_name' | 'head_entity_type' | 'relation_type' | 'tail_entity_name' | 'tail_entity_type' | 'description'>,
  query: string,
): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return [
    relation.head_entity_name,
    relation.head_entity_type,
    relation.relation_type,
    relation.tail_entity_name,
    relation.tail_entity_type,
    relation.description || '',
  ]
    .join(' ')
    .toLowerCase()
    .includes(q)
}
