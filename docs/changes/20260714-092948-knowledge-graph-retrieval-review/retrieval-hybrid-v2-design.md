# `retrieval_hybrid_v2` 设计

状态：用户已确认方案，书面设计复核通过，进入实现计划与测试驱动实施。

确认日期：2026-07-15（Asia/Shanghai）

## 1. 目标

把当前“pgvector + weighted `ILIKE` + RRF + optional rerank”升级为唯一的中文混合检索契约：

```text
PostgreSQL pgvector
+ PostgreSQL pg_search / pdb.jieba BM25
→ RRF
→ configured rerank
→ direct top-k
→ optional parent context
```

最终实现必须满足：

* FAQ/document lexical 使用真正 BM25 和一致的中文 tokenizer。
* PostgreSQL 继续持有 canonical 文本、来源状态、向量和倒排索引；不增加第二套全文服务。
* 每个分数的来源和含义明确，不能再把 vector、lexical、KG、RRF 或 rerank 分数统称为 `score/top_score`。
* 离线验证完成后一次硬切；FAQ/document 正式运行路径不保留 weighted `ILIKE`、旧 GIN、backend switch、双读或 fallback。

## 2. 明确不做

* 不把 FAQ、document 或 chunk 投影到 Neo4j。
* 不接入 Elasticsearch、OpenSearch、Meilisearch 或托管搜索 API。
* 不在业务表增加人工维护的分词字符串列。
* 不把 PostgreSQL 内置 `ts_rank` 或 `pg_trgm` 命名成 BM25。
* 不改变 KG 默认只用于显式评测 debug 的产品边界。
* 不在本实施单元实现 3D 图谱或图算法。

## 3. 部署契约

首版固定使用 ParadeDB Community `pg_search` v0.24.2：

* PostgreSQL 16 / Ubuntu 24.04 对应官方预编译包。
* `shared_preload_libraries` 必须包含 `pg_search`，安装后重启 PostgreSQL。
* 数据库 schema 使用 `CREATE EXTENSION IF NOT EXISTS pg_search`；缺系统包或 preload 时 schema 初始化硬失败。
* `python -m cyclops check-config` 和服务健康检查必须验证 PostgreSQL major、扩展存在、扩展版本及 BM25 index 状态。
* 启动前发现扩展缺失、版本不符或 migration/index 定义错误时进程硬失败；成功启动后的运行期 readiness 异常由 health 返回 503。两种情况都绝不切换回 `ILIKE`。

Community 扩展按 AGPL-3.0 使用。仓库保留版本、来源和许可证说明；本地实现与验证可继续。若未来把含扩展的闭源产品交付外部客户，发布前必须完成 AGPL 合规审查或取得商业许可。业务数据、配置与模型密钥不因此进入 Git。

## 4. 数据与索引

### 4.1 Canonical 文本

`knowledge_chunks.search_text` 继续保存可解释的原文组合，包括 FAQ 问题/相似问法/答案/标签，或文档标题/章节/关键词/假设问题/正文。它是唯一 lexical document，不另存应用侧 token。

索引过程是：

```text
search_text 原文
→ pdb.jieba
→ token + postings + TF + DF + document length
→ PostgreSQL BM25 index
```

token 和 corpus statistics 由索引内部维护；查询端使用同一 `pdb.jieba` tokenizer。

### 4.2 唯一 BM25 index

每张表只创建一个 BM25 index。目标结构为：

```sql
CREATE INDEX IF NOT EXISTS knowledge_chunks_bm25_idx
ON knowledge_chunks
USING bm25 (
    id,
    (search_text::pdb.jieba),
    (source_type::pdb.literal),
    (source_id::pdb.literal),
    (source_chunk_id::pdb.literal),
    (status::pdb.literal),
    (embedding_status::pdb.literal),
    (chunk_level::pdb.literal)
)
WITH (key_field = 'id')
WHERE source_type IN ('faq', 'document');
```

BM25 查询必须原样包含 `source_type IN ('faq', 'document')`，保证 PostgreSQL 能选择该 partial index。上述 DDL 是 v2 的唯一目标 schema，并由真实 PostgreSQL migration test 直接执行验证。

