CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS faq_documents (
    id TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    source_file TEXT,
    source_group TEXT,
    source_date TEXT,
    category TEXT,
    question TEXT NOT NULL,
    question_variants JSONB NOT NULL DEFAULT '[]'::jsonb,
    answer TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence TEXT NOT NULL,
    status TEXT NOT NULL,
    sensitivity TEXT,
    embedding_text TEXT NOT NULL,
    embedding vector(1024),
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    embedding_updated_at TIMESTAMPTZ,
    embedding_error TEXT,
    content_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE faq_documents
    ALTER COLUMN embedding DROP NOT NULL;

ALTER TABLE faq_documents
    ADD COLUMN IF NOT EXISTS embedding_status TEXT NOT NULL DEFAULT 'pending';

ALTER TABLE faq_documents
    ADD COLUMN IF NOT EXISTS embedding_model TEXT;

ALTER TABLE faq_documents
    ADD COLUMN IF NOT EXISTS embedding_dimensions INTEGER;

ALTER TABLE faq_documents
    ADD COLUMN IF NOT EXISTS embedding_updated_at TIMESTAMPTZ;

ALTER TABLE faq_documents
    ADD COLUMN IF NOT EXISTS embedding_error TEXT;

ALTER TABLE faq_documents
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

-- FAQ 用 status 表达禁用(usable/needs_review/disabled)，不再要正交的 is_disabled 列；历史库若已加则移除。
ALTER TABLE faq_documents
    DROP COLUMN IF EXISTS is_disabled;

-- 历史遗留的非常规状态(如 product_request / draft / archived)归一到三态：非 usable/disabled 的都视作待复核。
UPDATE faq_documents
SET status = 'needs_review'
WHERE status NOT IN ('usable', 'needs_review', 'disabled');

UPDATE faq_documents
SET embedding_status = 'ready'
WHERE embedding IS NOT NULL
  AND embedding_status = 'pending';

-- 缺少当前内容指纹的向量不属于现行契约，必须通过正式 embedding 重建。
UPDATE faq_documents
SET embedding_status = 'stale',
    embedding_error = NULL,
    updated_at = now()
WHERE embedding_status = 'ready'
  AND content_hash IS NULL;

CREATE INDEX IF NOT EXISTS faq_documents_status_confidence_idx
    ON faq_documents (status, confidence);

CREATE INDEX IF NOT EXISTS faq_documents_embedding_status_idx
    ON faq_documents (embedding_status);

CREATE INDEX IF NOT EXISTS faq_documents_category_idx
    ON faq_documents (category);

CREATE INDEX IF NOT EXISTS faq_documents_embedding_idx
    ON faq_documents USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_chunk_id TEXT,
    parent_chunk_id TEXT,
    chunk_level TEXT NOT NULL DEFAULT 'chunk',
    source_title TEXT,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
    page_start INTEGER,
    page_end INTEGER,
    block_type TEXT,
    source_offsets JSONB NOT NULL DEFAULT '{}'::jsonb,
    content TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    search_text TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence TEXT,
    status TEXT NOT NULL DEFAULT 'needs_review',
    embedding vector(1024),
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    embedding_updated_at TIMESTAMPTZ,
    embedding_error TEXT,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, chunk_index)
);

ALTER TABLE knowledge_chunks
    ADD COLUMN IF NOT EXISTS parent_chunk_id TEXT,
    ADD COLUMN IF NOT EXISTS chunk_level TEXT NOT NULL DEFAULT 'chunk',
    ADD COLUMN IF NOT EXISTS section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS page_start INTEGER,
    ADD COLUMN IF NOT EXISTS page_end INTEGER,
    ADD COLUMN IF NOT EXISTS block_type TEXT,
    ADD COLUMN IF NOT EXISTS source_offsets JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS knowledge_chunks_source_idx
    ON knowledge_chunks (source_type, source_id);

CREATE INDEX IF NOT EXISTS knowledge_chunks_parent_idx
    ON knowledge_chunks (parent_chunk_id);

CREATE INDEX IF NOT EXISTS knowledge_chunks_status_source_idx
    ON knowledge_chunks (status, source_type);

CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_status_idx
    ON knowledge_chunks (embedding_status);

CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS knowledge_chunks_metadata_idx
    ON knowledge_chunks USING gin (metadata);

