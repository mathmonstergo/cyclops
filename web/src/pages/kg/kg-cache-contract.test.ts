import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const hooksSource = readFileSync(new URL('../../api/hooks.ts', import.meta.url), 'utf8')
const documentDrawerSource = readFileSync(
  new URL('../documents/document-drawer.tsx', import.meta.url),
  'utf8',
)

test('KG review invalidation helper awaits all three cache families', () => {
  // 用静态契约锁定唯一 helper 的精确 key 和 Promise 聚合，避免 mock 自证实现正确。
  const helperStart = hooksSource.indexOf(
    'export async function invalidateKgReviewQueries',
  )
  const helperEnd = hooksSource.indexOf('export interface KgEntityListParams', helperStart)

  assert.notEqual(helperStart, -1)
  assert.notEqual(helperEnd, -1)
  const helper = hooksSource.slice(helperStart, helperEnd)
  assert.match(
    helper,
    /invalidateKgReviewQueries\(queryClient:\s*QueryClient\):\s*Promise<void>/,
  )
  assert.match(helper, /await Promise\.all\(\[/)
  assert.equal((helper.match(/queryClient\.invalidateQueries/g) || []).length, 3)
  for (const queryKey of ['kg-entities', 'kg-relations', 'kg-subgraph']) {
    assert.equal((helper.match(new RegExp(`queryKey: \\['${queryKey}'\\]`, 'g')) || []).length, 1)
  }
})

test('all current KG review mutations reuse the single invalidation helper', () => {
  // confirm、status 与抽取完成必须刷新同一组缓存，调用者内不得再维护局部 key 清单。
  const mutations = [
    ['useConfirmKgEntity', 'useConfirmKgRelation'],
    ['useConfirmKgRelation', 'useSetKgEntityStatus'],
    ['useSetKgEntityStatus', 'useSetKgRelationStatus'],
    ['useSetKgRelationStatus', 'useKgSubgraph'],
    ['useCreateKgExtractionJob', null],
  ] as const

  for (const [name, nextName] of mutations) {
    const start = hooksSource.indexOf(`export function ${name}`)
    const end = nextName
      ? hooksSource.indexOf(`export function ${nextName}`, start)
      : hooksSource.length
    assert.notEqual(start, -1, `${name} must exist`)
    assert.notEqual(end, -1, `${name} must have a stable source boundary`)
    const mutation = hooksSource.slice(start, end)
    assert.match(
      mutation,
      /onSuccess:\s*async\s*\(\)\s*=>\s*\{\s*await invalidateKgReviewQueries\(qc\)\s*\}/,
      `${name} must await the shared KG invalidation helper`,
    )
    assert.doesNotMatch(mutation, /qc\.invalidateQueries\(\{\s*queryKey:\s*\['kg-/)
  }
})

test('all KG review queries refetch whenever their observer mounts', () => {
  // KG 页面必须绕过 10 秒 fresh cache，接住离开来源页期间发生的后台或 out-of-band 变更。
  const queries = [
    ['useKgEntities', 'export interface KgRelationListParams'],
    ['useKgRelations', 'export function useConfirmKgEntity'],
    ['useKgSubgraph', 'export function useCreateKgExtractionJob'],
  ] as const

  for (const [name, boundary] of queries) {
    const start = hooksSource.indexOf(`export function ${name}`)
    const end = hooksSource.indexOf(boundary, start)
    assert.notEqual(start, -1, `${name} must exist`)
    assert.notEqual(end, -1, `${name} must have a stable source boundary`)
    const query = hooksSource.slice(start, end)
    assert.equal((query.match(/refetchOnMount:\s*'always'/g) || []).length, 1)
  }
})

test('all KG owner source mutations await the shared invalidation helper on success', () => {
  // 只锁定会改变 evidence 实时有效性的来源写路径，不扩散到 embedding、评测或只读 hook。
  const sourceMutations = [
    ['useDeleteImportFile', 'useEmbedImportFile'],
    ['useToggleImportFileDisabled', 'useToggleImportChunkDisabled'],
    ['useToggleImportChunkDisabled', 'useUpdateImportChunk'],
    ['useUpdateImportChunk', 'useEmbedImportChunk'],
    ['useSaveFaq', 'useEmbedFaq'],
  ] as const

  for (const [name, nextName] of sourceMutations) {
    const start = hooksSource.indexOf(`export function ${name}`)
    const end = hooksSource.indexOf(`export function ${nextName}`, start)
    assert.notEqual(start, -1, `${name} must exist`)
    assert.notEqual(end, -1, `${name} must have a stable source boundary`)
    const mutation = hooksSource.slice(start, end)
    assert.match(
      mutation,
      /onSuccess:\s*async\s*\([^)]*\)\s*=>\s*\{[^]*?await invalidateKgReviewQueries\(qc\)/,
      `${name} must await the shared KG invalidation helper after success`,
    )
    assert.equal((mutation.match(/invalidateKgReviewQueries\(qc\)/g) || []).length, 1)
    assert.doesNotMatch(mutation, /qc\.invalidateQueries\(\{\s*queryKey:\s*\['kg-/)
  }
})

test('synchronous Markdown parse completion invalidates KG but queued parsing does not', () => {
  // start hook 只有返回 needs_review/completed 时才完成了 snapshot replacement；processing 不得提前刷新 KG。
  const start = hooksSource.indexOf('export function useStartImportParseJob')
  const end = hooksSource.indexOf('export function useToggleImportFileDisabled', start)

  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const mutation = hooksSource.slice(start, end)
  assert.match(mutation, /onSuccess:\s*async\s*\(data,\s*vars\)\s*=>\s*\{/)
  assert.match(
    mutation,
    /if\s*\(\s*data\.file\.status === 'needs_review'\s*\|\|\s*data\.file\.status === 'completed'\s*\)\s*\{\s*await invalidateKgReviewQueries\(qc\)\s*\}/,
  )
  assert.equal((mutation.match(/invalidateKgReviewQueries\(qc\)/g) || []).length, 1)
  assert.doesNotMatch(mutation, /data\.file\.status === 'failed'[^]*invalidateKgReviewQueries/)
})

test('parse status response boundary invalidates KG for every successful terminal snapshot', () => {
  // queryFn 不依赖 previous 状态；首次 GET 直接完成 provider 时也必须等待 KG 缓存失效。
  const start = hooksSource.indexOf('export function useImportFileParseStatus')
  const end = hooksSource.indexOf('export interface ParseStatusResponse', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  const query = hooksSource.slice(start, end)

  assert.match(query, /const qc = useQueryClient\(\)/)
  assert.match(query, /queryFn:\s*async\s*\(\)\s*=>\s*\{/)
  assert.match(
    query,
    /const response = await requestJson<ParseStatusResponse>\([^]*?\)\s*if\s*\(\s*response\.file\.status === 'needs_review'\s*\|\|\s*response\.file\.status === 'completed'\s*\)\s*\{\s*await invalidateKgReviewQueries\(qc\)\s*\}\s*return response/,
  )
  assert.equal((query.match(/invalidateKgReviewQueries\(qc\)/g) || []).length, 1)
  assert.doesNotMatch(query, /response\.file\.status === 'failed'/)
  assert.doesNotMatch(query, /prev(?:ious)?Status|prev\s*===/)
})

test('DocumentDrawer leaves KG invalidation to the parse response boundary', () => {
  // Drawer 只保留文档缓存和 toast；组件卸载不能再决定 KG 是否刷新。
  assert.doesNotMatch(documentDrawerSource, /invalidateKgReviewQueries/)
  const drawerStart = documentDrawerSource.indexOf('function DrawerInner')
  const drawerEnd = documentDrawerSource.indexOf('function TaskPanel', drawerStart)
  assert.notEqual(drawerStart, -1)
  assert.notEqual(drawerEnd, -1)
  const drawer = documentDrawerSource.slice(drawerStart, drawerEnd)
  const effectStart = drawer.indexOf('const prevStatusRef')
  const effectEnd = drawer.indexOf('const parseJob', effectStart)
  assert.notEqual(effectStart, -1)
  assert.notEqual(effectEnd, -1)
  const terminalEffect = drawer.slice(effectStart, effectEnd)

  assert.match(terminalEffect, /prev === 'processing' && cur && cur !== 'processing'/)
  assert.match(terminalEffect, /qc\.invalidateQueries\(\{ queryKey: \['import-chunks', fileId\] \}\)/)
  assert.match(terminalEffect, /qc\.invalidateQueries\(\{ queryKey: \['import-files'\] \}\)/)
  assert.match(terminalEffect, /if \(cur === 'failed'\)[^]*toast\.error/)
  assert.match(
    terminalEffect,
    /else if \(cur === 'needs_review' \|\| cur === 'completed'\)[^]*toast\.success/,
  )
})