`IF NOT EXISTS` 只保证重复启动安全，不代表同名错误索引合法。readiness 必须通过 `pg_get_indexdef()` 与 canonicalized 目标 DDL 对比 tokenizer、字段、partial predicate 和 key field；定义不一致时启动/验收失败，不能沿用旧索引。

删除：

* `knowledge_chunks_search_idx` 的 `to_tsvector('simple', search_text)` GIN。
* FAQ/document weighted `ILIKE` SQL。
* 只为旧 SQL 服务的固定字段权重和 psycopg `%` 转义测试。

## 5. Lexical 查询契约

### 5.1 Query 构造

`HybridRetrievalService.retrieve()` 的输入唯一解释为原始用户 query，只做 Unicode/空白规范化，不在某个入口单独使用 LLM intent rewrite。Admin 当前特有的 `query_rewrite` 检索分支删除；CLI、微信、MCP、RagTool、Admin 和评测全部检索同一文本。intent analysis 继续负责安全与展示，但不改写检索 query，`QueryAnalysis.query_rewrite` 字段随 v2 删除。

人工 retrieval aliases 继续保留，但只扩展实际命中的 canonical/alias，不再维护固定中文领域词表来代替 tokenizer。

`build_keyword_terms()` 被删除，替换为命名准确的 lexical query builder：

* 原始 query 始终保留。
* 命中的 canonical 与 aliases 作为 OR 扩展。
* 错误码、型号和中英数字混排保留原字符串，不做应用侧切词。
* 所有值通过 SQL 参数传入；不接受用户直接提交 ParadeDB query-parser 语法。

诊断字段由 `query_terms` 改为 `lexical_expansions`，不把应用 alias 误称为 tokenizer 输出。

`normalized_query` 是规范化后的原始 query。`lexical_expansions` 只包含“命中的 canonical → 该词条 aliases”，不重复包含原始 query；构造时按该顺序稳定追加，以 Unicode trim + casefold 结果作为去重 key，但保留第一次出现值的原始拼写，并排除与 `normalized_query` 使用同一 key 的值，最多保留 20 个扩展。唯一查询参数固定为 `lexical_query = join([normalized_query, *lexical_expansions])`，各项之间使用单个空格。唯一 SQL 骨架为：

```sql
SELECT
  kc.id, kc.source_type, kc.source_id, kc.source_chunk_id,
  kc.parent_chunk_id, kc.chunk_level, kc.source_title,
  kc.section_path, kc.page_start, kc.page_end, kc.block_type,
  kc.source_offsets, kc.content, kc.metadata, kc.tags,
  kc.confidence, kc.status,
  pdb.score(kc.id) AS channel_score
FROM knowledge_chunks kc
LEFT JOIN import_files imp
  ON kc.source_type = 'document' AND imp.id = kc.source_id
LEFT JOIN import_chunks ic
  ON kc.source_type = 'document'
 AND ic.id = kc.source_chunk_id
 AND ic.file_id = kc.source_id
LEFT JOIN faq_documents fq
  ON kc.source_type = 'faq' AND fq.id = kc.source_id
WHERE kc.source_type IN ('faq', 'document')
  AND kc.search_text ||| %(lexical_query)s
  AND (
    (kc.source_type = 'faq' AND fq.status = 'usable' AND fq.embedding_status = 'ready')
    OR (kc.source_type = 'document' AND kc.status = 'usable')
  )
  AND kc.embedding_status = 'ready'
  AND kc.embedding IS NOT NULL
  AND (
    kc.source_type <> 'document'
    OR (
      imp.id IS NOT NULL AND ic.id IS NOT NULL
      AND imp.is_disabled = false AND ic.is_disabled = false
    )
  )
  AND (kc.source_type <> 'document' OR kc.chunk_level = 'child')
ORDER BY pdb.score(kc.id) DESC, kc.id ASC
LIMIT %(candidate_limit)s;
```

