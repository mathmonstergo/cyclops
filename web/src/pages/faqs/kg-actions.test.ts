import assert from 'node:assert/strict'
import test from 'node:test'
import { canExtractFaqKg } from './kg-actions.ts'

test('allows FAQ KG extraction only for a clean saved usable record', () => {
  assert.equal(canExtractFaqKg({ isNew: false, dirty: false, status: 'usable' }), true)
  assert.equal(canExtractFaqKg({ isNew: true, dirty: false, status: 'usable' }), false)
  assert.equal(canExtractFaqKg({ isNew: false, dirty: true, status: 'usable' }), false)
  assert.equal(canExtractFaqKg({ isNew: false, dirty: false, status: 'needs_review' }), false)
  assert.equal(canExtractFaqKg({ isNew: false, dirty: false, status: 'disabled' }), false)
})
