import type { KgExtractionJob } from './schemas'

interface WaitForKgExtractionJobOptions {
  intervalMs?: number
  maxAttempts?: number
  sleep?: (delayMs: number) => Promise<void>
}

type LoadKgExtractionJob = (jobId: string) => Promise<KgExtractionJob>

// 默认最多等待两分钟，覆盖后端 60 秒模型超时及排队/状态提交余量。
const DEFAULT_POLL_INTERVAL_MS = 500
const DEFAULT_MAX_POLL_ATTEMPTS = 240

// 轮询唯一的异步 KG 任务契约；failed/未知状态立即失败，只有 completed 才返回成功。
export async function waitForKgExtractionJob(
  initialJob: KgExtractionJob,
  loadJob: LoadKgExtractionJob,
  options: WaitForKgExtractionJobOptions = {},
): Promise<KgExtractionJob> {
  if (initialJob.status !== 'queued') {
    throw new Error('KG extraction POST must return a queued job')
  }
  const intervalMs = options.intervalMs ?? DEFAULT_POLL_INTERVAL_MS
  const maxAttempts = options.maxAttempts ?? DEFAULT_MAX_POLL_ATTEMPTS
  const sleep = options.sleep ?? wait
  let current = initialJob

  for (let attempt = 0; attempt <= maxAttempts; attempt += 1) {
    if (current.status === 'completed') return current
    if (current.status === 'failed') {
      throw new Error(current.error || 'KG 抽取失败')
    }
    if (current.status !== 'queued' && current.status !== 'processing') {
      throw new Error(`unknown KG extraction job status: ${current.status}`)
    }
    if (attempt === maxAttempts) {
      throw new Error('KG 抽取任务等待超时')
    }
    await sleep(intervalMs)
    current = await loadJob(current.id)
  }

  throw new Error('KG 抽取任务等待超时')
}

// 等待指定毫秒数，供浏览器轮询复用且允许测试注入无等待实现。
function wait(delayMs: number): Promise<void> {
  if (delayMs <= 0) return Promise.resolve()
  return new Promise((resolve) => globalThis.setTimeout(resolve, delayMs))
}