不为每个 alias 生成动态 OR SQL，不使用 `unnest` 计分，不在 Python 做命中后过滤。

### 5.2 实时资格门禁

BM25 SQL 与 vector SQL 保持相同的正式知识资格：

* source type 只能是 `faq` 或 `document`。
* FAQ 必须实时 `status='usable'`、FAQ embedding ready、knowledge row embedding ready，且 `kc.embedding IS NOT NULL`。
* document knowledge row必须 usable、embedding ready、`kc.embedding IS NOT NULL`、chunk level 为 child。
* document file 与 source chunk 必须仍存在且未禁用，且 chunk 必须精确属于该 file。

这些条件必须在同一 SQL 的 `WHERE` 中、在最终排序与 `LIMIT` 之前生效。禁止先取 BM25 top-k，再在 Python 中删除失效来源。

### 5.3 排序

lexical channel 固定按：

```text
pdb.score(id) DESC, id ASC
```

排序必须稳定。BM25 raw score 只用于 lexical channel 内部排序和诊断，不与 vector、KG 或 rerank raw score直接比较。

## 6. Canonical 检索模型

当前 `RetrievedKnowledgeChunk.score` 把内容实体与某次查询的异构分数混在一起。v2 将两者分离：

### `RetrievedKnowledgeChunk`

只保存 canonical 内容与 provenance，不再包含 `score`：

* ID、source type/source ID/source chunk ID
* parent/child 信息
* title、section、page、offset、content
* metadata、tags、status

### `RetrievalChannelHit`

表示某一路对某个 chunk 的一次命中：

* `document`
* `channel`: `vector`、`lexical_bm25` 或 `kg`
* `channel_rank`
* `channel_score`

### `FusedCandidate`

表示最终 direct candidate：

* `document`
* `fused_rank`
* `final_rank`
* `channel_hits: tuple[RetrievalChannelHit, ...]`
* `fused_score`
* `rerank_score: float | None`
* KG debug 的 fact matches

parent context 只返回 `RetrievedKnowledgeChunk`，没有人工 `score=1.0`。

channel names 和 `channel_scores` 只从 `channel_hits` 派生，不在 candidate 维护第二套可漂移字段。`HybridRetrievalResult` 对应字段固定为 `vector_hits`、`bm25_hits`、`kg_hits`、`candidates` 和 `parent_contexts`，不再返回带某一路分数的 document lists。其中 `vector_hits/bm25_hits` 是 `RetrievalChannelHit`，`kg_hits` 是稳定 `GraphFactHit` 并保留没有 live evidence 的 orphan fact 供诊断；KG 展开的原始知识命中只进入 candidate 的 `channel_hits(channel='kg')`。

### `GraphFactHit`

KG seed producer 与 evidence expansion 之间只使用稳定领域身份：

* `fact_id`：PostgreSQL entity/relation 业务 ID。
* `fact_type`：`entity` 或 `relation`。
* `fact_rank`。
* `seed_score`：仅用于当前 seed channel 诊断，不跨 producer 比较。

v2 先让现有 PostgreSQL KG seed query 返回该 DTO，并删除 synthetic `fact_chunk_id` 契约；后续 Neo4j 单元直接成为同一 DTO 的唯一 producer，同时删除 PostgreSQL producer。不存在 adapter、backend option 或两个 producer 同时运行。

Evidence consumer 唯一签名为 `expand_graph_fact_hits(hits: Sequence[GraphFactHit])`：`fact_type='entity'` 连接 `kg_evidence.entity_id=fact_id`，`fact_type='relation'` 连接 `kg_evidence.relation_id=fact_id`。查询复用 FAQ usable、document file/chunk enabled、document direct child 等实时门禁；按原始 knowledge row ID 去重，采用 best fact rank，保留全部 fact matches，并固定按 `best_fact_rank ASC, original_id ASC` 返回。

## 7. RRF 与 rerank

### 7.1 RRF

