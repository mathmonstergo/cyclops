import assert from 'node:assert/strict'
import test from 'node:test'
import {
  EMPTY_BATCH_RUN_STATE,
  type EvaluationBatchRunSnapshot,
  type EvaluationBatchRunState,
  selectEvaluationBatchRunState,
} from './batch-state.ts'

const DONE_BATCH_RUN_STATE: EvaluationBatchRunState = {
  status: 'done',
  total: 2,
  completed: 2,
  succeeded: 2,
  failed: 0,
}

test('shows a completed batch snapshot only for its own strategy', () => {
  const baselineSnapshot: EvaluationBatchRunSnapshot = {
    strategy: 'baseline',
    runState: DONE_BATCH_RUN_STATE,
  }
  const kgSnapshot: EvaluationBatchRunSnapshot = {
    strategy: 'kg_debug',
    runState: DONE_BATCH_RUN_STATE,
  }

  assert.equal(selectEvaluationBatchRunState(baselineSnapshot, 'baseline').status, 'done')
  assert.deepEqual(
    selectEvaluationBatchRunState(baselineSnapshot, 'kg_debug'),
    EMPTY_BATCH_RUN_STATE,
  )
  assert.equal(selectEvaluationBatchRunState(kgSnapshot, 'kg_debug').status, 'done')
  assert.deepEqual(
    selectEvaluationBatchRunState(kgSnapshot, 'baseline'),
    EMPTY_BATCH_RUN_STATE,
  )
})

test('shows idle state before any strategy has a batch snapshot', () => {
  assert.deepEqual(selectEvaluationBatchRunState(null, 'baseline'), EMPTY_BATCH_RUN_STATE)
  assert.deepEqual(selectEvaluationBatchRunState(null, 'kg_debug'), EMPTY_BATCH_RUN_STATE)
})
