import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('current retrieval and KG DTOs do not expose open compatibility fields', () => {
  const source = readFileSync(new URL('./schemas.ts', import.meta.url), 'utf8')
  const contractStart = source.indexOf('// Retrieval Evaluation')

  assert.notEqual(contractStart, -1)
  assert.doesNotMatch(source.slice(contractStart), /\[key:\s*string\]:\s*unknown/)
})

test('assistant and evaluation candidates require canonical top-level source fields', () => {
  const source = readFileSync(new URL('./schemas.ts', import.meta.url), 'utf8')
  const assistantSource = source.slice(
    source.indexOf('export interface AssistantSource'),
    source.indexOf('export interface AssistantStreamPayload'),
  )
  const evaluationItem = source.slice(
    source.indexOf('export interface RetrievalEvalItem'),
    source.indexOf('export interface RetrievalEvalRun'),
  )

  for (const contract of [assistantSource, evaluationItem]) {
    assert.match(contract, /source_id:\s*string/)
    assert.match(contract, /source_chunk_id:\s*string\s*\|\s*null/)
    assert.match(contract, /source_title:\s*string\s*\|\s*null/)
    assert.match(contract, /content:\s*string/)
    assert.doesNotMatch(contract, /\bchunk_id\??:/)
    assert.doesNotMatch(contract, /\btitle\??:/)
    assert.doesNotMatch(contract, /\btext\??:/)
  }

  for (const redundantField of ['question', 'answer', 'category', 'tags', 'metadata']) {
    assert.doesNotMatch(evaluationItem, new RegExp(`\\b${redundantField}\\??:`))
  }
})

test('KG review candidates carry the exact revision used by confirm requests', () => {
  // 列表 DTO、hook body 和页面调用必须传递同一个 revision，不能只按稳定 ID 确认。
  const schemas = readFileSync(new URL('./schemas.ts', import.meta.url), 'utf8')
  const hooks = readFileSync(new URL('./hooks.ts', import.meta.url), 'utf8')
  const page = readFileSync(
    new URL('../pages/KnowledgeGraphPage.tsx', import.meta.url),
    'utf8',
  )
  const kgContracts = schemas.slice(schemas.indexOf('// Knowledge Graph'))

  assert.equal((kgContracts.match(/review_revision:\s*number/g) || []).length, 2)
  assert.equal(
    (hooks.match(/body:\s*\{\s*expected_revision:\s*expectedRevision\s*\}/g) || [])
      .length,
    2,
  )
  assert.doesNotMatch(hooks, /\/confirm[^]*?body:\s*\{\s*\}/)
  assert.equal(
    (
      page.match(
        /mutateAsync\(\{\s*id:\s*item\.id,\s*expectedRevision:\s*item\.review_revision,?\s*\}\)/g,
      ) || []
    ).length,
    2,
  )
})

test('KG DTOs require backend-computed live counts and evidence validity', () => {
  // 实时有效数量和历史证据有效性必须由后端显式返回，前端不能从数组长度猜测。
  const schemas = readFileSync(new URL('./schemas.ts', import.meta.url), 'utf8')
  const evidenceContract = schemas.slice(
    schemas.indexOf('export interface KgEvidence'),
    schemas.indexOf('export interface KgEntity'),
  )
  const entityContract = schemas.slice(
    schemas.indexOf('export interface KgEntity'),
    schemas.indexOf('export interface KgRelation'),
  )
  const relationContract = schemas.slice(
    schemas.indexOf('export interface KgRelation'),
    schemas.indexOf('export interface KgListResponse'),
  )
  const edgeContract = schemas.slice(
    schemas.indexOf('export interface KgSubgraphEdge'),
    schemas.indexOf('export interface KgSubgraphResponse'),
  )

  assert.match(evidenceContract, /\bis_valid:\s*boolean/)
  assert.doesNotMatch(evidenceContract, /\bis_valid\?:/)
  assert.match(entityContract, /\bsource_count:\s*number/)
  assert.doesNotMatch(entityContract, /\bsource_count\?:/)
  assert.match(relationContract, /\bevidence_count:\s*number/)
  assert.doesNotMatch(relationContract, /\bevidence_count\?:/)
  assert.match(edgeContract, /\bevidence_count:\s*number/)
  assert.doesNotMatch(edgeContract, /\bevidence_count\?:/)
})

test('KG review UI separates live counts from the complete evidence history', () => {
  // 列表展示后端实时计数；抽屉保留全部历史，并用低对比标签标出失效来源。
  const page = readFileSync(
    new URL('../pages/KnowledgeGraphPage.tsx', import.meta.url),
    'utf8',
  )

  assert.match(page, /<span>有效来源<\/span>/)
  assert.match(page, /\{item\.source_count\}/)
  assert.match(page, /<span>有效证据<\/span>/)
  assert.match(page, /\{item\.evidence_count\}/)
  assert.doesNotMatch(page, /item\.evidence\??\.length/)
  assert.match(page, /title=\{`证据历史 \(\$\{evidence\.length\}\)`\}/)
  assert.match(page, /evidence\.map\(\(item\)\s*=>/)
  assert.match(
    page,
    /!item\.is_valid\s*&&\s*\(\s*<span className="[^"]*text-\(--color-text-faint\)[^"]*">来源已失效<\/span>/,
  )
})
