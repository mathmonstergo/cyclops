import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('assistant source rendering does not recover canonical fields from legacy aliases', () => {
  const source = readFileSync(new URL('./debug-drawer.tsx', import.meta.url), 'utf8')

  assert.doesNotMatch(source, /src\.title\b/)
  assert.doesNotMatch(source, /src\.text\b/)
  assert.doesNotMatch(source, /src\.metadata\?\.page_start/)
  assert.doesNotMatch(source, /metadata\?\.parent_content/)
})