CREATE INDEX IF NOT EXISTS knowledge_chunks_section_path_idx
    ON knowledge_chunks USING gin (section_path);

CREATE INDEX IF NOT EXISTS knowledge_chunks_search_idx
    ON knowledge_chunks USING gin (to_tsvector('simple', search_text));

CREATE TABLE IF NOT EXISTS kg_entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN (
            'product_platform_module',
            'feature_ui_action',
            'error_symptom',
            'process_task_object',
            'role_permission_channel',
            'condition_policy'
        )
    ),
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'needs_review' CHECK (status IN ('needs_review', 'usable', 'disabled')),
    review_revision BIGINT NOT NULL DEFAULT 1,
    confidence DOUBLE PRECISION,
    source_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE kg_entities
    ADD COLUMN IF NOT EXISTS review_revision BIGINT NOT NULL DEFAULT 1;

CREATE UNIQUE INDEX IF NOT EXISTS kg_entities_name_type_idx
    ON kg_entities (lower(name), entity_type);

CREATE INDEX IF NOT EXISTS kg_entities_status_type_idx
    ON kg_entities (status, entity_type, updated_at DESC);

CREATE TABLE IF NOT EXISTS kg_relations (
    id TEXT PRIMARY KEY,
    head_entity_id TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN (
            'belongs_to',
            'requires',
            'causes',
            'resolves_by',
            'blocked_by',
            'available_for',
            'escalate_when'
        )
    ),
    tail_entity_id TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'needs_review' CHECK (status IN ('needs_review', 'usable', 'disabled')),
    review_revision BIGINT NOT NULL DEFAULT 1,
    confidence DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (head_entity_id, relation_type, tail_entity_id)
);

