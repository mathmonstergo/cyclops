import type { AssistantSource } from '@/api/schemas'

export type AssistantSourceTarget = {
  message_id?: string
  key?: string
  source_type?: string
  group_key?: string
}

// 生成来源卡片稳定定位 key，关键约束是缺 canonical source_id 时立即暴露契约错误。
export function buildAssistantSourceTargetKey(source: AssistantSource): string {
  if (!source.source_id) throw new Error('assistant source requires canonical source_id')
  const sourceType = source.source_type
  const sourceId = source.source_id
  const chunkId = source.source_chunk_id ?? source.id
  return `${sourceType}:${sourceId}:${chunkId}`
}

// 生成来源分组 key，关键约束是与回答下方 chip 的 FAQ/文档分组保持一致。
export function buildAssistantSourceGroupKey(source: AssistantSource): string {
  if (source.source_type === 'faq') return '__faq__'
  return String(source.source_title || source.source_id || '未命名文档')
}

// 从 chip 对应来源生成抽屉定位目标，关键约束是分组 chip 定位到该组第一条来源。
export function buildAssistantSourceGroupTarget(
  source: AssistantSource | undefined,
): AssistantSourceTarget | null {
  if (!source) return null
  return {
    source_type: String(source.source_type || ''),
    group_key: buildAssistantSourceGroupKey(source),
  }
}

// 在抽屉来源列表中查找定位目标，关键约束是 key 精确匹配优先，分组匹配兜底。
export function findAssistantSourceTargetIndex(
  sources: AssistantSource[],
  target: AssistantSourceTarget | null | undefined,
): number {
  if (!target) return -1
  if (target.key) {
    const index = sources.findIndex((source) => buildAssistantSourceTargetKey(source) === target.key)
    if (index >= 0) return index
  }
  if (target.group_key) {
    return sources.findIndex((source) => {
      if (target.source_type && source.source_type !== target.source_type) return false
      return buildAssistantSourceGroupKey(source) === target.group_key
    })
  }
  return -1
}
