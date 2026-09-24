// 全局状态字典：所有状态文案统一中文，禁止页面里写英文 raw 值。
// 后端字段命名不变，只在 UI 渲染时翻译。

export const parseStateLabel: Record<string, string> = {
  pending: '排队中',
  running: '解析中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export const embeddingStatusLabel: Record<string, string> = {
  pending: '未索引',
  ready: '已索引',
  stale: '过期',
  failed: '索引失败',
  none: '未生成',
  partial: '部分索引',
}

export const questionsStatusLabel: Record<string, string> = {
  pending: '未生成',
  ready: '已生成',
  failed: '生成失败',
  skipped: '已跳过',
}

export const faqStatusLabel: Record<string, string> = {
  usable: '可用',
  needs_review: '待复核',
  disabled: '禁用',
}

export const kgReviewStatusLabelMap: Record<string, string> = {
  usable: '已确认',
  needs_review: '待审核',
  disabled: '已停用',
}

export const confidenceLabel: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}

// 智能问答：意图识别结果中文映射，跟 retrieval.py 的 INTENT_* 常量保持同步。
export const intentLabel: Record<string, string> = {
  faq_exact: 'FAQ 精确匹配',
  procedure: '操作流程',
  troubleshooting: '故障排查',
  realtime_status: '实时状态查询',
  chitchat_or_out_of_scope: '闲聊 / 超范围',
  sensitive_or_forbidden: '敏感词拦截',
}

export function tr(dict: Record<string, string>, value: string | null | undefined, fallback = ''): string {
  if (!value) return fallback
  return dict[value] || value
}
