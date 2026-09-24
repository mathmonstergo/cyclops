# Neo4j 对本项目的适配性再评估

调研日期：2026-07-15（Asia/Shanghai）

## 评估问题

本次不是只问“Neo4j 能不能存实体和关系”，而是评估它是否能为本项目带来足够大的产品与工程收益，以抵消第二套数据库、跨库一致性、运维和迁移成本。重点覆盖：

- 文档级知识图谱、evidence 与审核状态机；
- 向量、中文全文/BM25 与图遍历组合的 GraphRAG；
- 后期 3D 图谱探索；
- 多跳、路径、社区和中心性能力；
- 当前 PostgreSQL + pgvector 事务边界能否安全迁移；
- Community / Enterprise / GDS 的许可与运维边界。

评估基于当前未提交工作树、只读数据库审计和 Neo4j 2026.06 当前官方文档/源码。没有修改业务代码、数据库或运行服务。

## 先给结论

Neo4j 对本项目有真实的长期价值，但这个价值主要出现在“图谱成为核心查询能力”之后，而不是当前的审核 CRUD、1–2 跳局部子图或尚未启用的 KG debug 召回上。

推荐目标架构是：

> **PostgreSQL 继续作为唯一业务与审核写模型；Neo4j 在达到引入门槛后，作为可重建、无 PostgreSQL 查询 fallback 的图查询读模型。**

当前不建议把 PostgreSQL 主库或整个知识查询层立即迁到 Neo4j，原因不是 Neo4j 能力不足，而是目前没有 usable 图、没有 KG 评测流量、正式问答全部关闭 KG，也没有 3 跳以上路径或图算法需求。此时直接迁移是在需求价值被证明之前先承担双数据库复杂度。

也不建议永久排除 Neo4j。若后续产品确定需要多跳解释、路径搜索、社区/中心性、vector → graph traversal，或频繁的 3D 渐进探索，Neo4j 会比继续扩张 PostgreSQL 递归 SQL 更自然。届时应通过一次受控 POC 达标后直接切换图查询入口，而不是长期双读。

## 当前项目的真实基线

### 数据与流量

2026-07-15 的只读审计结果：

| 项目 | 当前值 |
|---|---:|
| KG 实体候选 | 5 |
| KG 关系候选 | 2 |
| KG evidence | 0 |
| usable 图节点 / 边 | 0 / 0 |
| FAQ / 导入文件 / 导入切片 | 0 / 0 / 0 |
| 评测 case / run | 0 / 0 |
| query analytics | 0 |

5 条 KG `knowledge_chunks` 全为 `needs_review + embedding pending`，当前没有可检索图事实。唯一 completed 抽取任务历史报告过 7 条 evidence，但来源删除后现存 evidence 已为 0，实体 `source_count` 仍保留 1，进一步说明“删除来源后精确重算并自动 disabled”的 P0 正确性工作应先完成。

### 当前查询并不是重图工作负载

- 子图只允许 1–2 hop、最多 200 边；前端固定 1 hop、40 边。
- KG 检索不是多跳推理，而是 `KG fact 文本命中 → evidence → 原始 FAQ/document child` 的关系型展开。
- 正式助手、CLI、微信和 MCP 全部显式 `use_kg=False`；只有评测 KG debug 可以开启图谱通道。
- 当前 lexical 是前导 `%...%` 的 PostgreSQL `ILIKE`，不是 BM25；它比当前 1–2 hop 子图更可能先成为规模瓶颈。
- 当前实体/关系审核 API 本机中位数约 2.4 ms，对应 SQL 中位数约 0.07–0.09 ms；没有性能故障证据。

因此，当前没有数据可以证明 Neo4j 会改善正式回答质量或延迟。

## Neo4j 能为本项目带来的实质优点

### 1. 原生图遍历、路径和模式查询

Cypher 25 支持 quantified path patterns、可变长度路径、关系类型/方向/属性约束、inline predicate 剪枝和 shortest path。映射到本项目后，可以自然表达：