* vector、BM25 和显式 KG debug 各自只按 rank 投票。
* 同一 canonical knowledge row 按 ID 合并。
* 同一知识行对应多个 KG fact 时仍只贡献一次 best-rank KG vote，同时保留全部 fact 诊断。
* `rrf_k=60` 首版保持固定，避免在没有金标时增加无依据参数。

### 7.2 Rerank

只要 rerank client 已配置且融合后至少有两条候选，就执行重排；候选数小于或等于最终 `top_k` 也不能跳过，因为 rerank 的职责包含重新排序，而不只是截断。

规则：

* 输入上限为 `rerank_input_size`。
* provider 返回的合法 `index + relevance_score` 同时保存。
* partial response 按原 RRF 顺序补足，补足项 `rerank_score=None`。
* provider 失败、空返回或没有合法 index 时，完整保持 RRF 顺序并标记 `rerank_used=false`。
* 不把不同 provider 的 relevance score 当作跨模型统一概率。
* `relevance_score` 必须是有限数；NaN、Infinity、bool、重复或越界 index 均为非法结果。

`final_rank` 永远表示最终列表位置；`fused_rank` 保留 RRF 位置。provider 命中的候选使用 `ranking_score_kind='rerank'`，补足项使用 `ranking_score_kind='rrf'` 和自身 fused score。一个列表可以包含不同 score kind，调用方只能按 `final_rank` 排序，不能比较异构 `ranking_score` 重新排序。

## 8. Wire 与 analytics 契约

删除语义不确定的顶层 `top_score`、`min_score` 和 document `score`。所有入口使用独立的 `RetrievalEnvelope`，direct candidates 与 parent contexts 永远分开：

```json
{
  "query": "报告怎么导出",
  "top_k": 5,
  "candidate_limit": 50,
  "rerank_used": true,
  "retrieval_latency_ms": 51,
  "lexical_expansions": ["报告导出"],
  "channel_counts": {"vector": 12, "lexical_bm25": 9, "kg": 0},
  "stage_latencies_ms": {
    "alias": 1,
    "embedding": 18,
    "vector": 4,
    "bm25": 3,
    "kg": 0,
    "rrf": 1,
    "rerank": 22,
    "parent": 2
  },
  "candidates": [
    {
      "final_rank": 1,
      "fused_rank": 2,
      "ranking_score_kind": "rerank",
      "ranking_score": 0.91,
      "fused_score": 0.0325,
      "rerank_score": 0.91,
      "channel_scores": {
        "vector": 0.78,
        "lexical_bm25": 6.42,
        "kg": null
      },
      "document": {
        "id": "kc_document_example",
        "source_type": "document",
        "source_id": "file_example",
        "content": "报告导出操作说明"
      }
    }
  ],
  "parent_contexts": [
    {
      "document": {
        "id": "kc_document_parent_example",
        "source_type": "document",
        "source_id": "file_example",
        "content": "报告管理章节上下文"
      }
    }
  ]
}
```

未使用 rerank 时，所有 direct candidates 为 `ranking_score_kind='rrf'` 且 `ranking_score=fused_score`。search/evaluation 的 `parent_contexts` 为空；answer 路径可以把 parent 加入 prompt，但不会混入 candidates。

Admin SSE、RagTool、MCP 和评测 DTO 统一把该对象放在 `retrieval` 字段；业务外层只增加 `answer_draft`、event type、tool/mode 等字段，不再各自重新拼装 `documents` 形状。

analytics 同步记录：

* top rank score 与 score kind
* fused/rerank/channel scores
* rerank adapter/model
* candidate count、各 channel count
* `request_latency_ms`、`retrieval_latency_ms` 与各阶段 latency

`retrieval_latency_ms` 由 `HybridRetrievalService.retrieve()` 从方法入口到结果返回前直接测量，是唯一端到端检索耗时；它同时写入 `RetrievalEnvelope` 和 analytics。所有 stage latency 也由该服务在实际调用边界内部测量并随结果返回；调用方不得在整次 `retrieve()` 已完成后伪造 vector/BM25/rerank 起止时间。固定阶段为 alias、embedding、vector、BM25、KG、RRF、rerank 和 parent。阶段耗时之和受调度、公共处理和计时边界影响，不得当作或反推 `retrieval_latency_ms`。

