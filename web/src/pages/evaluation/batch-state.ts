import type { EvaluationStrategy } from './helpers'

export type EvaluationBatchRunState = {
  status: 'idle' | 'running' | 'done'
  total: number
  completed: number
  succeeded: number
  failed: number
  currentQuestion?: string
}

export type EvaluationBatchRunSnapshot = {
  strategy: EvaluationStrategy
  runState: EvaluationBatchRunState
}

export const EMPTY_BATCH_RUN_STATE: EvaluationBatchRunState = {
  status: 'idle',
  total: 0,
  completed: 0,
  succeeded: 0,
  failed: 0,
}

// 只向当前策略暴露同策略批次状态；策略不匹配时必须呈现 idle，禁止复用上一策略徽章。
export function selectEvaluationBatchRunState(
  snapshot: EvaluationBatchRunSnapshot | null,
  strategy: EvaluationStrategy,
): EvaluationBatchRunState {
  if (!snapshot || snapshot.strategy !== strategy) return EMPTY_BATCH_RUN_STATE
  return snapshot.runState
}
