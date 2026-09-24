# 统一检索、中文 BM25 与 Neo4j 方案 B 边界审计

调研日期：2026-07-15（Asia/Shanghai）

## 结论

当前检索不能称为“已经完善”，BM25 也没有修复或实现。

已经基本正确的是统一入口、canonical 数据模型、实时来源状态门禁、direct child / parent context 分离、RRF 集中融合，以及 KG 只在评测 debug 中显式开启。尚未闭合的是中文 lexical 召回、可命中的倒排索引、最终分数语义、rerank 完整性、真实 PostgreSQL 执行计划与业务金标评测。

方案 B 的唯一边界应保持为：PostgreSQL 是业务、审核与正式 RAG 的唯一事实源；Neo4j 只保存可重建的 usable entity/relation 图读模型。FAQ/document chunk 的 BM25 必须留在 PostgreSQL。把 chunk、全文和正式 lexical 查询投影到 Neo4j 会把方案 B 扩成方案 C，不应在实现中静默发生。

## 当前运行链路

```text
active aliases
→ query terms
→ query embedding
→ pgvector direct candidates
→ weighted ILIKE direct candidates
→ optional KG fact ILIKE + live evidence expansion（仅评测 debug）
→ RRF
→ optional rerank
→ final direct top-k
→ optional parent context backfill
```

所有正式入口都已使用 `HybridRetrievalService`，并显式 `use_kg=False`。只有 retrieval evaluation 可以显式 `use_kg=True`。

主要代码：

* `cyclops/retrieval.py:125`：唯一混合检索编排。
* `cyclops/db/knowledge.py:323`：pgvector 检索与实时来源门禁。
* `cyclops/db/knowledge.py:372`：FAQ/document weighted `ILIKE`。
* `cyclops/db/kg.py:1755`：KG fact weighted `ILIKE`。
* `cyclops/db/kg.py:1796`：KG fact 精确展开到实时 FAQ/document evidence。
* `.trellis/spec/backend/cyclops-retrieval-contracts.md`：当前 v1 契约。

## 已正确实现的部分

* CLI、RAG、微信、MCP、RagTool 和 Admin assistant 已统一到同一检索服务；旧 FAQ-only 检索和 `RetrievedDocument` 已删除。
* vector 与 lexical 都只允许 FAQ/document；FAQ 同时检查实时 `status` 与 embedding ready，文档检查文件/切片未禁用，只允许 direct child。
* RRF 按 canonical knowledge row ID 去重，并保存 vector、keyword、KG 三路诊断。
* parent 只在最终排序后回填，不进入 RRF、top-k、评测命中或 analytics chunk ids。
* KG fact 不直接成为回答候选，只通过精确 evidence 回到实时可用的原始 FAQ/document child。
* rerank 调用失败、空返回或非法 index 时保持 RRF 顺序，不回退旧检索路径。

## 尚未完善的部分

### 1. 中文 lexical 不是 BM25

`cyclops/db/knowledge.py:372` 和 `cyclops/db/kg.py:1755` 仍对标题、`search_text`、正文执行带前导通配符的固定加权 `ILIKE`。运行 SQL 中没有 `@@`、`ts_rank`、`USING bm25` 或 `pdb.score`。

`sql/001_init.sql:148` 虽然创建了 `GIN(to_tsvector('simple', search_text))`，但运行查询完全没有引用该表达式；该索引不会服务当前 lexical。PostgreSQL `simple` 配置也不能提供合格的连续中文分词。

`build_keyword_terms()` 只生成错误码、人工 alias、固定领域词和拉丁 token。普通中文问题可能没有任何 term，只剩整句子串匹配。

### 2. `ILIKE` 通配符语义有缺口

当前 `%{query}%` 没有把用户输入中的 `%` 和 `_` 转义成 LIKE literal。它不是 SQL 注入，但会把这两个字符解释为通配符，造成意外宽匹配和重型扫描。现有“percent escape”测试只验证 psycopg SQL 字符串中的 `%%`，没有验证用户输入。

### 3. 最终 score 语义混乱

RRF 先加入 vector 候选，因此双通道候选的 `candidate.document.score` 往往仍是 vector raw score；keyword-only 或 KG-only 又可能保存另一种 raw score。rerank 只使用 provider 返回的 index 改顺序，没有保存 `relevance_score`。

CLI、MCP、RagTool、Admin 和 analytics 仍把 `document.score` 当作 `top_score`。因此当前“最高分”可能是向量相似度、固定 lexical 分或 KG 分，既不是 RRF 分，也不是 rerank 分，不能用于统一阈值或置信度解释。

### 4. rerank 不能保证改善排序

`rerank_candidates()` 在候选数小于或等于 `top_k` 时完全跳过 provider。即使两条候选需要互换顺序，也不会重排。现有测试把这一行为固定成预期，但它只节省调用，不能满足“rerank 用于最终排序”的语义。

### 5. 性能和一致性尚未验证