现有 analytics `latency_ms` 明确重命名为 `request_latency_ms`，表示包含检索之外的 answer/tool/transport 处理在内的整次请求耗时；不保留旧字段别名。调用方分别提交请求总耗时与服务返回的检索总耗时，二者不得互相代填。

旧的“用一个固定阈值比较所有 `top_score`”低置信逻辑删除。零命中继续作为确定性诊断；非零命中的低置信判断必须等具体 ranker/provider 建立校准阈值后再增加，不做静默推测。

`query_analytics_events` v2 schema 固定为：

* 保留 query、intent、retrieved chunk IDs、hit count 和 requester；把旧 `latency_ms` 重命名为 `request_latency_ms`。
* 删除 `top_score`。
* 增加 `top_ranking_score`、`top_ranking_score_kind`、`top_fused_score`、`top_rerank_score`。
* 增加 `top_channel_scores JSONB`、`channel_counts JSONB`、`stage_latencies_ms JSONB` 和 `retrieval_latency_ms`。
* 增加 `rerank_used`、`rerank_adapter`、`rerank_model`；adapter 固定记录 `cohere_compatible` 等代码枚举，不保存 base URL、API key 或任意凭据。

删除 `/api/analytics/low-score` 及其 SQL/UI；当前 analytics 数据为 0，迁移直接清空派生事件，不转换旧匿名 score。

## 9. 评测契约

评测策略升级为 v3，旧 v2 运行属于可重建派生数据，迁移时删除，不增加兼容读取。

每个 case 可以离线运行以下诊断，但正式生产仍只有一个 `retrieval_hybrid_v2`：

* vector-only
* lexical-BM25-only
* full hybrid
* full hybrid + KG debug

vector-only 与 lexical-BM25-only 不执行 rerank，用于衡量原始 channel；full hybrid 与 KG debug 使用正式 rerank 配置。

指标：

* Recall@K
* Precision@K
* MRR
* Hit@1
* nDCG@K
* vector/lexical/KG channel recall
* candidate coverage
* total、vector、BM25、rerank P50/P95 latency

仓库保留非敏感中文集成语料，分类 ID 固定为：`chinese_compound`、`short_term`、`error_or_model_code`、`mixed_zh_latin_number`、`alias`、`lifecycle_gate`。自动化门至少 30 个标注 case，每类不少于 5 个；语料同时覆盖 FAQ/document、parent/child 和禁用来源。真实业务质量门在用户导入实际材料后通过管理页评测集完成，不把生产内容提交 Git。

指标只统计存在 expected ID 的标注 case；expected chunk IDs 优先，否则使用 expected source IDs，无 expected 的 case 不进入分母。首版相关性为 binary，所有汇总为 case macro average：

* `Recall@K = |expected ∩ topK| / |expected|`。
* `Precision@K = |expected ∩ topK| / K`。
* `MRR = 1 / first_relevant_rank`，无命中为 0。
* `nDCG@K` 使用 binary gain `1/log2(rank+1)`，除以该 case 的理想 DCG。
* channel recall 对 vector/BM25/KG 各自 top-K 使用同一 Recall@K 公式。
* candidate coverage 为“pre-rerank union candidate pool 至少包含一个 expected ID 的 case 数 / labeled case 数”。

## 10. 配置与错误处理

增加严格范围校验：

* `RAG_TOP_K >= 1`
* `0 <= RAG_VECTOR_MIN_SCORE <= 1`
* `RERANK_INPUT_SIZE >= RAG_TOP_K`
* 所有 timeout 必须大于 0

删除 `RAG_MIN_SCORE` / `rag_min_score` / wire `min_score`；唯一新名称为 `RAG_VECTOR_MIN_SCORE`、`rag_vector_min_score` 和内部 `vector_min_score`。它只控制 vector channel 的 cosine candidate 入选，不代表 BM25、RRF、rerank 或最终回答置信度，不读取旧配置别名。