- 某产品功能经哪些模块、版本和限制最终影响某操作；
- 两个实体之间的 3–10 跳解释路径；
- 只允许特定关系类型、可信度或来源范围的路径；
- 搜索节点后逐步 expand、reveal selected nodes 之间的边；
- 高度节点、环和多个连通分量上的重复探索。

PostgreSQL 递归 CTE 可以完成有限遍历，但随着路径类型、剪枝、最短路径和路径解释规则增加，查询与测试复杂度会上升。Neo4j 的优势不是“边查询一定更快”，而是图模式成为主要产品语言后，表达力和可维护性更高。

### 2. 向量、全文和图遍历可以处于同一查询层

Neo4j 当前原生提供：

- node/relationship vector index，HNSW approximate nearest-neighbor；
- node/relationship full-text index；
- Cypher `SEARCH` 后继续图遍历；
- 2026.01 起 vector index 支持额外 filterable properties；
- 2026.06 支持 scalar/binary quantization、search expansion factor 和更多 HNSW 调优。

官方 `neo4j-graphrag` 1.18.0 提供 `VectorRetriever`、`VectorCypherRetriever`、`HybridRetriever`、`HybridCypherRetriever`、`Text2CypherRetriever` 和 `ToolsRetriever`。其中最有价值的是“先向量/混合召回 chunk，再沿实体、关系、来源和 evidence 扩图”，这比当前独立执行 vector、ILIKE、KG fact 和 evidence expansion 更接近真正的 GraphRAG。

但官方 retriever 不是本项目的无缝替换：当前 `HybridRetriever` / `HybridCypherRetriever` 搜索签名没有 metadata `filters`，而本项目必须实时门禁 FAQ、文档、chunk、embedding、KG owner、关系端点和 evidence。最新原生 `SEARCH ... WHERE` 可解决部分 filterable property 场景，但仍需要自定义查询和严格集成测试，不能直接套 helper 后假定正确。

### 3. Neo4j 的全文检索当前实际使用 Lucene BM25

Neo4j 官方文档将 full-text score 表述为 Lucene query score，没有把可调 BM25 参数作为公共契约；但 2026.06 当前源码可以确认：

- Neo4j `FulltextIndexReader` 使用其 Lucene `IndexSearcher`，没有调用 `setSimilarity()`；
- Neo4j 2026.06 依赖 Lucene 10.4.0；
- Lucene 10.4.0 `IndexSearcher` 默认 similarity 是 `new BM25Similarity()`。

因此，在当前版本实现上，Neo4j full-text 的默认相关性算法确实是 BM25，而不是本项目现在的固定权重 `ILIKE`。

这也回答了“BM25 前是否要保存分词”：

> 应用不需要另外保存一列分词结果。Neo4j/Lucene 的 analyzer 在写索引和查询时分词，token、词频、文档频率与长度统计保存在 Lucene 倒排索引内部。原文和 embedding 仍作为节点属性保存。

Neo4j 当前内置 `cjk` analyzer，官方源码说明它对中日韩文本做 normalize、case-fold、生成 bi-gram，并过滤 stop words。它能让中文文本进入倒排/BM25，但 **CJK 双字切分不等于高质量中文语义分词**：

- 对错误码、型号、短产品名和连续中文短语通常有用；
- 对领域复合词、同义表达和精确词边界未必优于 jieba/IK/自定义词典；
- Neo4j 支持 custom analyzer，但自定义中文 analyzer 意味着维护 Java plugin、版本兼容和部署；
- 另一种做法是应用预分词后写独立检索属性，但那时仍需保存 canonical 分词产物并统一索引/查询 tokenizer。

所以 Neo4j 可以提供“真正 BM25 的现成底座”，却不能免除真实中文金标集上的 tokenizer 评测。

### 4. GDS 给后续图谱产品提供成熟算法层

Neo4j Graph Data Science 当前覆盖：