* weighted `ILIKE` 的成本近似随 eligible rows × query terms × fields 增长，且当前没有 `pg_trgm` 索引。
* baseline answer 依次执行 aliases、vector、lexical、parent 四次数据库读取；非 Admin 入口未统一使用连接池，各通道也不共享同一快照。
* HNSW 是覆盖全部 `knowledge_chunks` 的通用索引，运行时再过滤状态和 child；没有真实规模下的 ANN post-filter recall / underfill 验证。
* parent context 被人工赋值 `score=1.0` 后进入 prompt/source DTO，虽然不参与排名，仍会造成“高置信度”错觉。
* `rag_top_k`、`rag_min_score`、`rerank_input_size` 缺少有效范围校验。

### 6. 评测不足

现场 PostgreSQL 为 16.14，仅安装 `plpgsql, vector`；可用相关扩展只有 `pg_trgm, vector`。当前 retrieval eval case/run 和 query analytics 均为 0。

现有评测只覆盖 Recall@K、MRR、Hit@1，没有 nDCG@K、Precision@K、各召回通道独立 recall、候选覆盖率和检索延迟。数据库测试大多验证 SQL 字符串，没有在真实 PostgreSQL 上执行 lexical 查询、`EXPLAIN (ANALYZE, BUFFERS)` 或索引命中。

## BM25 是否需要应用保存分词

BM25 必须有一致的索引端与查询端 tokenizer，并依赖 token frequency、document frequency 和 document length。但应用通常不需要额外保存一列可见的“分词字符串”。

推荐模式是：

```text
knowledge_chunks.search_text 保存 canonical 原文
→ pdb.jieba 在建索引时分词
→ token/postings/TF/DF/doc length 存在 BM25 倒排索引内部
→ 查询文本使用同一 tokenizer 解析
```

只有选择 VectorChord-BM25 一类显式 sparse-vector 方案时，才会在业务表中增加 `bm25vector` 列保存 token ID → TF。那不是 BM25 的普遍要求，而是具体扩展的数据模型。

## PostgreSQL 中文 lexical 方案对比

### A. ParadeDB `pg_search + pdb.jieba`（推荐 POC）

* 真正 BM25，使用 `USING bm25` 与 `pdb.score`。
* 官方内置 `pdb.jieba`，明确用于中文词边界；token、TF/DF 和长度统计由索引内部维护。
* 2026-07-10 当前版本为 v0.24.2；支持 PostgreSQL 15+，提供 PG16 / Ubuntu 24.04 预编译包。
* 需要 superuser、`shared_preload_libraries='pg_search'`、重启和 `CREATE EXTENSION pg_search`。
* Community 许可证为 AGPL-3.0；生产/分发场景必须先完成许可证确认，或选择 Enterprise 商业许可。
* 每张表只能有一个 BM25 index，需一次性把 key、搜索字段和用于 filter/sort 的字段纳入索引设计。

判断：在当前 PG16 环境中唯一同时具备成熟 BM25、官方中文 tokenizer 和可直接安装包的候选，适合作为首选隔离 POC。

### B. `pg_textsearch + zhparser`

* 真正 BM25，支持 k1/b，许可证为 PostgreSQL License。
* 接受 PostgreSQL text search configuration；官方 README 已给出 `zhparser` 中文配置示例。
* 2026-06-23 当前版本为 v1.3.1，只支持 PostgreSQL 17/18，也需要 preload 与重启。

判断：许可证更宽松，但当前 PG16 不能使用；采用它意味着先升级数据库 major 并额外验证 zhparser。列为第二候选，不推荐为了 BM25 同时扩大到数据库 major 迁移。

### C. `pg_trgm` indexed substring

* 可直接安装，支持 GIN/GiST 加速 `LIKE/ILIKE` 和字符串相似度；中文连续字符串通常可以受益。
* 不保存 corpus DF/IDF，也没有 BM25 文档长度归一化，因此不是 BM25。

判断：可作为现有 ILIKE 的离线性能基线，但不能作为新 `retrieval_hybrid_v2` 的最终契约。硬切后不保留该运行路径。

### 不推荐：VectorChord-BM25 + pg_tokenizer

它能提供严格 BM25 和 Jieba 示例，但需要显式 `bm25vector`、两个扩展与额外 preload；上游 README 明确主要只测试过英文，中文与事务行为成熟度不如首选。只保留为实验候选。

### 为什么不使用 PostgreSQL 内置 FTS + zhparser 冒充 BM25

内置 FTS 可以通过 zhparser 获得中文 token，也能用 GIN 加速 `@@`。但 PostgreSQL 官方说明 `ts_rank/ts_rank_cd` 不使用任何 corpus 全局信息，因此没有 BM25 所需的全局文档频率口径，不能命名为 BM25。

## 推荐的 `retrieval_hybrid_v2` 唯一契约

