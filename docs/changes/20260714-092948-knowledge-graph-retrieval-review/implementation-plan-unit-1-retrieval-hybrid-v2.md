# Retrieval Hybrid v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development task-by-task. 不得保留 v1 wire、`ILIKE` lexical、旧配置名、backend switch 或静默 fallback；本计划不自动提交 Git commit。

**Goal:** 把 FAQ/document 检索硬切为 PostgreSQL `pg_search 0.24.2 + pdb.jieba` BM25、pgvector、RRF 和可选 rerank 的唯一 v2 契约，并让每个排名分数和延迟都有唯一语义。

**Architecture:** PostgreSQL 同时保存 canonical `search_text`、pgvector 和 BM25 index；应用只构造规范化原查询与人工 alias 扩展。数据库返回有明确 channel/rank/raw score 的 hits，服务层用 RRF 融合、保存 rerank relevance score，并通过一个 `RetrievalEnvelope` 供 Admin、CLI、RAG tool、MCP、微信与评测共享。

**Tech Stack:** Python 3.11、PostgreSQL 16、ParadeDB `pg_search` 0.24.2、`pdb.jieba`、pgvector、FastAPI、React/TypeScript、pytest、Vitest。

---

## File map

- Modify: `sql/001_init.sql` — 安装扩展、唯一 BM25 index、analytics v2、eval v3 与旧派生数据清理。
- Modify: `cyclops/db/models.py` — 无分数 canonical chunk 与 channel hit DTO。
- Create: `cyclops/graph.py` — 跨 PostgreSQL/Neo4j 稳定的 `GraphFactHit` 领域身份。
- Modify: `cyclops/db/knowledge.py` — vector/BM25/readiness SQL 与 row mapper。
- Modify: `cyclops/db/analytics.py` — 命名 score/latency 打点，删除 low-score 查询。
- Modify: `cyclops/db/retrieval_meta.py` — eval contract v3 与四个明确诊断策略。
- Modify: `cyclops/db/__init__.py` — 只导出当前 DTO/常量。
- Modify: `cyclops/retrieval.py` — lexical builder、计时、RRF、rerank、envelope 和 v3 metrics。
- Modify: `cyclops/llm.py` — 严格校验 rerank index 与有限 relevance score。
- Modify: `cyclops/config.py`, `.env.example`, `cyclops/admin_server.py`, `cyclops/asgi_app.py`, `cyclops/cli.py` — 严格配置、readiness、health 与服务装配。
- Modify: `cyclops/rag.py`, `cyclops/rag_tool.py`, `cyclops/mcp_server.py`, `cyclops/wechat_service.py` — 唯一 envelope consumer。
- Modify: `web/src/api/schemas.ts`, `web/src/api/hooks.ts`, `web/src/store/assistant.ts`, `web/src/pages/assistant/debug-drawer.tsx`, `web/src/pages/evaluation/*`, `web/src/pages/SettingsPage.tsx`, `web/src/pages/settings/settings-model.ts` — v2/v3 wire、展示与本地存储硬切。
- Create: `scripts/install_pg_search_0_24_2.sh` — Ubuntu 24.04 / PG16 精确安装与 preload 检查。
- Create: `tests/test_retrieval_postgres.py` — 真实扩展、索引、生命周期、EXPLAIN 和中文金标集成门。
- Create: `tests/fixtures/retrieval_v2_corpus.json` — 非敏感 FAQ/document 合成语料。
- Create: `tests/fixtures/retrieval_v2_cases.json` — 六类、每类至少五条的 expected-ID 金标。
- Modify: `tests/test_retrieval.py`, `tests/test_db.py`, `tests/test_config.py`, `tests/test_llm.py`, `tests/test_admin_server.py`, `tests/test_asgi_app.py`, `tests/test_rag.py`, `tests/test_rag_tool.py`, `tests/test_mcp_server.py`, `tests/test_cli.py`, `tests/test_wechat_service.py` — 当前 Python 契约。
- Modify: `web/src/api/schemas-contract.test.ts`, `web/src/pages/evaluation/*.test.ts`, `web/src/pages/assistant/*.test.ts`, `web/src/pages/settings/settings-model.test.ts` — 当前前端契约。

### Task 1: 锁定扩展、配置与 schema 的硬失败契约

- [ ] **Step 1: 先写配置与 schema 失败测试**

在 `tests/test_config.py` 和 `tests/test_db.py` 增加以下断言：

