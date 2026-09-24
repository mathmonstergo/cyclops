import assert from 'node:assert/strict'
import test from 'node:test'
import {
  documentChunkerLabel,
  requireDocumentChunkerType,
} from './chunker-options.ts'

test('accepts only canonical document chunker values', () => {
  assert.equal(requireDocumentChunkerType('naive'), 'naive')
  assert.equal(requireDocumentChunkerType('manual'), 'manual')
  assert.equal(requireDocumentChunkerType('qa'), 'qa')
  assert.equal(requireDocumentChunkerType('table'), 'table')
  assert.equal(documentChunkerLabel('qa'), 'Q/A')
})

test('rejects missing, differently-cased, and unknown document chunkers', () => {
  for (const value of [undefined, null, '', 'NAIVE', 'legacy']) {
    assert.throws(
      () => requireDocumentChunkerType(value),
      /document chunker_type must be canonical/,
    )
  }
})
