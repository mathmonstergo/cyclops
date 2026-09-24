import type { DocumentChunkerType } from '@/api/schemas'

export type { DocumentChunkerType } from '@/api/schemas'

export const DOCUMENT_CHUNKER_OPTIONS: {
  value: DocumentChunkerType
  label: string
}[] = [
  { value: 'naive', label: 'Naive' },
  { value: 'manual', label: 'Manual' },
  { value: 'qa', label: 'Q/A' },
  { value: 'table', label: 'Table' },
]

const DOCUMENT_CHUNKER_LABELS: Record<DocumentChunkerType, string> = {
  naive: 'Naive',
  manual: 'Manual',
  qa: 'Q/A',
  table: 'Table',
}

// 校验后端返回的 chunker 字段；缺失、大小写别名和未知值都直接暴露契约错误。
export function requireDocumentChunkerType(value: unknown): DocumentChunkerType {
  if (value === 'naive' || value === 'manual' || value === 'qa' || value === 'table') {
    return value
  }
  throw new Error('document chunker_type must be canonical')
}

// 统一列表和抽屉的 chunker 标签，避免同一枚举在多个组件里重复硬编码。
export function documentChunkerLabel(value: unknown): string {
  return DOCUMENT_CHUNKER_LABELS[requireDocumentChunkerType(value)]
}