```python
def test_settings_use_vector_specific_threshold_without_old_alias():
    """向量阈值只能由 RAG_VECTOR_MIN_SCORE 配置，旧名字不得读取。"""
    settings = Settings.from_env(valid_env(RAG_VECTOR_MIN_SCORE="0.42"))
    assert settings.rag_vector_min_score == 0.42
    with pytest.raises(SettingsError, match="RAG_VECTOR_MIN_SCORE"):
        Settings.from_env(valid_env(RAG_VECTOR_MIN_SCORE="1.01"))


def test_schema_uses_only_jieba_bm25_index():
    """FAQ/document lexical 只能使用一个 pdb.jieba BM25 partial index。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS pg_search" in schema
    assert "knowledge_chunks_bm25_idx" in schema
    assert "search_text::pdb.jieba" in schema
    assert "WHERE source_type IN ('faq', 'document')" in schema
    assert "knowledge_chunks_search_idx" not in schema
```

还要测试 `RAG_TOP_K >= 1`、`RERANK_INPUT_SIZE >= RAG_TOP_K`、全部 timeout 大于 0；测试必须确认环境里只有 `RAG_MIN_SCORE` 时失败，不把旧名当 alias。

- [ ] **Step 2: 运行 RED**

Run:

```bash
python -m pytest tests/test_config.py tests/test_db.py -k "vector_min or bm25 or pg_search or range" -q
```

Expected: 因当前读取 `RAG_MIN_SCORE`、schema 仍创建 `simple` GIN 且没有 `pg_search` 而失败。

- [ ] **Step 3: 实现唯一配置名和 BM25 DDL**

配置固定为：

```python
rag_top_k: int = 5
rag_vector_min_score: float = 0.35
rerank_input_size: int = 50
```

环境和 settings API 只使用 `RAG_VECTOR_MIN_SCORE` / `rag_vector_min_score`。schema 固定加入：

```sql
CREATE EXTENSION IF NOT EXISTS pg_search;

DROP INDEX IF EXISTS knowledge_chunks_search_idx;

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

`IF NOT EXISTS` 不代表同名坏索引有效；下一步 readiness 必须比较定义。

- [ ] **Step 4: 增加数据库 readiness 和 HTTP health**

在 `KnowledgeMixin` 增加：

```python
def retrieval_readiness(self) -> dict[str, Any]:
    """验证 PG16、pg_search 0.24.2 与唯一 BM25 index 定义，不执行回退。"""

def assert_retrieval_ready(self) -> dict[str, Any]:
    """启动前强制检索依赖 ready；任何版本或索引偏差都直接抛错。"""
```

检查 `server_version_num // 10000 == 16`、`pg_extension.extversion == '0.24.2'`、`pg_index.indisvalid/indisready`、`pg_get_indexdef()` 的 tokenizer/字段/key/predicate 以及 `pdb.indexes()` 目标行。`run_admin_asgi()` 与 `check-config` 在真实装配时调用 assert；`GET /api/health/retrieval` 返回非敏感摘要，运行期异常映射 503，且不返回 vector-only 结果。

- [ ] **Step 5: 实现精确安装脚本并验证语法**

`scripts/install_pg_search_0_24_2.sh` 固定检查 Ubuntu 24.04、x86_64、PG16，并只下载以下官方 release asset：

```text
https://github.com/paradedb/paradedb/releases/download/v0.24.2/postgresql-16-pg-search_0.24.2-1PARADEDB-noble_amd64.deb
sha256: 83e2191ff2265760565e36fe3e5b94a1a8110aba82a940ba3d00b78d98d699b0
```

脚本在 `dpkg -i` 前执行 SHA256 校验，把 `pg_search` 加入 `shared_preload_libraries`，重启 PostgreSQL，并用 `SHOW shared_preload_libraries` 验证；许可证来源固定记录 `https://github.com/paradedb/paradedb/blob/v0.24.2/LICENSE`。脚本不得安装 latest 或按失败结果选择另一扩展。

Run:

```bash
bash -n scripts/install_pg_search_0_24_2.sh
python -m pytest tests/test_config.py tests/test_db.py -k "vector_min or bm25 or readiness" -q
```

Expected: PASS。

### Task 2: 用 channel hit 分离内容与查询分数

- [ ] **Step 1: 写 DTO 与数据库 API 的失败测试**

在 `tests/test_retrieval.py` / `tests/test_db.py` 先声明目标接口：