- centrality：PageRank、Betweenness、Closeness、Degree、HITS 等；
- community detection：Louvain、Leiden、Label Propagation、WCC/SCC 等；
- path finding：Dijkstra、A*、Yen、BFS/DFS、Steiner tree、maximum flow 等；
- similarity、KNN、node embeddings、link prediction 和 ML pipeline。

这可支持后续的：

- 文档/产品知识社区摘要；
- 核心实体与高影响节点识别；
- 多跳问题的候选路径和解释；
- 3D 图谱的社区着色、聚类和布局辅助。

GDS 不是零成本在线查询功能。它把 projected graph 和算法状态放在 Java heap；官方建议先用 `.estimate` 估算，并明确大图分析需要显著堆内存。GDS Community Edition 最大并发为 4，Enterprise Edition 才不限制并发并需要有效 license key。对当前本地单机、小图而言 CE 足够；若未来把 GDS 作为生产高并发分析服务，需要重新评估资源和许可。

### 5. 3D 图谱的数据访问会更自然，但渲染仍是前端职责

Neo4j 对 3D 的直接收益是：搜索实体、渐进展开、路径查找、补边、社区和中心性这些后台查询更自然；Neo4j Bloom 也提供成熟的 search-first、bounded scene、expand 和 inspector 产品范式。

但引入 Neo4j **不会自动得到 3D 页面**：

- Bloom 主要是图探索产品参考，不是本项目的 3D React 渲染层；
- Three.js、`react-force-graph-3d`、标签、镜头、选中、WebGL 资源和可访问性仍要实现；
- 浏览器 force layout 和 draw call 性能不会因为数据库换成 Neo4j 自动消失；
- 当前 1-hop/40-edge bounded scene 用 PostgreSQL 同样可以供数。

因此“未来要 3D”是 Neo4j 的加分项，不是现在换库的充分条件。

### 6. 当前数据量小，未来若确定采用，迁移窗口确实较好

当前没有 usable 图和业务文档，数据搬迁成本最低。若产品已经明确 Neo4j 最终会成为图查询层，现在设计稳定 ID、projection contract、outbox 和查询接口，比数据量扩大后再补要便宜。

不过“现在设计边界”不等于“现在必须运行第二套数据库”。可以先让当前 schema、文档级 snapshot、来源失效和评测集稳定，再用同一稳定 ID 做 POC；这不会形成旧兼容路径。

## Neo4j 不会替我们解决的问题

- 文档 Map / entity resolution / relation Reduce 的抽取质量；
- evidence 是否精确、来源删除后的自动 disabled 和 `source_count` 重算；
- 人工审核 revision、409 CAS、实体后关系的锁序；
- FAQ/文档/切片/embedding 生命周期；
- 3D WebGL 渲染与可访问性；
- 中文 tokenizer 的领域质量；
- KG 是否真的改善客服问题的 Recall@K、MRR、Top1 和答案正确率。

这些都必须先通过当前 P0 工作和评测建立可信基线。

## 最大结构风险：当前 PostgreSQL 是统一事务边界

当前 PostgreSQL KG 不只是三张图表。一个事务同时协调：

```text
FAQ 或 import file/chunks 来源指纹
→ entity ID 升序锁定与 upsert
→ relation ID 升序锁定与 upsert
→ review revision / status
→ evidence
→ knowledge_chunks 检索投影
→ extraction job completed
```

来源删除/重解析也在同一事务内处理来源、KG owner、evidence 和检索投影。Neo4j 自己支持 ACID，但 PostgreSQL 与 Neo4j 之间没有本项目可用的分布式原子事务。若把 KG 审核写入 Neo4j、把来源和 `knowledge_chunks` 留在 PostgreSQL，会出现：

- 来源已删除但 Neo4j 事实尚未禁用；
- Neo4j confirm 成功但 PostgreSQL 检索投影失败；
- 两边 revision、evidence 或 endpoint status 分叉；
- 为修复分叉不得不增加 saga、重试、幂等、checkpoint 和 reconcile。

