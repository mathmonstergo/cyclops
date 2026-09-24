import assert from 'node:assert/strict'
import test from 'node:test'
import type { KgExtractionJob } from './schemas.ts'
import { waitForKgExtractionJob } from './kg-jobs.ts'

function job(status: KgExtractionJob['status'], overrides: Partial<KgExtractionJob> = {}): KgExtractionJob {
  // 构造最小任务夹具，关键约束是测试只改变当前状态和显式覆盖字段。
  return {
    id: 'kg_job_1',
    source_type: 'faq',
    source_id: 'faq_1',
    source_chunk_id: null,
    status,
    entity_count: 0,
    relation_count: 0,
    evidence_count: 0,
    ...overrides,
  }
}

test('polls queued KG job until completed', async () => {
  const states = [job('processing'), job('completed', { entity_count: 2 })]
  const loaded: string[] = []

  const completed = await waitForKgExtractionJob(
    job('queued'),
    async (jobId) => {
      loaded.push(jobId)
      const next = states.shift()
      assert.ok(next)
      return next
    },
    { intervalMs: 0, maxAttempts: 3 },
  )

  assert.equal(completed.status, 'completed')
  assert.equal(completed.entity_count, 2)
  assert.deepEqual(loaded, ['kg_job_1', 'kg_job_1'])
})

test('throws bounded backend error when KG job fails', async () => {
  await assert.rejects(
    waitForKgExtractionJob(
      job('queued'),
      async () => job('failed', { error: '模型 JSON 不合法' }),
      { intervalMs: 0 },
    ),
    /模型 JSON 不合法/,
  )
})

test('rejects unknown KG status instead of silently polling', async () => {
  await assert.rejects(
    waitForKgExtractionJob(
      job('queued'),
      async () => ({ ...job('queued'), status: 'migrating' as KgExtractionJob['status'] }),
      { intervalMs: 0 },
    ),
    /unknown KG extraction job status/,
  )
})

test('rejects every non-queued initial response from the KG POST contract', async () => {
  for (const status of ['processing', 'completed', 'failed'] as const) {
    await assert.rejects(
      waitForKgExtractionJob(job(status), async () => job('completed')),
      /KG extraction POST must return a queued job/,
    )
  }
})

test('times out after exactly maxAttempts polling cycles', async () => {
  const maxAttempts = 3
  let sleepCount = 0
  let loadCount = 0

  await assert.rejects(
    waitForKgExtractionJob(
      job('queued'),
      async () => {
        loadCount += 1
        return job('processing')
      },
      {
        intervalMs: 0,
        maxAttempts,
        sleep: async () => {
          sleepCount += 1
        },
      },
    ),
    /KG 抽取任务等待超时/,
  )

  assert.equal(sleepCount, maxAttempts)
  assert.equal(loadCount, maxAttempts)
})