```python
document = RetrievedKnowledgeChunk(
    id="kc_1",
    source_type="faq",
    source_id="faq_1",
    source_chunk_id=None,
    parent_chunk_id=None,
    chunk_level="chunk",
    source_title="报告导出",
    section_path=[],
    page_start=None,
    page_end=None,
    block_type=None,
    source_offsets={},
    content="进入报告页后点击导出。",
    metadata={},
    tags=[],
    confidence="high",
    status="usable",
)
assert not hasattr(document, "score")

hit = RetrievalChannelHit(
    document=document,
    channel="lexical_bm25",
    channel_rank=1,
    channel_score=6.42,
)
assert hit.channel_score == 6.42
```

DB 测试要求方法只存在当前名称，且三个读取方法都不接受 caller 可覆盖的 `status` 参数：

```text
search_knowledge_vector(query_embedding, top_k, vector_min_score)
search_knowledge_bm25(lexical_query, top_k)
get_parent_context_chunks(child_ids)
```

并断言 `search_knowledge_text` 不存在。

- [ ] **Step 2: 运行 RED**

Run:

```bash
python -m pytest tests/test_retrieval.py tests/test_db.py -k "channel_hit or knowledge_bm25 or score_model" -q
```

Expected: 当前 canonical chunk 带 `score` 且没有 BM25 API，因此失败。

- [ ] **Step 3: 实现 DTO 和 mapper**

`RetrievedKnowledgeChunk` 删除 `score`；新增：

```python
@dataclass(frozen=True)
class RetrievalChannelHit:
    """表示一次通道命中；raw score 只能在该通道内部解释。"""

    document: RetrievedKnowledgeChunk
    channel: Literal["vector", "lexical_bm25", "kg"]
    channel_rank: int
    channel_score: float
```

数据库把 canonical mapper 与 hit mapper 分开。parent SQL 不再生成 `1.0 AS score`；vector SQL 改为 `AS channel_score`，稳定按 distance、ID 排序；BM25 SQL 完整使用设计文档的同一 live-status predicate，并在 `LIMIT` 前过滤。

- [ ] **Step 4: 实现唯一 BM25 query**

```python
def search_knowledge_bm25(
    self,
    lexical_query: str,
    *,
    top_k: int,
) -> list[RetrievalChannelHit]:
    """用 pdb.jieba BM25 返回实时可用 FAQ/document hits。"""
```

SQL 必须包含：

```sql
WHERE kc.source_type IN ('faq', 'document')
  AND kc.search_text ||| %(lexical_query)s
  AND kc.embedding_status = 'ready'
  AND kc.embedding IS NOT NULL
  AND (kc.source_type <> 'document' OR kc.chunk_level = 'child')
ORDER BY pdb.score(kc.id) DESC, kc.id ASC
LIMIT %(top_k)s
```

FAQ status/embedding 与 document file/chunk enabled 的完整 join predicate 沿用 vector 当前契约；不先取 top-N 再由 Python 丢弃。

- [ ] **Step 5: 运行 GREEN**

Run:

```bash
python -m pytest tests/test_db.py tests/test_retrieval.py -k "knowledge or channel_hit or parent" -q
```

Expected: PASS，parent 保持无查询分数的 canonical chunk。

- [ ] **Step 6: 先把 KG debug 边界硬切为 stable fact identity**

在 `cyclops/graph.py` 用唯一 DTO 替换 `KgFactHit`：

```python
@dataclass(frozen=True)
class GraphFactHit:
    """表示图事实 seed 命中；身份只使用 PostgreSQL entity/relation 业务 ID。"""
    fact_id: str
    fact_type: Literal["entity", "relation"]
    fact_rank: int
    seed_score: float
```

现有 PostgreSQL KG seed producer 暂时仍作为唯一 producer，但返回 `source_id + entity/relation`，不再暴露 synthetic knowledge chunk ID。`expand_graph_fact_hits(hits)` 直接按 `fact_type/fact_id` join `kg_evidence` 与 live FAQ/document child；分组 key 固定 `(fact_type, fact_id)`。测试断言 DTO、wire、fusion 和 eval analysis 中没有 `fact_chunk_id/fact_score`。Unit 2 切换提交将直接删除该 PostgreSQL seed producer并由 Neo4j 生成同一 DTO，不增加 adapter、backend argument 或并行 producer。

- [ ] **Step 7: 运行 KG stable-ID 回归**

Run:

