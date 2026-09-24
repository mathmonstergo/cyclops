import assert from 'node:assert/strict'
import test from 'node:test'
import { createEvaluationRunMutex } from './run-mutex.ts'

test('allows only one evaluation run lease and ignores stale releases', () => {
  const mutex = createEvaluationRunMutex()
  const firstLease = mutex.tryAcquire()

  assert.notEqual(firstLease, null)
  assert.equal(mutex.tryAcquire(), null)

  mutex.release(Symbol('stale'))
  assert.equal(mutex.tryAcquire(), null)

  if (firstLease === null) throw new Error('first evaluation run lease is required')
  mutex.release(firstLease)
  assert.notEqual(mutex.tryAcquire(), null)
})
