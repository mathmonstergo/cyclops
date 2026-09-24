// 智能问答 SSE 客户端：fetch ReadableStream + `\n\n` 分块 + JSON data 解析 + AbortSignal 支持。
// 后端事件契约：admin_server.py:format_sse_event → `event: <type>\ndata: <json>\n\n`，data 内 type 字段与事件类型一致。
import { ApiError } from '../api/client.ts'
import type { AssistantSource, AssistantStreamPayload } from '@/api/schemas'

export interface AssistantMetaEvent {
  type: 'meta'
  flow_id: string
  flow_name?: string
  stream?: boolean
  available_nodes?: string[]
  enabled_nodes?: string[]
}

export interface AssistantStepEvent {
  type: 'step'
  step_id: string
  title: string
  status: 'running' | 'completed' | 'failed' | string
  duration_ms?: number
  summary?: string
  documents?: AssistantSource[]
  analysis?: Record<string, unknown>
  [key: string]: unknown
}

export interface AssistantDeltaEvent {
  type: 'delta'
  text: string
}

export interface AssistantDoneEvent {
  type: 'done'
  flow_id: string
  question: string
  answer_draft: string
  documents: AssistantSource[]
}

export interface AssistantErrorEvent {
  type: 'error'
  error?: string
  message?: string
  [key: string]: unknown
}

export type AssistantSseEvent =
  | AssistantMetaEvent
  | AssistantStepEvent
  | AssistantDeltaEvent
  | AssistantDoneEvent
  | AssistantErrorEvent

interface StreamArgs {
  payload: AssistantStreamPayload
  signal: AbortSignal
  onEvent: (event: AssistantSseEvent) => void
}

export async function streamAssistantChat({
  payload,
  signal,
  onEvent,
}: StreamArgs): Promise<void> {
  const res = await fetch('/api/assistant/chat-stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(payload),
    signal,
  })

  if (!res.ok || !res.body) {
    let message = `请求失败 (${res.status})`
    try {
      const text = await res.text()
      if (text) {
        const json = JSON.parse(text) as { error?: unknown }
        if (typeof json.error === 'string' && json.error.trim()) message = json.error
      }
    } catch {
      // 非 JSON 错误体只能显示 HTTP 状态，不能伪造业务原因。
    }
    throw new ApiError(message, res.status, 'http_error')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  const dispatchBlock = (block: string) => {
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart())
      }
    }
    if (!dataLines.length) return
    try {
      const evt = JSON.parse(dataLines.join('\n')) as AssistantSseEvent
      onEvent(evt)
    } catch {
      // 容忍偶发非 JSON 事件
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      dispatchBlock(block)
    }
  }
  // 尾部冲洗：流结束但最后一块没有以 \n\n 结尾时仍尝试解析。
  if (buf.trim()) dispatchBlock(buf)
}