```bash
python -m pytest tests/test_db.py tests/test_retrieval.py tests/test_admin_server.py -k "graph_fact or kg" -q
```

Expected: PASS；最终评测候选仍只有原始 FAQ/document，fact 只作诊断。

### Task 3: 实现 lexical builder、RRF、rerank 与真实计时

- [ ] **Step 1: 写 lexical builder 失败测试**

目标模型和结果：

```python
result = build_lexical_query(
    "  报告\n怎么导出  ",
    [{"canonical": "报告导出", "aliases": ["导出报告", "报告怎么导出"]}],
)
assert result.normalized_query == "报告 怎么导出"
assert result.lexical_expansions == ("报告导出", "导出报告", "报告怎么导出")
assert result.lexical_query == "报告 怎么导出 报告导出 导出报告 报告怎么导出"
```

增加 casefold 去重、首次拼写保留、原查询排除、20 条上限、错误码/型号原样保留测试；删除固定 `DOMAIN_KEYWORDS` 与 `build_keyword_terms` 断言。

- [ ] **Step 2: 写 RRF/rerank 失败测试**

`FusedCandidate` 唯一字段为：

```python
document
fused_rank
final_rank
channel_hits
fused_score
rerank_score
kg_matches
```

测试至少两条候选且 client 已配置时始终调用 rerank，即使候选数等于 `top_k`；合法 `index + relevance_score` 同时保存；NaN、Infinity、bool、重复和越界 index 被忽略；partial response 按 RRF 顺序补足并保留 `rerank_score=None`。RRF 同分只按 canonical `document.id` 稳定破平，绝不比较 vector 与 BM25 raw score。

- [ ] **Step 3: 运行 RED**

Run:

```bash
python -m pytest tests/test_retrieval.py -k "lexical or rrf or rerank or latency" -q
```

Expected: 当前固定关键词、匿名 channel fields、候选数等于 top-k 时跳过 rerank，因此失败。

- [ ] **Step 4: 实现当前模型和服务计时**

新增：

```python
@dataclass(frozen=True)
class LexicalQuery:
    """保存规范化原查询、alias 扩展和唯一 BM25 查询参数。"""
    normalized_query: str
    lexical_expansions: tuple[str, ...]
    lexical_query: str


@dataclass(frozen=True)
class HybridRetrievalResult:
    """保存 direct hits/candidates、parent context 和服务内真实耗时。"""
    query: str
    top_k: int
    candidate_limit: int
    lexical_expansions: tuple[str, ...]
    vector_hits: tuple[RetrievalChannelHit, ...]
    bm25_hits: tuple[RetrievalChannelHit, ...]
    kg_hits: tuple[GraphFactHit, ...]
    candidates: tuple[FusedCandidate, ...]
    parent_contexts: tuple[RetrievedKnowledgeChunk, ...]
    rerank_used: bool
    stage_latencies_ms: dict[str, int]
    retrieval_latency_ms: int
```

`vector_hits/bm25_hits` 是原始 `RetrievalChannelHit`；`kg_hits` 是 stable `GraphFactHit`，包括没有 live evidence 的 orphan fact 以供评测诊断。KG 展开后的原始知识命中只进入 candidate 的 `channel_hits(channel='kg')`，不生成第二份 synthetic document list。

`HybridRetrievalService.retrieve()` 用 `time.perf_counter()` 在方法入口和返回前直接测量 total；每个阶段在实际调用边界计时。不得把阶段求和当 total。生产 `retrieve()` 永远执行 vector+BM25；评测的 channel-only 诊断使用单独命名的 `retrieve_channel_diagnostic(query, channel)`，channel 只接受 `vector` 或 `lexical_bm25`，不成为生产 backend 开关。RRF 排序键固定为 `(-fused_score, document.id)`。rerank provider 失败时完整保持 RRF 顺序并标 `rerank_used=false`；这是 ranker 失败语义，不是 lexical backend fallback。

- [ ] **Step 5: 运行 GREEN**

Run:

```bash
python -m pytest tests/test_retrieval.py -q
```

Expected: PASS；异构 raw score 只在 `channel_hits` 内存在。

### Task 4: 硬切所有调用方到一个 RetrievalEnvelope

- [ ] **Step 0: 完成 UI 布局 checkpoint**

