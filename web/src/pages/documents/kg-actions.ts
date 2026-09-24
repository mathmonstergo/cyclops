import type { KgExtractionJob } from '@/api/schemas'

type KgExtractionCounts = Pick<KgExtractionJob, 'entity_count' | 'relation_count'>

interface ChunkPageLocator {
  page_start?: number | null
  page_end?: number | null
}

// 整理 KG 抽取完成提示，关键约束是直接显示后端返回的实体/关系计数。
export function formatKgExtractionResult(result: KgExtractionCounts): string {
  return `KG 候选已生成：${result.entity_count} 个实体，${result.relation_count} 条关系`
}

// 格式化切片页码定位，关键约束是只显示页码，不把 block_type 这类解析内部类型暴露给用户。
export function formatChunkPageLocator(chunk: ChunkPageLocator): string {
  const start = chunk.page_start
  if (start === null || start === undefined) return ''
  const end = chunk.page_end
  if (end !== null && end !== undefined && end !== start) return `p${start}-${end}`
  return `p${start}`
}

// 缩短文档和切片 ID 的展示文本，关键约束是复制时仍使用完整 ID。
export function shortDocumentId(value: string | null | undefined): string {
  const clean = String(value || '').trim()
  if (clean.length <= 14) return clean
  return `${clean.slice(0, 12)}...`
}