BM25 不增加“是否启用”开关。扩展、索引或查询失败按入口现有错误 transport 返回明确错误；不能只返回 vector 结果，也不能执行旧 lexical。

## 11. 迁移与硬切

1. 在隔离 PostgreSQL 16 环境安装并锁定 `pg_search` 0.24.2。
2. 先用当前 SQL 记录非敏感基线结果与延迟到测试 artifact；该实现不进入最终 runtime。
3. 按“安装系统包/preload并重启 → 执行 schema migration → `check-config` → 启动服务”的固定顺序部署。缺包、版本或 migration 错误使启动硬失败；只有成功启动后发生的数据库/索引运行期异常才由 health 返回 503。
4. 创建 BM25 index，执行生命周期、rollback、VACUUM、reindex、重启和备份恢复测试。
5. 切换唯一 lexical SQL、canonical models、wire、analytics 和 eval v3。
6. 删除 FAQ/document weighted `ILIKE`、旧 GIN、旧 query terms、旧 score fields 和对应测试。KG debug 的现有 ILIKE 只存续到后续 Neo4j 同次硬切，不是 v2 fallback，也不被正式 `use_kg=false` 流量调用。
7. `rg` 静态门确认正式代码没有旧 lexical runtime、backend switch 或 fallback。
8. Assistant 浏览器持久化 key 从 `cs-assistant-v1` 硬切到 `cs-assistant-v2`，旧本地会话直接丢弃，不解析旧 source/score 形状。
9. 完成全量测试与真实 PostgreSQL `EXPLAIN (ANALYZE, BUFFERS)` 后启动服务供用户验收。

当前业务库没有 FAQ/document/eval 数据，因此不需要迁移用户内容；schema 与索引仍必须支持以后正常 insert/update/delete/disable/re-enable。

## 12. 验证门

### 正确性

* FAQ needs_review/disabled/stale、document parent、禁用文件、禁用切片、删除来源零泄漏。
* insert/update/delete/disable/re-enable 和事务 rollback 后索引结果与 canonical 表一致。
* query alias、错误码和中文 tokenizer 在索引端/查询端一致。
* parent 不参与排名，且不携带伪分数。

### 质量

* 非敏感集成语料的 expected ID Recall@K 不低于旧基线。
* 错误码、型号和安全门禁用例不得回退。
* 若旧基线未满分，MRR/Hit@1/nDCG 至少一项提升且其余回退不超过 1 个百分点；若旧基线相关指标均为 1.0，则要求全部不退化。
* 专门的中文 BM25 子集相对旧 lexical 至少在 Recall@K 或 MRR 一项严格提升；否则停止切换并报告，不把旧路径打包为 fallback。

### 性能与运维

* `EXPLAIN (ANALYZE, BUFFERS)` 确认 BM25 index 被使用，live filter 后 top-k 不欠量。
* 在当前测试规模与 10 倍预估规模上记录 warm P50/P95；本机 lexical P95 目标不高于 50ms。
* 验证 autovacuum/dead rows、reindex、数据库重启、扩展升级检查和完整恢复。

### Readiness

* `check-config` 查询 `server_version_num`、`pg_extension.extversion='0.24.2'`、`pg_index.indisvalid/indisready` 和 `pdb.indexes()` 中的目标索引。
* `GET /api/health/retrieval` 返回相同的非敏感 readiness 摘要；缺扩展/坏索引返回 503。
* 完整 `pdb.verify_index()` 只用于发布检查命令，不在每次健康请求执行。

### 静态清理

* 正式 Python/SQL 不再包含 FAQ/document weighted `ILIKE`。
* 不再创建 `to_tsvector('simple', search_text)` GIN。
* 不存在 runtime `old/new`、`postgres/other` lexical backend 开关。
* 不存在匿名 `score/top_score` 对外字段。

## 13. 实施边界

本设计是第一个独立实施单元。它完成并通过自身质量门后，再切入 Neo4j 图读模型实施；两者共享 `HybridRetrievalService` 的稳定上层入口，但不共享存储职责或失败 fallback。