还有一个图模型细节：本项目的“关系事实”拥有稳定 ID、审核 revision 和自己的 evidence。若把它直接建成 Neo4j relationship，relationship 不能再作为其他 relationship 的端点，关系 evidence 会很别扭。若 Neo4j 成为 canonical store，应把关系事实实体化为 `(:KgRelationFact)-[:HEAD|TAIL|SUPPORTED_BY]->(...)`，这会扩大迁移范围。

因此不推荐让 Neo4j 直接成为当前 KG 审核主存储。

## 三种可行架构

### A. 继续只用 PostgreSQL + pgvector

做法：完成文档级 KG、来源治理、中文 lexical/BM25 独立评测；子图继续使用 bounded recursive SQL。

优点：

- 单事务和单数据库最简单；
- 当前工作负载已足够快；
- 没有投影延迟、第二套备份和故障模式；
- 最适合先验证 KG 是否改善客服答案。

缺点：

- 复杂多跳、路径和图算法继续增长时开发成本高；
- vector/full-text/graph 仍是多个查询通道；
- 若图谱最终成为核心，后续仍需迁移或引入图读模型。

适用：未来 3–6 个月仍以 1–2 hop 展示、审核和 baseline/KG debug 为主。

### B. PostgreSQL 唯一写模型 + Neo4j 可重建图查询模型（推荐目标）

做法：PostgreSQL 保留 FAQ、文档、任务、KG 审核、evidence、revision 和 `knowledge_chunks`；同一事务写 transactional outbox。Projector 从已提交 PostgreSQL 最终状态幂等 reconcile，只把 usable entity/relation（必要时加 source stub）投影到 Neo4j。`/api/kg/subgraph`、路径和后续图分析在切换日后只读 Neo4j。

关键约束：

- PostgreSQL 是唯一 command model，Neo4j 是明确 query model，不是双写双真源；
- 只用稳定业务 ID，不暴露 Neo4j 内部 element ID；
- backfill 使用 snapshot + outbox high-water mark + replay；
- 切换前 checksum、排空 outbox并建立 barrier；
- 切换后删除 PostgreSQL subgraph 运行路径；
- Neo4j 不可用或投影落后时明确 fail closed，不静默查 PostgreSQL fallback；
- text/vector RAG 初期继续用 PostgreSQL，避免投影延迟污染正式答案。

优点：

- 获得图查询、路径、GDS 和 3D 后台能力；
- 不破坏当前审核/来源事务；
- Neo4j 全库可由 PostgreSQL 重建；
- 引入范围可控，符合“唯一当前契约、无 fallback”。

缺点：

- 仍需运行第二套数据库、outbox、projector、lag/reconcile 和集成测试；
- 图是最终一致的；
- 在当前 0 usable 数据和 0 图流量下，短期收益有限。

适用：已经确认图探索/多跳会成为核心，但仍要求 PostgreSQL 审核真源。

### C. PostgreSQL 业务写模型 + Neo4j 统一知识查询层

做法：FAQ/导入/任务/评测继续在 PostgreSQL；chunk、embedding、full-text、entity、relation 和 source/evidence projection 进入 Neo4j，正式检索一次完成 vector + BM25 + graph traversal。

优点：

- 最能发挥 Neo4j GraphRAG 的组合价值；
- 可以减少当前 vector、lexical、fact、evidence 多次往返；
- 中文 CJK BM25、向量过滤和图扩展位于同一读库。

缺点：

- `knowledge_chunks` 的 FAQ/document live status、parent/child、embedding stale 和 source disable 都要投影；
- 投影落后时可能泄漏被删或过期知识，必须用 watermark fail closed 或 PostgreSQL 回查；
- 官方 Hybrid retriever 不能直接表达全部当前门禁；
- 排名语义、RRF、parent context、analytics 和评测 contract 都要重新验证；
- 范围接近重写检索存储层，当前没有质量或性能证据支持。

