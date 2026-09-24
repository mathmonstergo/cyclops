interface FaqKgEligibility {
  isNew: boolean
  dirty: boolean
  status: string | null | undefined
}

// FAQ 只有正式保存、无未保存修改且当前 usable 时才允许发起 KG 抽取。
export function canExtractFaqKg({ isNew, dirty, status }: FaqKgEligibility): boolean {
  return !isNew && !dirty && status === 'usable'
}
