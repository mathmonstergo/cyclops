import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiError } from '../api/client.ts'
import { streamAssistantChat } from './sse-assistant.ts'

test('uses the canonical backend error string for assistant HTTP failures', async (context) => {
  // SSE 建连失败仍使用统一 { error: string } 错误体，不读取嵌套兼容形状。
  const originalFetch = globalThis.fetch
  context.after(() => {
    globalThis.fetch = originalFetch
  })
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ error: '问题不能为空' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    })

  await assert.rejects(
    streamAssistantChat({
      payload: { question: '' },
      signal: new AbortController().signal,
      onEvent: () => undefined,
    }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.message, '问题不能为空')
      return true
    },
  )
})