适用：方案 B 已运行稳定、KG 已默认参与正式检索、真实评测证明 graph expansion 有价值，并且确实需要统一读层时。

## 对比矩阵

| 维度 | A：仅 PostgreSQL | B：Neo4j 图读模型 | C：Neo4j 统一知识读层 |
|---|---|---|---|
| 当前实现/运维复杂度 | 最低 | 中 | 最高 |
| 现有事务正确性 | 原样保留 | 原样保留 | 需跨库 fail-closed 协议 |
| 1–2 hop / bounded 3D | 足够 | 很好 | 很好 |
| 3–10 hop / 路径模式 | 逐步变复杂 | 最自然 | 最自然 |
| 社区/中心性/GDS | 需另做 | 原生生态 | 原生生态 |
| 真正 BM25 | 需另选 PG 方案 | RAG 暂无变化 | Lucene BM25 + analyzer |
| vector + graph 一次查询 | 否 | 图层首版不做 | 是 |
| 当前工作负载投入产出 | 最好 | 偏低 | 最低 |
| 可重建性 | 不适用 | 高 | 高，但投影范围大 |
| 未来图谱核心化上限 | 中 | 高 | 最高 |

## 推荐决策与引入门槛

### 推荐

本项目现在不“换掉 PostgreSQL”，也不把 Neo4j 设为 KG 审核主库。

推荐把架构方向从“明确不引入 Neo4j”调整为：

> **当前 P0 仍用 PostgreSQL 完成；Neo4j 作为条件触发的方案 B 目标架构。先做隔离 POC，满足门槛后一次性切换图查询 API；方案 C 只保留为更后期的演进。**

这既承认 Neo4j 的正向价值，也避免仅因为要做 3D 或 BM25 就提前引入第二套权威数据路径。

### 建议触发 POC 的产品条件

满足至少一项：

- 产品确认需要 3 跳以上受约束路径、最短路径或多路径解释；
- 3D 图谱成为高频核心工作台，而不是后期辅助视图；
- 需要社区、中心性、PageRank 或图摘要；
- KG debug 在真实金标集上证明改善，并计划进入正式问答；
- PostgreSQL 在目标图规模和优化后仍不能满足明确 SLA。

### POC 数据与指标

系统性能集使用确定性合成图，不写业务库：

| 档位 | 节点 | 边 | Evidence | Knowledge rows |
|---|---:|---:|---:|---:|
| S | 10k | 50k | 150k | 100k |
| M | 100k | 1m | 3m | 2m |
| L，仅路线图需要时 | 1m | 10m | 30m | 20m |

必须比较：

- 1-hop 40/200、2-hop 200、3–6 hop shortest/type-constrained paths；
- cold/warm cache，并发 1/8/32 的 P50/P95/P99；
- node/edge/path 结果集合、方向、类型、usable-only、missing/isolated、truncated 必须完全一致；
- outbox 重放、重复消费、删除 tombstone、断点恢复和全量重建；
- 投影延迟期间不得泄漏 stale/disabled/review 数据；
- 200–500 条脱敏人工问题比较 baseline/KG debug 的 Recall@K、MRR、Top1、Precision/nDCG 和 graph expansion precision；
- 中文 lexical 单独覆盖错误码、型号、产品复合词、同义词、标点、长问题和 SOP。

候选通过线：

- 正确性和禁用数据泄漏测试 100% 通过；
- 目标高级图查询 P95 至少约 2 倍优于优化后的 PostgreSQL，或 PostgreSQL 无法合理表达目标能力；
- 1-hop 交互查询建议 P95 ≤ 100 ms、P99 ≤ 250 ms；2-hop/200 edges 建议 P95 ≤ 200 ms、P99 ≤ 500 ms；
- 投影延迟和重建时间满足产品明确 SLA；
- KG 检索质量有可重复的真实收益，而不是只看数据库跑分。

## 版本、许可与运维判断