ALTER TABLE kg_relations
    ADD COLUMN IF NOT EXISTS review_revision BIGINT NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS kg_relations_status_type_idx
    ON kg_relations (status, relation_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS kg_relations_head_idx
    ON kg_relations (head_entity_id, status);

CREATE INDEX IF NOT EXISTS kg_relations_tail_idx
    ON kg_relations (tail_entity_id, status);

CREATE TABLE IF NOT EXISTS kg_evidence (
    id TEXT PRIMARY KEY,
    entity_id TEXT REFERENCES kg_entities(id) ON DELETE CASCADE,
    relation_id TEXT REFERENCES kg_relations(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_chunk_id TEXT,
    source_title TEXT,
    section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
    page_start INTEGER,
    page_end INTEGER,
    excerpt TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT kg_evidence_owner_check CHECK (
        (entity_id IS NOT NULL AND relation_id IS NULL)
        OR (entity_id IS NULL AND relation_id IS NOT NULL)
    ),
    CONSTRAINT kg_evidence_char_offsets_check CHECK (
        char_start >= 0 AND char_end > char_start
    ),
    CONSTRAINT kg_evidence_source_locator_check CHECK (
        (source_type = 'faq' AND source_chunk_id IS NULL)
        OR (source_type = 'document' AND source_chunk_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS kg_evidence_entity_idx
    ON kg_evidence (entity_id);

CREATE INDEX IF NOT EXISTS kg_evidence_relation_idx
    ON kg_evidence (relation_id);

CREATE INDEX IF NOT EXISTS kg_evidence_source_idx
    ON kg_evidence (source_type, source_id, source_chunk_id);

CREATE TABLE IF NOT EXISTS import_files (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    parser TEXT NOT NULL,
    chunker_type TEXT NOT NULL DEFAULT 'naive',
    status TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE import_files
    DROP COLUMN IF EXISTS parse_batch_id,
    DROP COLUMN IF EXISTS parse_file_name,
    DROP COLUMN IF EXISTS parse_progress,
    ADD COLUMN IF NOT EXISTS chunker_type TEXT NOT NULL DEFAULT 'naive',
    ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS import_parse_jobs (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES import_files(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'submitting', 'polling', 'finalizing', 'completed', 'failed')
    ),
    chunker_type TEXT NOT NULL CHECK (
        chunker_type IN ('naive', 'manual', 'qa', 'table')
    ),
    input_fingerprint TEXT NOT NULL,
    provider_batch_id TEXT,
    provider_file_name TEXT,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT import_parse_jobs_progress_object_check
        CHECK (jsonb_typeof(progress) = 'object'),
    CONSTRAINT import_parse_jobs_lease_pair_check
        CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS import_parse_jobs_one_active_per_file_idx
    ON import_parse_jobs(file_id)
    WHERE status IN ('queued', 'submitting', 'polling', 'finalizing');

CREATE TABLE IF NOT EXISTS import_chunks (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES import_files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
    page_start INTEGER,
    page_end INTEGER,
    block_type TEXT,
    source_offsets JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_blocks JSONB NOT NULL DEFAULT '[]'::jsonb,
    children_delimiter TEXT NOT NULL DEFAULT '',
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    message_count INTEGER NOT NULL DEFAULT 0,
    keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    candidate_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE import_chunks
    DROP COLUMN IF EXISTS parent_chunk_id,
    DROP COLUMN IF EXISTS chunk_level,
    ADD COLUMN IF NOT EXISTS section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS page_start INTEGER,
    ADD COLUMN IF NOT EXISTS page_end INTEGER,
    ADD COLUMN IF NOT EXISTS block_type TEXT,
    ADD COLUMN IF NOT EXISTS source_offsets JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS source_blocks JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS children_delimiter TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS questions_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS questions_model TEXT,
    ADD COLUMN IF NOT EXISTS questions_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS questions_error TEXT;

CREATE TABLE IF NOT EXISTS cyclops_schema_migrations (
    id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 串行领取一次性修复；marker 写入与所有 owner/projection 更新处于同一事务。
LOCK TABLE cyclops_schema_migrations IN EXCLUSIVE MODE;

DO $kg_live_state_repair$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM cyclops_schema_migrations
        WHERE id = '20260715_kg_live_state_repair_v1'
    ) THEN
-- KG owner 按实时来源门禁修复历史计数和审核终态；首次修复递增 revision。
WITH live_entity_counts AS (
    SELECT
        entity.id,
        (
            COUNT(DISTINCT (
                valid_ev.source_type,
                valid_ev.source_id,
                COALESCE(valid_ev.source_chunk_id, '')
            )) FILTER (WHERE
                (valid_ev.source_type = 'faq' AND faq.status = 'usable')
                OR (
                    valid_ev.source_type = 'document'
                    AND import_file.id IS NOT NULL
                    AND import_chunk.id IS NOT NULL
                    AND import_file.is_disabled = false
                    AND import_chunk.is_disabled = false
                )
            )
        )::integer AS source_count
    FROM kg_entities AS entity
    LEFT JOIN kg_evidence AS valid_ev ON valid_ev.entity_id = entity.id
    LEFT JOIN faq_documents AS faq
      ON valid_ev.source_type = 'faq'
     AND faq.id = valid_ev.source_id
    LEFT JOIN import_files AS import_file
      ON valid_ev.source_type = 'document'
     AND import_file.id = valid_ev.source_id
    LEFT JOIN import_chunks AS import_chunk
      ON valid_ev.source_type = 'document'
     AND import_chunk.id = valid_ev.source_chunk_id
     AND import_chunk.file_id = import_file.id
    GROUP BY entity.id
), expected_entity_state AS (
    SELECT
        entity.id,
        live.source_count,
        CASE
            WHEN live.source_count = 0 THEN 'disabled'
            ELSE 'needs_review'
        END AS status
    FROM kg_entities AS entity
    JOIN live_entity_counts AS live ON live.id = entity.id
)
UPDATE kg_entities AS entity
SET source_count = expected.source_count,
    status = expected.status,
    review_revision = entity.review_revision + 1,
    updated_at = now()
FROM expected_entity_state AS expected
WHERE entity.id = expected.id
  AND (
        entity.source_count IS DISTINCT FROM expected.source_count
        OR entity.status IS DISTINCT FROM expected.status
      );

WITH live_relation_counts AS (
    SELECT
        relation.id,
        (
            COUNT(valid_ev.id) FILTER (WHERE
                (valid_ev.source_type = 'faq' AND faq.status = 'usable')
                OR (
                    valid_ev.source_type = 'document'
                    AND import_file.id IS NOT NULL
                    AND import_chunk.id IS NOT NULL
                    AND import_file.is_disabled = false
                    AND import_chunk.is_disabled = false
                )
            )
        )::integer AS evidence_count
    FROM kg_relations AS relation
    LEFT JOIN kg_evidence AS valid_ev ON valid_ev.relation_id = relation.id
    LEFT JOIN faq_documents AS faq
      ON valid_ev.source_type = 'faq'
     AND faq.id = valid_ev.source_id
    LEFT JOIN import_files AS import_file
      ON valid_ev.source_type = 'document'
     AND import_file.id = valid_ev.source_id
    LEFT JOIN import_chunks AS import_chunk
      ON valid_ev.source_type = 'document'
     AND import_chunk.id = valid_ev.source_chunk_id
     AND import_chunk.file_id = import_file.id
    GROUP BY relation.id
), expected_relation_state AS (
    SELECT
        relation.id,
        CASE
            WHEN live.evidence_count = 0 THEN 'disabled'
            ELSE 'needs_review'
        END AS status
    FROM kg_relations AS relation
    JOIN live_relation_counts AS live ON live.id = relation.id
)
UPDATE kg_relations AS relation
SET status = expected.status,
    review_revision = relation.review_revision + 1,
    updated_at = now()
FROM expected_relation_state AS expected
WHERE relation.id = expected.id
  AND relation.status IS DISTINCT FROM expected.status;

-- 当前 synthetic projection 必须逐行镜像 canonical owner，不能从来源事件猜统一状态。
UPDATE knowledge_chunks AS projection
SET status = entity.status,
    updated_at = now()
FROM kg_entities AS entity
WHERE projection.source_type = 'kg_entity'
  AND projection.source_id = entity.id
  AND projection.status IS DISTINCT FROM entity.status;

UPDATE knowledge_chunks AS projection
SET status = relation.status,
    updated_at = now()
FROM kg_relations AS relation
WHERE projection.source_type = 'kg_relation'
  AND projection.source_id = relation.id
  AND projection.status IS DISTINCT FROM relation.status;

        INSERT INTO cyclops_schema_migrations (id)
        VALUES ('20260715_kg_live_state_repair_v1');
    END IF;
END
$kg_live_state_repair$;

CREATE TABLE IF NOT EXISTS import_candidates (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES import_files(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES import_chunks(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    similar_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    category TEXT,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence TEXT NOT NULL DEFAULT 'medium',
    internal_note TEXT,
    source_excerpt TEXT NOT NULL,
    duplicate_level TEXT NOT NULL DEFAULT 'none',
    duplicate_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    duplicate_target_id TEXT,
    duplicate_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    saved_faq_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE import_candidates
    ADD COLUMN IF NOT EXISTS duplicate_level TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS duplicate_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS duplicate_target_id TEXT,
    ADD COLUMN IF NOT EXISTS duplicate_reason TEXT;

CREATE TABLE IF NOT EXISTS import_generation_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',
    total_count INTEGER NOT NULL DEFAULT 0,
    queued_count INTEGER NOT NULL DEFAULT 0,
    processing_count INTEGER NOT NULL DEFAULT 0,
    generated_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS import_generation_job_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES import_generation_jobs(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES import_chunks(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    reason TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS import_files_status_idx
    ON import_files (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS import_chunks_file_idx
    ON import_chunks (file_id, chunk_index);

CREATE INDEX IF NOT EXISTS import_candidates_chunk_idx
    ON import_candidates (chunk_id, status);

CREATE INDEX IF NOT EXISTS import_generation_job_items_chunk_status_idx
    ON import_generation_job_items (chunk_id, status);

CREATE TABLE IF NOT EXISTS retrieval_eval_cases (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    intent TEXT,
    expected_source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retrieval_eval_runs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES retrieval_eval_cases(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    retrieved_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    analysis JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS retrieval_eval_cases_status_idx
    ON retrieval_eval_cases (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS retrieval_eval_runs_case_idx
    ON retrieval_eval_runs (case_id, created_at DESC);

CREATE TABLE IF NOT EXISTS retrieval_aliases (
    id TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS retrieval_aliases_status_idx
    ON retrieval_aliases (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS query_analytics_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    query TEXT NOT NULL,
    intent TEXT,
    retrieved_chunk_ids TEXT[] NOT NULL DEFAULT '{}',
    top_score DOUBLE PRECISION,
    hit_count INT NOT NULL DEFAULT 0,
    rerank_used BOOLEAN NOT NULL DEFAULT false,
    latency_ms INT,
    requester_type TEXT NOT NULL DEFAULT 'unknown',
    requester_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_query_analytics_created_at
    ON query_analytics_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_query_analytics_hit_zero
    ON query_analytics_events (created_at DESC)
    WHERE hit_count = 0;

CREATE TABLE IF NOT EXISTS query_analytics_cluster_summaries (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    cluster_label TEXT NOT NULL,
    suggested_content TEXT,
    event_count INT NOT NULL,
    sample_queries TEXT[] NOT NULL DEFAULT '{}'
);

-- 文档当前只允许 parent/child 两层；旧 chunk 行是可重建派生数据，直接删除而不保留检索兼容。
DELETE FROM knowledge_chunks
WHERE source_type = 'document'
  AND chunk_level NOT IN ('parent', 'child');

-- 文档 child 的 source_chunk_id 与 metadata.chunk_id 统一指向原始 import_chunks.id。
-- 这是一次性数据校正；运行时代码只读取 canonical locator，不保留旧格式分支。
UPDATE knowledge_chunks AS child
SET source_chunk_id = parent.source_chunk_id,
    metadata = jsonb_set(
        child.metadata,
        '{chunk_id}',
        to_jsonb(parent.source_chunk_id),
        true
    ),
    embedding_status = 'stale',
    embedding_error = NULL,
    updated_at = now()
FROM knowledge_chunks AS parent
WHERE child.source_type = 'document'
  AND child.chunk_level = 'child'
  AND child.parent_chunk_id = parent.id
  AND parent.source_type = 'document'
  AND parent.chunk_level = 'parent'
  AND parent.source_chunk_id IS NOT NULL
  AND child.source_id = parent.source_id
  AND (
        child.source_chunk_id IS DISTINCT FROM parent.source_chunk_id
        OR child.metadata->>'chunk_id' IS DISTINCT FROM parent.source_chunk_id
      );

-- 只有 parent 的 ready 切片没有直接召回单元，标 stale 后由现有 embedding 流程重建 child。
UPDATE knowledge_chunks AS parent
SET embedding_status = 'stale',
    embedding_error = NULL,
    updated_at = now()
WHERE parent.source_type = 'document'
  AND parent.chunk_level = 'parent'
  AND parent.embedding_status = 'ready'
  AND NOT EXISTS (
        SELECT 1
        FROM knowledge_chunks AS child
        WHERE child.source_type = 'document'
          AND child.source_id = parent.source_id
          AND child.source_chunk_id = parent.source_chunk_id
          AND child.parent_chunk_id = parent.id
          AND child.chunk_level = 'child'
      );

-- FAQ 统一投影与实时 FAQ embedding 文本指纹不一致时，只标 stale 等待正式重建。
UPDATE knowledge_chunks AS projection
SET embedding_status = 'stale',
    embedding_error = NULL,
    updated_at = now()
FROM faq_documents AS faq
WHERE projection.source_type = 'faq'
  AND projection.source_id = faq.id
  AND projection.content_hash IS DISTINCT FROM faq.content_hash
  AND projection.embedding_status <> 'stale';

-- canonical FAQ 投影只同步非向量字段，历史回填不得改写 embedding 生命周期或正文指纹。
WITH expected_faq_projection_fields AS (
    SELECT
        faq.id AS source_id,
        faq.confidence,
        faq.status,
        jsonb_build_object(
            'category', NULLIF(btrim(COALESCE(faq.category, '')), ''),
            'question_variants', faq.question_variants,
            'evidence', faq.evidence,
            'source_file', faq.source_file,
            'source_group', faq.source_group,
            'source_date', faq.source_date
        ) AS metadata
    FROM faq_documents AS faq
)
UPDATE knowledge_chunks AS projection
SET metadata = expected.metadata,
    confidence = expected.confidence,
    status = expected.status,
    updated_at = now()
FROM expected_faq_projection_fields AS expected
WHERE projection.source_type = 'faq'
  AND projection.source_id = expected.source_id
  AND projection.chunk_index = 0
  AND projection.chunk_level = 'chunk'
  AND projection.source_chunk_id IS NULL
  AND projection.parent_chunk_id IS NULL
  AND (
        projection.metadata IS DISTINCT FROM expected.metadata
        OR projection.confidence IS DISTINCT FROM expected.confidence
        OR projection.status IS DISTINCT FROM expected.status
      );

-- ready FAQ 缺少同指纹 ready 投影时退回正式 embedding 队列；不提供运行时 backfill 第二路径。
UPDATE faq_documents AS faq
SET embedding_status = 'stale',
    embedding_error = NULL,
    updated_at = now()
WHERE faq.embedding_status = 'ready'
  AND NOT EXISTS (
        SELECT 1
        FROM knowledge_chunks AS projection
        WHERE projection.source_type = 'faq'
          AND projection.source_id = faq.id
          AND projection.content_hash = faq.content_hash
          AND projection.embedding_status = 'ready'
      );

-- 假设问题已变化的文档投影只标 stale，不原地拼接正文或重算哈希。
UPDATE knowledge_chunks AS projection
SET embedding_status = 'stale',
    embedding_error = NULL,
    updated_at = now()
FROM import_chunks AS source_chunk
WHERE projection.source_type = 'document'
  AND projection.source_id = source_chunk.file_id
  AND projection.source_chunk_id = source_chunk.id
  AND COALESCE(projection.metadata->'questions', '[]'::jsonb)
      IS DISTINCT FROM COALESCE(source_chunk.questions, '[]'::jsonb)
  AND projection.embedding_status <> 'stale';

-- v2 以前的派生指标包含 synthetic KG 候选，无法无损换算为原始证据口径，直接删除。
DELETE FROM retrieval_eval_runs
WHERE analysis->'contract_version' IS DISTINCT FROM '2'::jsonb;

-- 精确移除指向 KG 投影的期望 chunk；普通业务 ID 即使前缀相似也必须保留。
UPDATE retrieval_eval_cases AS eval_case
SET expected_chunk_ids = (
        SELECT COALESCE(jsonb_agg(entry.value ORDER BY entry.ordinality), '[]'::jsonb)
        FROM jsonb_array_elements_text(eval_case.expected_chunk_ids)
            WITH ORDINALITY AS entry(value, ordinality)
        WHERE NOT EXISTS (
            SELECT 1
            FROM knowledge_chunks AS knowledge_chunk
            WHERE knowledge_chunk.id = entry.value
              AND knowledge_chunk.source_type IN ('kg_entity', 'kg_relation')
        )
    ),
    updated_at = now()
WHERE EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(eval_case.expected_chunk_ids) AS entry(value)
    JOIN knowledge_chunks AS knowledge_chunk
      ON knowledge_chunk.id = entry.value
     AND knowledge_chunk.source_type IN ('kg_entity', 'kg_relation')
);

-- 精确移除指向 KG 审核行的期望 source，避免旧 synthetic 来源继续参与人工标注。
UPDATE retrieval_eval_cases AS eval_case
SET expected_source_ids = (
        SELECT COALESCE(jsonb_agg(entry.value ORDER BY entry.ordinality), '[]'::jsonb)
        FROM jsonb_array_elements_text(eval_case.expected_source_ids)
            WITH ORDINALITY AS entry(value, ordinality)
        WHERE NOT EXISTS (
            SELECT 1
            FROM kg_entities AS entity
            WHERE entity.id = entry.value
        )
          AND NOT EXISTS (
            SELECT 1
            FROM kg_relations AS relation
            WHERE relation.id = entry.value
        )
    ),
    updated_at = now()
WHERE EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(eval_case.expected_source_ids) AS entry(value)
        JOIN kg_entities AS entity ON entity.id = entry.value
    )
   OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(eval_case.expected_source_ids) AS entry(value)
        JOIN kg_relations AS relation ON relation.id = entry.value
    );

-- 文档级 KG 是 0→1 硬切：先保留旧 owner 身份完成评测引用清理，再一次性重置旧候选与任务。
DO $document_kg_pipeline$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM cyclops_schema_migrations
        WHERE id = '20260715_document_kg_pipeline_v1'
    ) THEN
        DELETE FROM knowledge_chunks
        WHERE source_type IN ('kg_entity', 'kg_relation');
        DELETE FROM kg_evidence;
        DELETE FROM kg_relations;
        DELETE FROM kg_entities;
        DROP TABLE IF EXISTS kg_extraction_job_items;
        DROP TABLE IF EXISTS kg_extraction_jobs;

        INSERT INTO cyclops_schema_migrations (id)
        VALUES ('20260715_document_kg_pipeline_v1');
    END IF;
END
$document_kg_pipeline$;

-- CREATE TABLE IF NOT EXISTS 不会补旧 evidence 列；reset 后统一安装当前非空约束。
ALTER TABLE kg_evidence
    ADD COLUMN IF NOT EXISTS char_start INTEGER,
    ADD COLUMN IF NOT EXISTS char_end INTEGER;

ALTER TABLE kg_evidence
    ALTER COLUMN char_start SET NOT NULL,
    ALTER COLUMN char_end SET NOT NULL;

ALTER TABLE kg_evidence
    DROP CONSTRAINT IF EXISTS kg_evidence_char_offsets_check,
    DROP CONSTRAINT IF EXISTS kg_evidence_source_locator_check,
    ADD CONSTRAINT kg_evidence_char_offsets_check
        CHECK (char_start >= 0 AND char_end > char_start),
    ADD CONSTRAINT kg_evidence_source_locator_check CHECK (
        (source_type = 'faq' AND source_chunk_id IS NULL)
        OR (source_type = 'document' AND source_chunk_id IS NOT NULL)
    );

CREATE TABLE IF NOT EXISTS kg_extraction_jobs (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('faq', 'document')),
    source_id TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued' CHECK (
        phase IN ('queued', 'mapping', 'resolving', 'reducing', 'completed', 'failed')
    ),
    processed_chunks INTEGER NOT NULL DEFAULT 0 CHECK (processed_chunks >= 0),
    total_chunks INTEGER NOT NULL CHECK (total_chunks >= 1),
    source_fingerprint TEXT NOT NULL,
    resolution_result JSONB,
    entity_count INTEGER NOT NULL DEFAULT 0 CHECK (entity_count >= 0),
    relation_count INTEGER NOT NULL DEFAULT 0 CHECK (relation_count >= 0),
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    model TEXT,
    error TEXT CHECK (error IS NULL OR char_length(error) <= 1000),
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT kg_extraction_jobs_progress_check
        CHECK (processed_chunks <= total_chunks),
    CONSTRAINT kg_extraction_jobs_resolution_object_check
        CHECK (resolution_result IS NULL OR jsonb_typeof(resolution_result) = 'object'),
    CONSTRAINT kg_extraction_jobs_faq_phase_check
        CHECK (source_type = 'document' OR phase NOT IN ('resolving', 'reducing')),
    CONSTRAINT kg_extraction_jobs_lease_pair_check
        CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS kg_extraction_jobs_one_active_source_idx
    ON kg_extraction_jobs (source_type, source_id)
    WHERE phase IN ('queued', 'mapping', 'resolving', 'reducing');

CREATE INDEX IF NOT EXISTS kg_extraction_jobs_claim_due_idx
    ON kg_extraction_jobs (next_attempt_at, created_at, id)
    WHERE phase IN ('queued', 'mapping', 'resolving', 'reducing');

CREATE INDEX IF NOT EXISTS kg_extraction_jobs_source_history_idx
    ON kg_extraction_jobs (source_type, source_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS kg_extraction_job_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES kg_extraction_jobs(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    chunk_order INTEGER NOT NULL CHECK (chunk_order >= 0),
    source_fingerprint TEXT NOT NULL,
    section_path JSONB NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    phase TEXT NOT NULL DEFAULT 'queued' CHECK (
        phase IN ('queued', 'mapping', 'mapped', 'failed')
    ),
    map_result JSONB,
    error TEXT CHECK (error IS NULL OR char_length(error) <= 1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, chunk_id),
    UNIQUE (job_id, chunk_order),
    CONSTRAINT kg_extraction_job_items_section_path_array_check
        CHECK (jsonb_typeof(section_path) = 'array'),
    CONSTRAINT kg_extraction_job_items_map_result_object_check
        CHECK (map_result IS NULL OR jsonb_typeof(map_result) = 'object')
);

CREATE INDEX IF NOT EXISTS kg_extraction_job_items_job_phase_order_idx
    ON kg_extraction_job_items (job_id, phase, chunk_order, id);