在修改 React 页面前，把“现有 Assistant debug drawer 和 Evaluation workbench 原布局内替换 v2/v3 诊断，不增加营销卡片或新导航”的 UI prompt 发给用户；收到用户基于 prompt 的布局图或明确允许沿用现有布局后，才执行本 Task 的 React 文件步骤。该 checkpoint 不阻塞前面三项纯后端 Task。

- [ ] **Step 1: 先更新 caller contract tests**

目标 wire 固定为：

```json
{
  "query": "报告怎么导出",
  "top_k": 5,
  "candidate_limit": 50,
  "rerank_used": true,
  "retrieval_latency_ms": 51,
  "lexical_expansions": ["报告导出"],
  "channel_counts": {"vector": 12, "lexical_bm25": 9, "kg": 0},
  "stage_latencies_ms": {"alias": 1, "embedding": 18, "vector": 4, "bm25": 3, "kg": 0, "rrf": 1, "rerank": 22, "parent": 2},
  "candidates": [],
  "parent_contexts": []
}
```

每个 candidate 序列化 `final_rank/fused_rank/ranking_score_kind/ranking_score/fused_score/rerank_score/channel_scores/document/kg_matches`。调用方测试必须拒绝顶层 `top_score`、`min_score`、`documents` 和 document `score`。

- [ ] **Step 2: 删除 `QueryAnalysis.query_rewrite`**

规则与 Chat intent classifier 只输出 intent/safety/display 字段。Admin、评测、CLI、微信、MCP 和 RagTool 全部把规范化前的同一用户 query 交给检索；前端 debug 不再显示“改写 query”。这不是字段保留加忽略，而是 Python/JSON/TypeScript 三层同时删除。

- [ ] **Step 3: 实现一个 serializer 并复用**

```python
def retrieval_envelope_payload(result: HybridRetrievalResult) -> dict[str, Any]:
    """把唯一检索结果序列化为所有 transport 共用的 v2 envelope。"""
```

RagTool search/answer、MCP search/answer、Admin SSE step/done、CLI JSON 与评测均调用该函数，不在各入口重拼 document list。RAG prompt 只从 `candidates[].document + parent_contexts` 读取；parent 不进入命中数、analytics IDs 或排名。

- [ ] **Step 4: 硬切浏览器本地状态**

`web/src/store/assistant.ts` 的持久 key 改成 `cs-assistant-v2`，不读取、转换或合并 `cs-assistant-v1`。TypeScript schema 删除旧 score/rewrite/documents shape，debug drawer 按 final rank、score kind 和 channel score 展示。

- [ ] **Step 5: 运行跨入口测试**

Run:

```bash
python -m pytest tests/test_rag.py tests/test_rag_tool.py tests/test_mcp_server.py tests/test_cli.py tests/test_wechat_service.py tests/test_admin_server.py tests/test_asgi_app.py -q
cd web && npm test && npm run typecheck
```

Expected: PASS；没有 caller 直接调用 vector/BM25/fusion。

### Task 5: Analytics v2 与 evaluation v3

- [ ] **Step 1: 写 analytics schema/route 失败测试**

`query_analytics_events` 当前 schema 固定为：

```sql
request_latency_ms INTEGER,
retrieval_latency_ms INTEGER,
top_ranking_score DOUBLE PRECISION,
top_ranking_score_kind TEXT,
top_fused_score DOUBLE PRECISION,
top_rerank_score DOUBLE PRECISION,
top_channel_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
channel_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
stage_latencies_ms JSONB NOT NULL DEFAULT '{}'::jsonb,
rerank_used BOOLEAN NOT NULL DEFAULT false,
rerank_adapter TEXT,
rerank_model TEXT
```

迁移先清空当前派生事件，再删除 `top_score` / `latency_ms`。测试断言 `AnalyticsMixin.list_low_score_queries` 和 `/api/analytics/low-score` 均不存在。

- [ ] **Step 2: 写 eval v3 失败测试**

唯一运行 payload 为：

```json
{"strategy":"retrieval_vector_v2"}
{"strategy":"retrieval_bm25_v2"}
{"strategy":"retrieval_hybrid_v2"}
{"strategy":"retrieval_hybrid_v2_kg_debug"}
```

空对象、`use_kg`、未知 strategy 和多余字段全部拒绝。`analysis.contract_version` 必须是 JSON number `3`；schema 初始化直接删除非 v3 run，不读 v2。指标精确实现 Recall@K、Precision@K、MRR、Hit@1、binary nDCG@K、各 channel Recall、candidate coverage 与 total/vector/BM25/rerank latency。

