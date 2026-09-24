// 前端 TS 类型：从 admin_server.py 的 normalize_*_payload / DB rows 字段提炼。
// 既有迁移页面只列出当前使用字段；检索评测与 KG 的 0→1 DTO 在各自区段保持封闭契约。

export type DocumentChunkerType = 'naive' | 'manual' | 'qa' | 'table'

export interface ImportFile {
  id: string
  original_name: string
  file_type: string
  parser: string
  chunker_type: DocumentChunkerType
  status:
    | 'pending'
    | 'processing'
    | 'needs_review'
    | 'completed'
    | 'failed'
    | string
  message_count: number
  chunk_count: number
  candidate_count: number
  error: string | null
  is_disabled: boolean
  parse_progress: Record<string, unknown>
  embedding_summary?: EmbeddingSummary
  created_at: string
  updated_at: string
  [key: string]: unknown
}

export interface ImportChunk {
  id: string
  file_id: string
  chunk_index: number
  source_text: string
  parent_content?: string
  section_path: string[]
  page_start: number | null
  page_end: number | null
  block_type: string | null
  source_offsets: Record<string, unknown>
  source_blocks: SourceBlock[]
  status: string
  is_disabled: boolean
  questions: string[]
  questions_status: 'pending' | 'ready' | 'failed' | 'skipped' | string
  questions_model: string | null
  questions_updated_at: string | null
  questions_error: string | null
  embedding_status: 'pending' | 'ready' | 'stale' | 'failed' | string
  keywords?: string[]
  [key: string]: unknown
}

export interface SourceBlock {
  text: string
  block_type: string
  page_number?: number | null
  section_title?: string | null
  evidence?: {
    asset_paths?: {
      img_path?: string
      table_img_path?: string
      equation_img_path?: string
    }
    table_html?: string
    [key: string]: unknown
  }
  html?: string
  [key: string]: unknown
}

export interface EmbeddingSummary {
  status: string
  total_chunks: number
  knowledge_count: number
  ready_count: number
  stale_count: number
  failed_count: number
  pending_count: number
  missing_count: number
}

export interface ImportListResponse {
  items: ImportFile[]
  status_counts?: Record<string, number>
  total?: number
}

export interface ImportChunkListResponse {
  items: ImportChunk[]
  file?: ImportFile
}

export interface MessagesResponse {
  messages?: string[]
  [key: string]: unknown
}

// FAQ

export interface Faq {
  id: string
  question: string
  answer: string
  question_variants: string[]
  tags: string[]
  category: string | null
  status: string
  confidence: string | null
  embedding_status: string
  embedding_model: string | null
  embedding_dimensions: number | null
  embedding_updated_at: string | null
  created_at: string
  updated_at: string
  [key: string]: unknown
}

export interface FaqListResponse {
  items: Faq[]
  total: number
  status_counts?: Record<string, number>
}

// Assistant

export interface AssistantSource {
  id: string
  source_id: string
  source_type: 'document' | 'faq'
  source_chunk_id: string | null
  parent_chunk_id: string | null
  chunk_level: string
  source_title: string | null
  section_path: string[]
  page_start: number | null
  page_end: number | null
  block_type: string | null
  source_offsets: Record<string, unknown>
  content: string
  question: string
  answer: string
  category: string | null
  tags: string[]
  source_date: string | null
  confidence: string | null
  status: string
  score: number
  retrieval_channels?: string[]
  fused_score?: number | null
  vector_score?: number | null
  keyword_score?: number | null
  metadata: Record<string, unknown>
}

export interface AssistantStreamPayload {
  question: string
  conversation_id?: string
  system_prompt?: string
  conversation_context?: AssistantConversationContext
  // 单次请求覆盖供应商；三件套齐了后端会临时构造 ChatClient，否则走全局默认。
  chat_base_url?: string
  chat_api_key?: string
  chat_model?: string
}

export interface AssistantConversationContextMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface AssistantConversationContext {
  summary?: string
  recent_messages: AssistantConversationContextMessage[]
}

export interface SettingsSnapshot {
  database_url?: string
  database_url_configured?: boolean
  chat_base_url?: string
  chat_api_key?: string
  chat_api_key_configured?: boolean
  chat_model?: string
  embedding_base_url?: string
  embedding_api_key?: string
  embedding_api_key_configured?: boolean
  embedding_model?: string
  embedding_dimensions?: number
  wechat_token_file?: string
  wechat_message_chunk_size?: number
  rag_top_k?: number
  rag_min_score?: number
  upload_dir?: string
  mineru_api_token?: string
  mineru_api_token_configured?: boolean
  mineru_parse_timeout_seconds?: number
  mineru_use_kb_packager?: boolean
  document_chunk_token_num?: number
  document_chunker_type?: string
  document_chunk_delimiter?: string
  document_chunk_overlap_percent?: number
  document_children_delimiter?: string
  document_table_context_size?: number
  document_image_context_size?: number
  rerank_base_url?: string
  rerank_api_key?: string
  rerank_api_key_configured?: boolean
  rerank_model?: string
  rerank_input_size?: number
  [key: string]: unknown
}

