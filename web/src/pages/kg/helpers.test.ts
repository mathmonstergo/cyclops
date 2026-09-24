import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canConfirmKgCandidate,
  canConfirmKgRelation,
  clampKgPage,
  confidencePercent,
  evidenceSourceTarget,
  evidenceSummary,
  kgRelationConfirmBlockedReason,
  kgReviewStatusLabel,
  relationTitle,
} from './helpers.ts'

test('clamps KG pagination after the final row disappears', () => {
  assert.equal(clampKgPage(3, 59, 30), 2)
  assert.equal(clampKgPage(2, 0, 30), 1)
  assert.equal(clampKgPage(1, 91, 30), 1)
})

test('uses the mandatory live evidence flag before a KG candidate can be confirmed', () => {
  // 历史证据可继续展示，但确认资格只读取后端按实时来源状态计算的布尔值。
  const staleCandidate = { has_valid_evidence: false, evidence: [{ id: 'ev_1' }] }
  const liveCandidate = { has_valid_evidence: true, evidence: [] }

  assert.equal(canConfirmKgCandidate(staleCandidate), false)
  assert.equal(canConfirmKgCandidate(liveCandidate), true)
})

test('requires usable endpoints and the live evidence flag before a KG relation can be confirmed', () => {
  // 即使历史 evidence 非空，来源已失效时也不能向后端发出必然失败的确认请求。
  const relation = {
    evidence: [{ id: 'ev_1' }],
    has_valid_evidence: true,
    head_entity_status: 'usable',
    tail_entity_status: 'usable',
  }

  assert.equal(canConfirmKgRelation(relation), true)
  assert.equal(canConfirmKgRelation({ ...relation, head_entity_status: 'needs_review' }), false)
  assert.equal(canConfirmKgRelation({ ...relation, tail_entity_status: 'disabled' }), false)
  assert.equal(canConfirmKgRelation({ ...relation, has_valid_evidence: false }), false)
})

test('explains that relation confirmation is blocked by unconfirmed endpoints', () => {
  // 已有有效证据时，提示必须指出端点状态，不能错误声称证据缺失。
  assert.equal(
    kgRelationConfirmBlockedReason({
      has_valid_evidence: true,
      head_entity_status: 'needs_review',
      tail_entity_status: 'usable',
    }),
    '请先确认头尾实体',
  )
})

test('maps KG evidence to exact source drawer targets', () => {
  assert.deepEqual(
    evidenceSourceTarget({ source_type: 'faq', source_id: 'faq_1', source_chunk_id: null }),
    { kind: 'faq', sourceId: 'faq_1' },
  )
  assert.deepEqual(
    evidenceSourceTarget({
      source_type: 'document',
      source_id: 'imp_1',
      source_chunk_id: 'chunk_1',
    }),
    { kind: 'document', sourceId: 'imp_1', sourceChunkId: 'chunk_1' },
  )
  assert.equal(
    evidenceSourceTarget({
      source_type: 'document',
      source_id: 'imp_1',
      source_chunk_id: null,
    }),
    null,
  )
})

test('maps KG review statuses to operator-facing labels', () => {
  assert.equal(kgReviewStatusLabel('needs_review'), '待审核')
  assert.equal(kgReviewStatusLabel('usable'), '已确认')
  assert.equal(kgReviewStatusLabel('disabled'), '已停用')
  assert.equal(kgReviewStatusLabel('unknown'), 'unknown')
})

test('formats confidence as a stable percentage', () => {
  assert.equal(confidencePercent(0.923), '92%')
  assert.equal(confidencePercent(null), '未标注')
  assert.equal(confidencePercent(undefined), '未标注')
})

test('builds compact evidence summaries', () => {
  assert.equal(
    evidenceSummary({
      source_title: '检索技术白皮书',
      section_path: ['2.1 混合检索', '召回策略'],
      page_start: 12,
      page_end: 13,
    }),
    '检索技术白皮书 / 2.1 混合检索 / 召回策略 / 第 12-13 页',
  )
})

test('keeps zero-based evidence page numbers visible', () => {
  assert.equal(
    evidenceSummary({
      source_title: '封面说明',
      section_path: [],
      page_start: 0,
      page_end: 0,
    }),
    '封面说明 / 第 0 页',
  )
})

test('formats relation title from head, type and tail', () => {
  assert.equal(
    relationTitle({
      head_entity_name: '混合检索',
      relation_type: '优化',
      tail_entity_name: '召回率',
    }),
    '混合检索 - 优化 - 召回率',
  )
})