1. PostgreSQL `knowledge_chunks` 继续是 FAQ/document direct candidate 的唯一检索投影；原文保存在 `search_text`。
2. 使用 `pg_search + pdb.jieba` 建唯一 BM25 index，lexical 结果返回 `pdb.score` 排名；删除 weighted `ILIKE` SQL 和闲置 `simple` GIN，不保留 backend 开关或 fallback。
3. BM25、pgvector 与可选 KG evidence expansion 只通过各自 rank 进入 RRF，不直接比较异构 raw score。
4. rerank provider 已配置且候选至少两条时执行，即使候选数不大于 `top_k`；保存命名明确的 `rerank_score`。
5. 删除含义不确定的统一 `score/top_score` 置信度解释。wire/analytics 明确记录 `fused_score`、`rerank_score`、各 channel raw score 与 score kind；低置信策略不再拿异构分数共用一个阈值。
6. parent context 的 score 改为无排名值，不向模型或 UI伪装为 `1.0`。
7. 为 top-k、vector threshold 与 rerank input size 增加范围校验。
8. 为评测新增 lexical-only / vector-only / full hybrid 诊断、nDCG@K、Precision@K、channel recall、P50/P95 latency；至少建立一批真实中文金标后再切换。
9. BM25 查询必须在 `LIMIT` 前满足 FAQ status/embedding、document file/chunk enabled、direct child 等实时门禁。先用真实 PG `EXPLAIN` 验证 join/filter pushdown；若不能保证 top-k 不欠量，再在同一 PostgreSQL 事务内维护明确的检索资格投影，不允许先取 top-k 后在应用层丢弃。

## Neo4j 方案 B 的唯一投影边界

v1 只投影：

```text
(:KgEntity)-[:KG_RELATION]->(:KgEntity)
```

不投影 FAQ、document、chunk、evidence、embedding、BM25、任务、审核记录、评测和设置。PostgreSQL 仍负责 vector、BM25、RRF、rerank、parent context 和 KG fact → live evidence expansion。

方案 B 需要 transactional outbox、未投影事件扫描、checkpoint、幂等 reconcile、全量 rebuild/checksum，以及 Neo4j 不可用或投影落后时 HTTP 503 fail closed。切换图 API 时删除 PostgreSQL `get_kg_subgraph()` 运行路径，不增加 backend switch、双读或 fallback。

在接 outbox 前必须先修复两项 PostgreSQL 图状态语义：

* `source_count` 按实时有效来源精确重算，不能继续使用 `GREATEST` 保留旧计数。
* 来源失效后，零有效 evidence 的事实进入 `disabled`；仍有其他来源的共享事实进入 `needs_review`。

## 切换门槛

### BM25

* 锁定 PG major、`pg_search` 精确版本、部署方式与许可证结论。
* 中文金标覆盖领域复合词、别名、1–2 字词、错误码、型号和中英数字混排；错误码/型号不得回退。
* FAQ/document 生命周期、事务 rollback、重启、VACUUM、reindex、备份恢复后零禁用数据泄漏。
* 真实规模与 10× 预估规模执行 `EXPLAIN (ANALYZE, BUFFERS)`，确认 BM25 index、live filter 和 top-k 不欠量。
* 离线 A/B 达标后一次硬切；正式代码删除 weighted ILIKE 和闲置 GIN，不保留运行时旧路径。

### Neo4j

* PostgreSQL 与 Neo4j usable node/edge ID、属性和 checksum 一致。
* pending outbox 排空；重复消费不产生重复边；投影崩溃点均能幂等恢复。
* 来源禁用/删除后图 API 不返回旧事实；lag、rebuild、Neo4j 故障均明确 503。
* 达标后一次切换子图/路径 API 并删除 PostgreSQL runtime graph query。

## 官方资料

* ParadeDB BM25 index：https://docs.paradedb.com/documentation/indexing/create-index
* ParadeDB Jieba：https://docs.paradedb.com/documentation/tokenizers/available-tokenizers/jieba
* ParadeDB score：https://docs.paradedb.com/documentation/sorting/score
* ParadeDB extension deployment：https://docs.paradedb.com/deploy/self-hosted/extension
* ParadeDB release v0.24.2：https://github.com/paradedb/paradedb/releases/tag/v0.24.2
* pg_textsearch：https://github.com/timescale/pg_textsearch
* VectorChord-BM25：https://github.com/supervc-stack/VectorChord-bm25
* PostgreSQL text ranking：https://www.postgresql.org/docs/current/textsearch-controls.html#TEXTSEARCH-RANKING
* PostgreSQL pg_trgm：https://www.postgresql.org/docs/current/pgtrgm.html

## 本次验证

* 生产引用扫描确认所有正式入口只使用 `HybridRetrievalService`，并显式关闭 KG。
* 定向检索测试共通过 154 项：入口/RAG/RagTool/CLI/MCP/微信 67 项，SQL/筛选 61 项，Admin 检索/评测 26 项。
* 只读数据库查询确认 PostgreSQL 16.14、已安装 `plpgsql,vector`、当前仅可用 `pg_trgm,vector`，评测 case/run 与 analytics 均为 0。
* 本次调研没有修改业务代码、数据库数据或运行服务。
