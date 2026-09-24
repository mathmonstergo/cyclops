// 统一 fetch 封装 + 错误归一化。
// classify_error_response 的当前唯一错误体是 { error: string } + 4xx/5xx 状态码。

export class ApiError extends Error {
  status: number
  code: string

  constructor(message: string, status: number, code: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

interface FetchOptions extends Omit<RequestInit, 'body'> {
  query?: Record<string, string | number | boolean | undefined | null>
  body?: unknown
}

function buildUrl(path: string, query?: FetchOptions['query']): string {
  if (!query) return path
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const qs = search.toString()
  return qs ? `${path}?${qs}` : path
}

export async function requestJson<T = unknown>(
  path: string,
  options: FetchOptions = {},
): Promise<T> {
  const { query, headers, body, ...rest } = options
  const url = buildUrl(path, query)
  const isJsonBody =
    body !== undefined && body !== null && !(body instanceof FormData) && !(body instanceof Blob)
  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...((headers as Record<string, string>) || {}),
  }
  if (isJsonBody && !finalHeaders['Content-Type']) {
    finalHeaders['Content-Type'] = 'application/json'
  }
  const res = await fetch(url, {
    ...rest,
    headers: finalHeaders,
    body: isJsonBody && typeof body !== 'string' ? JSON.stringify(body) : (body as BodyInit | null),
  })
  if (res.status === 204) return undefined as T
  const text = await res.text()
  let parsed: unknown = undefined
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      // 非 JSON 响应
    }
  }
  if (!res.ok) {
    const errorValue = (parsed as { error?: unknown } | undefined)?.error
    const message = typeof errorValue === 'string' && errorValue.trim() ? errorValue : null
    throw new ApiError(
      message || `请求失败 (${res.status})`,
      res.status,
      'http_error',
    )
  }
  return parsed as T
}