- Neo4j Community Edition 是单实例可用的完整 Cypher/ACID 图数据库，适合当前本地工具；Community 只提供离线 dump/restore。
- clustering、failover、online backup、RBAC/LDAP 等属于 Enterprise Edition。若未来暴露为关键生产服务，需要计入订阅或 Aura 成本。
- GDS Community Edition 最大并发 4；Enterprise 解锁更高并发，需要 license key。
- GDS projected graph 和算法状态占 Java heap，不能与普通 OLTP 内存预算混为一谈。
- Neo4j 2026.01+ 的 vector filter 和 2026.06 新索引能力很有吸引力，但 5.26 才是当前 LTS。若 POC 依赖最新 `SEARCH ... WHERE`，必须明确“最新能力 vs LTS 稳定性”的版本决策。
- 对当前本地单人使用，CE 单实例不是阻塞项；对未来生产 HA，许可和第二套备份/监控是显著成本。

## 对现有计划的影响

在用户确认前不修改业务代码。若确认本建议：

1. 当前先完成 durable parsing、来源失效、文档级 KG、批量操作和 Tooltip P0；
2. 同步建立真实 KG/检索评测集；
3. 把 Neo4j 方案 B 拆为独立 POC 与后续迁移任务；
4. POC 不接正式 API、不双写业务路径；
5. 达标后再新建 schema/API 变更目录，设计 outbox、projection schema、硬切换与无 fallback 契约；
6. 3D 前端仍采用 Three.js + `react-force-graph-3d`，数据接口可以先保持稳定 ID + bounded subgraph，避免渲染层绑定具体数据库。

## 官方与源码来源

- Neo4j Cypher variable-length paths：https://neo4j.com/docs/cypher-manual/current/patterns/variable-length-paths/
- Neo4j full-text indexes：https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/full-text-indexes/
- Neo4j vector indexes：https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/
- Neo4j GraphRAG retrievers：https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html
- Neo4j GraphRAG Python 源码（当前 1.18.0）：https://github.com/neo4j/neo4j-graphrag-python
- Neo4j 2026.06 `CJK` analyzer 源码：https://github.com/neo4j/neo4j/blob/2026.06/community/lucene-index/src/main/java/org/neo4j/kernel/api/impl/schema/fulltext/analyzer/providers/CJK.java
- Neo4j 2026.06 full-text reader 源码：https://github.com/neo4j/neo4j/blob/2026.06/community/lucene-index/src/main/java/org/neo4j/kernel/api/impl/schema/fulltext/FulltextIndexReader.java
- Neo4j 2026.06 Lucene 版本：https://github.com/neo4j/neo4j/blob/2026.06/pom.xml
- Lucene 10.4.0 默认 BM25：https://github.com/apache/lucene/blob/releases/lucene/10.4.0/lucene/core/src/java/org/apache/lucene/search/IndexSearcher.java
- Neo4j Graph Data Science algorithms：https://neo4j.com/docs/graph-data-science/current/algorithms/
- GDS system requirements / CE concurrency：https://neo4j.com/docs/graph-data-science/current/installation/System-requirements/
- GDS memory estimation：https://neo4j.com/docs/graph-data-science/current/common-usage/memory-estimation/
- Neo4j CE/EE 功能边界：https://neo4j.com/docs/operations-manual/current/introduction/
- Neo4j backup/restore 边界：https://neo4j.com/docs/operations-manual/current/backup-restore/
- Neo4j clustering：https://neo4j.com/docs/operations-manual/current/clustering/

## 本地证据路径

- KG schema：`sql/001_init.sql`
- KG 写事务与子图：`cyclops/db/kg.py`
- FAQ / document 来源事务：`cyclops/db/faq.py`、`cyclops/db/imports.py`
- 当前 pgvector / ILIKE：`cyclops/db/knowledge.py`
- 混合检索与 RRF：`cyclops/retrieval.py`
- canonical result model：`cyclops/db/models.py`
- 当前 PRD：`.trellis/tasks/07-07-knowledge-graph-next/prd.md`
