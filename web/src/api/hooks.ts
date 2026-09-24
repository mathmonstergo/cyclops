import {
  type QueryClient,
  useIsMutating,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { requestJson } from './client'
import { waitForKgExtractionJob } from './kg-jobs'
import type {
  AssistantSettingsSnapshot,
  DocumentChunkerType,
  Faq,
  FaqListResponse,
  ImportChunkListResponse,
  ImportFile,
  ImportListResponse,
  KgEntity,
  KgExtractionJob,
  KgListResponse,
  KgRelation,
  KgSubgraphResponse,
  MessagesResponse,
  ProviderModelsResponse,
  ProviderProbeResponse,
  RetrievalAlias,
  RetrievalAliasListResponse,
  RetrievalEvalCaseRecord,
  RetrievalEvalCaseListResponse,
  RetrievalEvalRun,
  RetrievalEvalRunPayload,
  SettingsSnapshot,
} from './schemas'

// ───── 文档 / Import ─────

export interface ImportListParams {
  query?: string
  status?: string
  limit?: number
  offset?: number
}

export function useImportFiles(
  params: ImportListParams = {},
  options?: {
    refetchInterval?:
      | number
      | false
      | ((q: { state: { data?: ImportListResponse } }) => number | false | undefined)
  },
) {
  return useQuery({
    queryKey: ['import-files', params],
    queryFn: () =>
      requestJson<ImportListResponse>('/api/import/files', {
        query: { ...params, limit: params.limit ?? 100 },
      }),
    staleTime: 10_000,
    refetchInterval: options?.refetchInterval as never,
  })
}

// 读取解析进度；成功终态在实际响应边界刷新 KG，不能依赖可能已卸载的页面 effect。
export function useImportFileParseStatus(
  fileId: string | null,
  options?: {
    refetchInterval?:
      | number
      | false
      | ((q: { state: { data?: ParseStatusResponse } }) => number | false | undefined)
  },
) {
  const qc = useQueryClient()
  return useQuery({
    queryKey: ['import-parse-status', fileId],
    queryFn: async () => {
      if (!fileId) throw new Error('fileId is required for import parse status')
      const response = await requestJson<ParseStatusResponse>(
        `/api/import/files/${encodeURIComponent(fileId)}/parse-status`,
      )
      if (
        response.file.status === 'needs_review' ||
        response.file.status === 'completed'
      ) {
        await invalidateKgReviewQueries(qc)
      }
      return response
    },
    enabled: !!fileId,
    refetchInterval: options?.refetchInterval as never,
    staleTime: 0,
  })
}

export interface ParseStatusResponse {
  file: ImportFile
  status: string
  state: string
  progress: Record<string, unknown> & {
    state?: string
    stage?: string
    message?: string
    current?: number
    total?: number
  }
  percent: number
  error: string | null
}

// 读取指定文档切片；禁用查询不能靠 non-null assertion 伪造 ID。
export function useImportFileChunks(
  fileId: string | null,
  options?: { refetchInterval?: number },
) {
  return useQuery({
    queryKey: ['import-chunks', fileId],
    queryFn: () => {
      if (!fileId) throw new Error('fileId is required for import chunks')
      return requestJson<ImportChunkListResponse>(
        `/api/import/files/${encodeURIComponent(fileId)}/chunks`,
      )
    },
    enabled: !!fileId,
    refetchInterval: options?.refetchInterval,
  })
}

// 删除文档会失效其 KG evidence；成功后同时刷新文档列表与统一 KG 审核读模型。
export function useDeleteImportFile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      requestJson<MessagesResponse>(`/api/import/files/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['import-files'] })
      await invalidateKgReviewQueries(qc)
    },
  })
}

export function useEmbedImportFile() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['embed-import-file'],
    mutationFn: (id: string) =>
      requestJson<MessagesResponse & { count?: number }>(
        `/api/import/files/${encodeURIComponent(id)}/embed`,
        { method: 'POST', body: {} },
      ),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['import-files'] })
      qc.invalidateQueries({ queryKey: ['import-chunks', id] })
      qc.invalidateQueries({ queryKey: ['import-parse-status', id] })
    },
  })
}

export function useGenerateImportFileQuestions() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['generate-questions'],
    mutationFn: ({ id, force }: { id: string; force?: boolean }) =>
      requestJson<MessagesResponse>(
        `/api/import/files/${encodeURIComponent(id)}/generate-questions`,
        { method: 'POST', body: { force: !!force } },
      ),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ['import-chunks', vars.id] })
    },
  })
}

// 启动解析任务；只有同步返回成功终态时已替换 snapshot，processing 阶段不能提前刷新 KG。
export function useStartImportParseJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['start-parse-job'],
    // 启动解析返回完整轮询快照；chunker 只接受当前四个 canonical 枚举值。
    mutationFn: ({ id, chunker_type, parser }: { id: string; chunker_type?: DocumentChunkerType; parser?: string }) =>
      requestJson<ParseStatusResponse>(
        `/api/import/files/${encodeURIComponent(id)}/parse-jobs`,
        { method: 'POST', body: { ...(parser ? { parser } : {}), ...(chunker_type ? { chunker_type } : {}) } },
      ),
    onSuccess: async (data, vars) => {
      qc.invalidateQueries({ queryKey: ['import-files'] })
      qc.invalidateQueries({ queryKey: ['import-parse-status', vars.id] })
      qc.invalidateQueries({ queryKey: ['import-chunks', vars.id] })
      if (
        data.file.status === 'needs_review' ||
        data.file.status === 'completed'
      ) {
        await invalidateKgReviewQueries(qc)
      }
    },
  })
}

// 启停文档会改变其全部 KG evidence 的实时有效性；仅服务端成功后刷新 KG。
export function useToggleImportFileDisabled() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, is_disabled }: { id: string; is_disabled: boolean }) =>
      requestJson(`/api/import/files/${encodeURIComponent(id)}/disabled`, {
        method: 'POST',
        body: { is_disabled },
      }),
    onMutate: async ({ id, is_disabled }) => {
      await qc.cancelQueries({ queryKey: ['import-files'] })
      const snapshots = qc.getQueriesData<ImportListResponse>({ queryKey: ['import-files'] })
      snapshots.forEach(([key, data]) => {
        if (!data) return
        qc.setQueryData(key, {
          ...data,
          items: data.items.map((f) => (f.id === id ? { ...f, is_disabled } : f)),
        })
      })
      return { snapshots }
    },
    onError: (_err, _vars, ctx) => {
      ctx?.snapshots.forEach(([key, data]) => qc.setQueryData(key, data))
    },
    onSuccess: async () => {
      await invalidateKgReviewQueries(qc)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['import-files'] }),
  })
}

// 启停切片会改变该切片关联 evidence 的实时有效性；失败回滚时不得刷新 KG。
export function useToggleImportChunkDisabled() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, is_disabled }: { id: string; is_disabled: boolean }) =>
      requestJson(`/api/import/chunks/${encodeURIComponent(id)}/disabled`, {
        method: 'POST',
        body: { is_disabled },
      }),
    onMutate: async ({ id, is_disabled }) => {
      const snapshots = qc.getQueriesData<ImportChunkListResponse>({
        queryKey: ['import-chunks'],
      })
      snapshots.forEach(([key, data]) => {
        if (!data) return
        qc.setQueryData(key, {
          ...data,
          items: data.items.map((c) => (c.id === id ? { ...c, is_disabled } : c)),
        })
      })
      return { snapshots }
    },
    onError: (_err, _vars, ctx) => {
      ctx?.snapshots.forEach(([key, data]) => qc.setQueryData(key, data))
    },
    onSuccess: async () => {
      await invalidateKgReviewQueries(qc)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['import-chunks'] }),
  })
}

// 修改切片正文会让旧 evidence 失效；成功后刷新切片与全部 KG 审核查询。
export function useUpdateImportChunk() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, source_text }: { id: string; source_text: string }) =>
      requestJson(`/api/import/chunks/${encodeURIComponent(id)}`, {
        method: 'POST',
        body: { source_text },
      }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['import-chunks'] })
      await invalidateKgReviewQueries(qc)
    },
  })
}

// 单切片重新生成向量。典型场景：编辑切片原文后 embedding 被标记 stale，用户单独刷新这一片。
export function useEmbedImportChunk() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['embed-import-chunk'],
    mutationFn: (id: string) =>
      requestJson<MessagesResponse & { count?: number; file_id?: string }>(
        `/api/import/chunks/${encodeURIComponent(id)}/embed`,
        { method: 'POST', body: {} },
      ),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['import-files'] })
      if (data?.file_id) {
        qc.invalidateQueries({ queryKey: ['import-chunks', data.file_id] })
        qc.invalidateQueries({ queryKey: ['import-parse-status', data.file_id] })
      } else {
        qc.invalidateQueries({ queryKey: ['import-chunks'] })
      }
    },
  })
}

export function useUploadImportFile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ file, parse }: { file: File; parse?: boolean }) => {
      const form = new FormData()
      form.append('file', file)
      const query = parse ? '' : '?parse=false'
      return requestJson<ImportFile>(`/api/import/files${query}`, {
        method: 'POST',
        body: form,
      })
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['import-files'] }),
  })
}

// 全局检测某个 fileId 上是否还有任何后台任务（embedding / 假设问题 / 解析 job 提交）在跑。
// 即使抽屉关闭再打开，只要任务没结束，按钮状态就还是 pending。
export function useFilePendingTasks(fileId: string | null) {
  const embedPending = useIsMutating({
    mutationKey: ['embed-import-file'],
    predicate: (m) => m.state.variables === fileId,
  })
  const questionsPending = useIsMutating({
    mutationKey: ['generate-questions'],
    predicate: (m) => (m.state.variables as { id?: string } | undefined)?.id === fileId,
  })
  const parsePending = useIsMutating({
    mutationKey: ['start-parse-job'],
    predicate: (m) => (m.state.variables as { id?: string } | undefined)?.id === fileId,
  })
  return {
    embed: embedPending > 0,
    questions: questionsPending > 0,
    parse: parsePending > 0,
    any: embedPending + questionsPending + parsePending > 0,
  }
}

// ───── FAQ ─────

export interface FaqListParams {
  page?: number
  pageSize?: number
  query?: string
  status?: string
  embedding?: string
}

export function useFaqs(params: FaqListParams = {}) {
  const page = params.page ?? 1
  const pageSize = params.pageSize ?? 30
  return useQuery({
    queryKey: ['faqs', { ...params, page, pageSize }],
    queryFn: () =>
      // 后端用 snake_case `page_size`，不能传驼峰。
      requestJson<FaqListResponse>('/api/faqs', {
        query: {
          query: params.query,
          status: params.status,
          embedding: params.embedding,
          page,
          page_size: pageSize,
        },
    }),
    staleTime: 10_000,
    placeholderData: (prev) => prev,
  })
}

// 读取单条 FAQ；queryFn 只允许在真实 ID 存在时发起请求。
export function useFaq(id: string | null) {
  return useQuery({
    queryKey: ['faq', id],
    queryFn: () => {
      if (!id) throw new Error('FAQ id is required')
      return requestJson<Faq>(`/api/faqs/${encodeURIComponent(id)}`)
    },
    enabled: !!id,
  })
}

// 保存 FAQ 正文或状态会改变 KG evidence 实时有效性；成功后统一刷新 FAQ 与 KG。
export function useSaveFaq() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: Partial<Faq>) =>
      requestJson<Faq>('/api/faqs', { method: 'POST', body: payload }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['faqs'] })
      await invalidateKgReviewQueries(qc)
    },
  })
}

export function useEmbedFaq() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['embed-faq'],
    mutationFn: (id: string) =>
      requestJson(`/api/faqs/${encodeURIComponent(id)}/embed`, {
        method: 'POST',
        body: {},
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['faqs'] }),
  })
}

// 批量为所有「非绿」(pending/stale/failed) FAQ 生成 embedding；后端按候选表一次处理（≤200 条）。
export function useEmbedPendingFaqs() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['embed-pending-faqs'],
    mutationFn: (limit: number) =>
      requestJson<{ count?: number; items?: unknown[] }>('/api/faqs/embed-pending', {
        method: 'POST',
        body: { limit },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['faqs'] }),
  })
}

export interface OptimizeResponse {
  question?: string
  answer?: string
  tags?: string[]
  question_variants?: string[]
  reasoning?: string
  [key: string]: unknown
}

export function useOptimizeFaq() {
  return useMutation({
    mutationKey: ['optimize-faq'],
    mutationFn: (payload: { question: string; answer: string }) =>
      requestJson<OptimizeResponse>('/api/ai/optimize', {
        method: 'POST',
        body: payload,
      }),
  })
}

// ───── Assistant ─────

// 拉取后端 settings 快照，作为会话级供应商表单的"默认值"。
export function useAssistantDefaults() {
  return useQuery({
    queryKey: ['assistant-defaults'],
    queryFn: () => requestJson<AssistantSettingsSnapshot>('/api/settings'),
    staleTime: 60_000,
  })
}

// 拉取全局设置页快照；敏感字段由后端脱敏，前端只展示摘要和 configured 状态。
export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: () => requestJson<SettingsSnapshot>('/api/settings'),
    staleTime: 30_000,
  })
}

// 保存全局设置；成功后同时刷新设置页和智能问答默认配置缓存。
export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: Record<string, string | number | boolean>) =>
      requestJson<SettingsSnapshot>('/api/settings', {
        method: 'POST',
        body: payload,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['settings'] })
      qc.invalidateQueries({ queryKey: ['assistant-defaults'] })
    },
  })
}

export function useProbeChatProvider() {
  return useMutation({
    mutationKey: ['probe-chat-provider'],
    mutationFn: (body: { chat_base_url: string; chat_model: string; chat_api_key?: string }) =>
      requestJson<ProviderProbeResponse>('/api/assistant/probe', {
        method: 'POST',
        body,
      }),
  })
}

export function useListChatProviderModels() {
  return useMutation({
    mutationKey: ['list-chat-provider-models'],
    mutationFn: (body: { chat_base_url: string; chat_api_key?: string }) =>
      requestJson<ProviderModelsResponse>('/api/assistant/models', {
        method: 'POST',
        body,
      }),
  })
}

// ───── Retrieval Evaluation ─────

export interface RetrievalEvalCaseListParams {
  status?: string
  limit?: number
  offset?: number
}

// 拉取评测用例列表；queryKey 必须包含筛选参数，避免状态筛选复用旧缓存。
export function useRetrievalEvalCases(params: RetrievalEvalCaseListParams = {}) {
  return useQuery({
    queryKey: ['retrieval-eval-cases', params],
    queryFn: () =>
      requestJson<RetrievalEvalCaseListResponse>('/api/retrieval/eval-cases', {
        query: {
          status: params.status,
          limit: params.limit ?? 100,
          offset: params.offset ?? 0,
        },
      }),
    staleTime: 10_000,
  })
}

// 保存评测用例基础字段；运行快照只由列表接口的 latest_runs 返回。
export function useSaveRetrievalEvalCase() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: Partial<RetrievalEvalCaseRecord>) =>
      requestJson<RetrievalEvalCaseRecord>('/api/retrieval/eval-cases', {
        method: 'POST',
        body: payload,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['retrieval-eval-cases'] })
    },
  })
}

// 运行单条评测；成功后刷新用例列表，同时调用方可用返回值即时更新详情区。
export function useRunRetrievalEvalCase() {
  const qc = useQueryClient()
  return useMutation({
    mutationKey: ['run-retrieval-eval-case'],
    mutationFn: ({
      caseId,
      payload,
    }: {
      caseId: string
      payload: RetrievalEvalRunPayload
    }) =>
      requestJson<RetrievalEvalRun>(
        `/api/retrieval/eval-cases/${encodeURIComponent(caseId)}/run`,
        { method: 'POST', body: payload },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['retrieval-eval-cases'] })
    },
  })
}

// 拉取别名词典；当前后端只返回启用词条，用于页面维护和关键词扩展展示。
export function useRetrievalAliases() {
  return useQuery({
    queryKey: ['retrieval-aliases'],
    queryFn: () => requestJson<RetrievalAliasListResponse>('/api/retrieval/aliases'),
    staleTime: 10_000,
  })
}

// 保存别名词条；成功后刷新词典列表，后续评测运行会读取最新启用别名。
export function useSaveRetrievalAlias() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (payload: Partial<RetrievalAlias>) =>
      requestJson<RetrievalAlias>('/api/retrieval/aliases', {
        method: 'POST',
        body: payload,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['retrieval-aliases'] })
    },
  })
}

// ───── Knowledge Graph ─────

// 统一刷新 KG 审核读模型；三个 key 必须并行完成失效，调用者才能结束成功回调。
export async function invalidateKgReviewQueries(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ['kg-entities'] }),
    queryClient.invalidateQueries({ queryKey: ['kg-relations'] }),
    queryClient.invalidateQueries({ queryKey: ['kg-subgraph'] }),
  ])
}

export interface KgEntityListParams {
  status?: string
  entity_type?: string
  limit?: number
  offset?: number
}

// 拉取 KG 实体候选列表；queryKey 覆盖筛选和分页，避免审核状态切换后复用旧缓存。
export function useKgEntities(params: KgEntityListParams = {}) {
  return useQuery({
    queryKey: ['kg-entities', params],
    queryFn: () =>
      requestJson<KgListResponse<KgEntity>>('/api/kg/entities', {
        query: {
          status: params.status,
          entity_type: params.entity_type,
          limit: params.limit ?? 50,
          offset: params.offset ?? 0,
        },
      }),
    staleTime: 10_000,
    refetchOnMount: 'always',
  })
}

export interface KgRelationListParams {
  status?: string
  relation_type?: string
  limit?: number
  offset?: number
}

// 拉取 KG 关系列表；后端已带头尾实体和证据，前端只做展示和轻量筛选。
export function useKgRelations(params: KgRelationListParams = {}) {
  return useQuery({
    queryKey: ['kg-relations', params],
    queryFn: () =>
      requestJson<KgListResponse<KgRelation>>('/api/kg/relations', {
        query: {
          status: params.status,
          relation_type: params.relation_type,
          limit: params.limit ?? 50,
          offset: params.offset ?? 0,
        },
      }),
    staleTime: 10_000,
    refetchOnMount: 'always',
  })
}

// 确认实体并触发后端投影；成功后统一刷新实体、关系和局部子图审核读模型。
export function useConfirmKgEntity() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      id,
      expectedRevision,
    }: {
      id: string
      expectedRevision: number
    }) =>
      requestJson<{ item: KgEntity }>(`/api/kg/entities/${encodeURIComponent(id)}/confirm`, {
        method: 'POST',
        body: { expected_revision: expectedRevision },
      }),
    onSuccess: async () => {
      await invalidateKgReviewQueries(qc)
    },
  })
}

// 确认关系并触发后端投影；成功后复用同一组 KG 审核缓存失效规则。
export function useConfirmKgRelation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      id,
      expectedRevision,
    }: {
      id: string
      expectedRevision: number
    }) =>
      requestJson<{ item: KgRelation }>(`/api/kg/relations/${encodeURIComponent(id)}/confirm`, {
        method: 'POST',
        body: { expected_revision: expectedRevision },
      }),
    onSuccess: async () => {
      await invalidateKgReviewQueries(qc)
    },
  })
}

// 更新实体审核状态；端点变化可能联动关系，因此统一刷新三个 KG 查询族。
export function useSetKgEntityStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      requestJson<{ item: KgEntity }>(`/api/kg/entities/${encodeURIComponent(id)}/status`, {
        method: 'POST',
        body: { status },
      }),
    onSuccess: async () => {
      await invalidateKgReviewQueries(qc)
    },
  })
}

// 更新关系审核状态；成功后复用统一 KG 审核缓存失效规则。
export function useSetKgRelationStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      requestJson<{ item: KgRelation }>(`/api/kg/relations/${encodeURIComponent(id)}/status`, {
        method: 'POST',
        body: { status },
      }),
    onSuccess: async () => {
      await invalidateKgReviewQueries(qc)
    },
  })
}

// 读取 usable 局部子图；响应明确区分 isolated 与 connected，不发送可切换 status。
export function useKgSubgraph(centerEntityId: string | null, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ['kg-subgraph', centerEntityId],
    queryFn: () =>
      requestJson<KgSubgraphResponse>('/api/kg/subgraph', {
        query: {
          center_entity_id: centerEntityId,
          hops: 1,
          limit: 40,
        },
      }),
    enabled: !!centerEntityId && (options?.enabled ?? true),
    staleTime: 10_000,
    refetchOnMount: 'always',
  })
}

// 创建并轮询 KG 抽取任务；completed 后统一刷新全部 KG 审核查询，failed 直接抛错。
export function useCreateKgExtractionJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (payload: { source_id: string; source_type: 'faq' | 'document_chunk' }) => {
      const queued = await requestJson<KgExtractionJob>('/api/kg/extraction-jobs', {
        method: 'POST',
        body: payload,
      })
      return waitForKgExtractionJob(queued, (jobId) =>
        requestJson<KgExtractionJob>(`/api/kg/extraction-jobs/${encodeURIComponent(jobId)}`),
      )
    },
    onSuccess: async () => {
      await invalidateKgReviewQueries(qc)
    },
  })
}