- [ ] **Step 3: 运行 RED**

Run:

```bash
python -m pytest tests/test_db.py tests/test_admin_server.py -k "analytics or eval or metric" -q
cd web && npm test -- --run evaluation
```

Expected: 当前 analytics/v2 eval 字段与 payload 不符合而失败。

- [ ] **Step 4: 实现 schema、writer、API 和前端**

`record_query_event()` 必须分别接收 request/retrieval latency；`retrieval_latency_ms` 只能来自 service result。`retrieval_meta.py` 只列 v3 strategies/runs。前端策略选择、override、batch summary 和 result panel 全部以 v3 strategy 为 key，不回退另一个 strategy。

- [ ] **Step 5: 运行 GREEN**

Run:

```bash
python -m pytest tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py -k "analytics or retrieval_eval or metric" -q
cd web && npm test && npm run typecheck && npm run lint
```

Expected: PASS。

### Task 6: 真实 PostgreSQL、中文金标与性能门

- [ ] **Step 1: 建立非敏感 fixtures**

`retrieval_v2_corpus.json` 同时包含 FAQ、document parent/child、disabled file/chunk 和 stale rows；`retrieval_v2_cases.json` 固定六类：

```text
chinese_compound
short_term
error_or_model_code
mixed_zh_latin_number
alias
lifecycle_gate
```

每类至少五条，总数至少 30；每条显式给 `question`、`expected_chunk_ids` 或 `expected_source_ids`，不含真实客户材料。

- [ ] **Step 2: 写真实扩展集成测试**

`tests/test_retrieval_postgres.py` 只在显式 `TEST_PG_SEARCH_DATABASE_URL` 下运行，随机隔离 schema，并覆盖：

```text
schema init twice
index definition/readiness
insert/update/delete/disable/re-enable
transaction rollback
VACUUM/REINDEX/restart-visible query
Chinese expected IDs
alias and error/model code
disabled/stale/parent zero leakage
EXPLAIN (ANALYZE, BUFFERS) uses knowledge_chunks_bm25_idx
```

- [ ] **Step 3: 安装并运行真实门**

Run:

```bash
sudo bash scripts/install_pg_search_0_24_2.sh
python -m cyclops init-db
python -m cyclops check-config
TEST_PG_SEARCH_DATABASE_URL="$DATABASE_URL" python -m pytest tests/test_retrieval_postgres.py -q
```

Expected: pg_search 版本精确为 0.24.2、index ready/valid、中文 expected IDs 通过，EXPLAIN 使用 BM25 index。

- [ ] **Step 4: 记录基线和门槛**

用旧 `ILIKE` 仅在离线测试 artifact 中记录一次基线，不把它保留到生产模块。v2 必须满足：总体 Recall@K 不退化；错误码/型号/lifecycle 不退化；中文 BM25 子集的 Recall@K 或 MRR 至少一项严格提高；warm lexical P95 ≤ 50ms。未达标则停止切换并报告，不添加 runtime fallback。

### Task 7: 删除旧运行时并执行完整验证

- [ ] **Step 1: 静态硬切扫描**

Run:

```bash
rg -n "search_knowledge_text|_search_knowledge_text_sql|build_keyword_terms|DOMAIN_KEYWORDS|query_rewrite|RAG_MIN_SCORE|rag_min_score|top_score|min_score|knowledge_chunks_search_idx|to_tsvector\('simple'" cyclops web/src sql tests
rg -n "lexical_backend|search_backend|old_retrieval|fallback.*ILIKE|ILIKE.*fallback" cyclops web/src
```

Expected: 正式代码无命中；测试只能在明确的“removed/absent”静态断言或离线基线 fixture 名称中提及旧符号。

- [ ] **Step 2: 更新项目规格与 change log**

把 `.trellis/spec/backend/cyclops-retrieval-contracts.md`、`cyclops-assistant-contracts.md`、`cyclops-db-contracts.md` 和 frontend evaluation spec 更新到 v2/v3 当前契约；记录 AGPL-3.0 本地使用与外部闭源交付前合规/商业许可门。

- [ ] **Step 3: 完整质量门**

Run:

```bash
uv lock --check
python -m pytest
python -m ruff check .
python -m cyclops check-config
cd web && npm test && npm run typecheck && npm run lint && npm run build
git diff --check
```

Expected: 全部通过，Vite build 同步更新 `cyclops/static/dist`。完成后才进入 Neo4j Unit 2。