export type AssistantSettingsSnapshot = SettingsSnapshot

export interface ProviderProbeResponse {
  ok: boolean
  latency_ms?: number
  model?: string
  sample?: string
  error?: string
}

export interface ProviderModel {
  id: string
  owned_by?: string
}

export interface ProviderModelsResponse {
  ok: boolean
  items: ProviderModel[]
  error?: string
}

// Retrieval Evaluation

export interface RetrievalEvalMetrics {
  case_count?: number
  recall_at_k?: number
  mrr?: number
  hit_rate_at_1?: number
}

export type RetrievalEvalRunPayload = Record<string, never> | { use_kg: true }

export interface RetrievalKgFactMatch {
  fact_chunk_id: string
  fact_id: string
  fact_type: 'kg_entity' | 'kg_relation'
  fact_rank: number
  fact_score: number
}

export interface RetrievalKgFactAnalysis extends RetrievalKgFactMatch {
  expanded_candidate_ids: string[]
}

export interface RetrievalEvalAnalysis {
  contract_version: 2
  intent?: string
  confidence?: string
  query?: string
  query_rewrite?: string
  preferred_sources?: string[]
  query_terms?: string[]
  vector_count?: number
  keyword_count?: number
  use_kg: boolean
  kg_fact_count: number
  kg_expanded_candidate_count: number
  kg_facts: RetrievalKgFactAnalysis[]
  reason?: string
}

export type RetrievalEvalStrategyId =
  | 'retrieval_hybrid_v1'
  | 'retrieval_hybrid_v1_kg_debug'

export interface RetrievalEvalItem {
  id: string
  source_id: string
  source_type: 'faq' | 'document'
  source_chunk_id: string | null
  parent_chunk_id: string | null
  chunk_level: string
  source_title: string | null
  section_path: string[]
  page_start: number | null
  page_end: number | null
  block_type: string | null
  content: string
  channels: string[]
  fused_score: number
  vector_score: number | null
  keyword_score: number | null
  kg_score: number | null
  kg_matches: RetrievalKgFactMatch[]
}

export interface RetrievalEvalRun {
  id: string
  case_id: string
  strategy: RetrievalEvalStrategyId
  retrieved_items: RetrievalEvalItem[]
  metrics: RetrievalEvalMetrics
  analysis: RetrievalEvalAnalysis
  created_at?: string
}

export interface RetrievalEvalCaseRecord {
  id: string
  question: string
  intent: string | null
  expected_source_ids: string[]
  expected_chunk_ids: string[]
  tags: string[]
  note: string | null
  status: string
  created_at?: string
  updated_at?: string
}

export interface RetrievalEvalCase extends RetrievalEvalCaseRecord {
  latest_runs: RetrievalEvalRun[]
}

export interface RetrievalEvalCaseListResponse {
  items: RetrievalEvalCase[]
  total: number
}

export interface RetrievalAlias {
  id: string
  canonical: string
  aliases: string[]
  tags: string[]
  status: string
  created_at?: string
  updated_at?: string
}

export interface RetrievalAliasListResponse {
  items: RetrievalAlias[]
  total: number
}

// Knowledge Graph

export interface KgEvidence {
  id: string
  source_type: string
  source_id: string
  source_chunk_id: string | null
  source_title: string | null
  section_path: string[] | null
  page_start: number | null
  page_end: number | null
  excerpt: string
  is_valid: boolean
}

export interface KgEntity {
  id: string
  name: string
  entity_type: string
  aliases: string[]
  description: string | null
  status: string
  review_revision: number
  confidence: number | null
  evidence: KgEvidence[]
  has_valid_evidence: boolean
  source_count: number
  created_at?: string
  updated_at?: string
}

export interface KgRelation {
  id: string
  head_entity_id: string
  relation_type: string
  tail_entity_id: string
  description: string | null
  status: string
  review_revision: number
  confidence: number | null
  head_entity_name: string
  head_entity_type: string
  head_entity_status: string
  tail_entity_name: string
  tail_entity_type: string
  tail_entity_status: string
  evidence: KgEvidence[]
  has_valid_evidence: boolean
  evidence_count: number
  created_at?: string
  updated_at?: string
}

export interface KgListResponse<T> {
  items: T[]
  total: number
}

export interface KgSubgraphNode {
  id: string
  name: string
  entity_type: string
  description?: string | null
  status: 'usable'
  confidence?: number | null
}

export interface KgSubgraphEdge {
  id: string
  source: string
  target: string
  relation_type: string
  description?: string | null
  confidence?: number | null
  status: 'usable'
  evidence_count: number
}

export interface KgSubgraphResponse {
  state: 'isolated' | 'connected'
  center: KgSubgraphNode
  nodes: KgSubgraphNode[]
  edges: KgSubgraphEdge[]
}

export interface KgExtractionJob {
  id: string
  source_type: 'faq' | 'document_chunk'
  source_id: string
  source_chunk_id?: string | null
  status: 'queued' | 'processing' | 'completed' | 'failed'
  entity_count: number
  relation_count: number
  evidence_count: number
  model?: string | null
  error?: string | null
  created_at?: string
  updated_at?: string
}
