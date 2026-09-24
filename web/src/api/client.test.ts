import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiError, requestJson } from './client.ts'

test('uses the canonical backend error string as the request failure message', async (context) => {
  // 当前后端错误体只有 { error: string }；客户端不得等待不存在的嵌套 message。
  const originalFetch = globalThis.fetch
  context.after(() => {
    globalThis.fetch = originalFetch
  })
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ error: '关系端点尚未确认' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    })

  await assert.rejects(
    requestJson('/api/kg/relations/kg_rel_1/confirm', {
      method: 'POST',
      body: { expected_revision: 1 },
    }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.message, '关系端点尚未确认')
      assert.equal(error.status, 400)
      assert.equal(error.code, 'http_error')
      return true
    },
  )
})
