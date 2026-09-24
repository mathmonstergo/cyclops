# 知识图谱二期：闭环修复与检索校正

## Goal

在不让未审核内容进入正式问答的前提下，完成知识图谱、文档流程和资源管理闭环；随后以两个独立实施单元引入 PostgreSQL `pg_search + pdb.jieba` 的 `retrieval_hybrid_v2` 与 Neo4j 方案 B 图读模型。最终只保留唯一当前契约，不保留 weighted `ILIKE`、PostgreSQL 图查询 fallback 或双真源。

## 初始审查基线（实现前历史记录）

* 知识图谱 MVP 已实现实体/关系候选、审核表、`knowledge_chunks` 投影、局部子图 API 和管理页。
* 默认智能问答明确排除 `kg_entity` / `kg_relation`，KG 仅允许通过评测接口的 `use_kg=true` 显式启用。
* 当前前端评测请求固定发送空 payload，没有入口传入 `use_kg=true`，因此已确认 KG 无法从现有 UI 验证。
* KG 抽取失败仍以 HTTP 成功响应返回 `status=failed`；KG 页面和文档切片入口都会显示成功 toast。
* 当前库只读检查：7 个 usable 实体、6 个 needs_review 关系、7 个 usable 实体投影；10 个抽取任务中 1 个 completed、9 个 failed。
* 当前库暂未发现 usable 关系连接非 usable 端点，也未发现 usable KG 事实完全失去有效证据来源。
* 代码没有阻止关系在头尾实体未确认时进入 usable，也不会在实体停用时同步停用相关关系投影。
* KG 检索只检查投影自身状态，不检查证据对应 FAQ、文档或切片当前是否仍可用。
* 当前所谓关键词召回不是 BM25，而是 `ILIKE` 子串固定权重；schema 中的 `to_tsvector('simple', search_text)` GIN 索引没有被运行时查询使用。
* 文档切片生成 embedding 时，无 child 的切片只写 parent，但直接向量/关键词 SQL 排除所有 document parent，导致该类文档不可召回。
* FAQ 编辑会把 `faq_documents.embedding_status` 标成 stale，但不会同步统一知识单元，后台助手仍可能召回旧正文和旧向量。
* 后台助手使用统一混合检索；CLI、微信和 MCP 仍使用旧 FAQ 单路向量检索，入口语义不一致。
* 2026-07-14 实现前基线曾通过后端 303 tests、Ruff、配置检查以及前端 37 tests、lint、build；这些数字不代表本轮最终实现已经验收。

## 已完成阶段基线（2026-07-15，已验证）

* KG 抽取已收敛为唯一异步契约：显式 `source_type + source_id`，POST 返回 queued，后台执行，GET 轮询 completed/failed；不保留同步包装、来源推断或通用状态更新旁路。
* KG 模型输出、来源 fingerprint、证据 locator、审核确认、端点状态、重复抽取失效和来源删除/重解析失效均已按严格契约实现。
* KG confirm 唯一请求体为 `{"expected_revision": 正整数}`；列表返回 `review_revision`，锁内版本冲突映射 HTTP 409，禁止空 body、默认 revision 或 id-only 入口。
* KG 来源替换采用全局 entity→relation 两阶段锁序：实体全集按 ID lock/upsert，实体阶段后重读 incident relations，再按关系全集 ID lock/upsert；lock-only endpoint 不进入状态、revision 或投影更新，也不使用 deadlock retry。
* KG fact 已改为只做诊断和证据扩展：只通过精确 `kg_evidence` 映射展开到实时可用的 FAQ 或文档 direct child，最终候选和评测指标不再使用合成 KG chunk。
* 子图 API 已固定 usable-only，区分 404 的缺失/不可用中心、200 isolated 和 connected；不接受可切换 `status`。
* 评测已采用 v2 唯一契约：payload 只能是 `{}` 或 `{"use_kg": true}`，运行保存 `contract_version=2`，列表只返回按策略独立的 `latest_runs`，前端 override、汇总和诊断均按 case + strategy 隔离。
* FAQ 与文档管理入口已直接提供 KG 抽取；旧的手填 FAQ ID 抽取路径已删除，关系确认按钮同时检查证据与头尾实体状态。
* 文档知识身份已固定为文件 ID、来源切片 ID、知识行 ID 三层；每个来源切片至少生成一个 direct child，parent 只作为上下文，不参与直接候选、指标或 analytics。
* FAQ stale、假设问题 stale、文档 parent/child 召回与统一知识状态已完成校正。
* 后台助手、CLI、微信和 MCP 已统一到 `HybridRetrievalService` 与 `RetrievedKnowledgeChunk`；`RetrievedDocument`、FAQ-only search 和内部属性别名已删除，不保留兼容/fallback 路径。
* `parse_progress` 已固定为受数据库 CHECK 约束的 JSON object；文件级 chunker 必须读取持久化 canonical 枚举，后端和 UI 均不再为缺失/未知值静默回退 `naive`。
* 当前 lexical 仍是 PostgreSQL `ILIKE`。真正中文 BM25 本轮未实现，也没有引入搜索扩展、分词依赖或独立搜索服务。
* 本轮代码实现与契约文档已完成同步。最终门通过后端 567 passed / 4 skipped、Ruff、配置检查，前端 71 passed、typecheck、lint、build，真实 PostgreSQL 17 + pgvector 4 passed；最新 `dist` 的浏览器 smoke 也验证了 revision=7 原样提交、409 错误提示和无成功状态漂移。

## 2026-07-15 后续需求探索

### 已确认事实与产品决定

* 文档解析任务必须由后端持续推进；离开页面或关闭浏览器后仍要查询 MinerU、更新进度并完成落盘，前端只观察持久任务状态。
* 文档列表不新增进度列；解析中时在文件名称右侧紧贴小型进度条，抽屉保留阶段、页数和百分比详情。
* 删除文档/FAQ 来源后不硬删除 KG owner 历史：失去全部 evidence 的实体、关系及投影自动转为 `disabled`；仍有其他来源 evidence 的共享事实保留并退回 `needs_review`；`source_count` 必须精确重算。
* 文档删除成功后立即刷新 KG entity、relation 和 subgraph 查询缓存。
* 用户确认“只要有列表就得支持多选操作功能”采用推荐边界：所有具备合法共同批量动作的可变业务资源列表必须支持多选；导航、只读引用、候选、诊断和设置表面不增加无意义复选框。
* 第一阶段统一覆盖文档、FAQ、KG 实体、KG 关系和评测用例；检索别名、文档切片和会话进入有明确动作的专用维护模式。
* 用户要求知识图谱后期提供 3D 点线关系可视化，并参考成熟权威实现；当前先完成技术调研并预留数据/API 边界。

### 当前缺口

* 当前 `/api/import/files` 只读取数据库快照，真正查询 MinerU、回写进度和完成落盘的是抽屉中的 `/parse-status` 轮询；关闭页面后解析不会持续推进。
* 打开 Drawer 时 Radix 自动聚焦首个可聚焦控件，而首个控件经常是 Copy ID；Tooltip 对 focus 即时打开，所以没有鼠标移动也会显示。单纯增加 hover delay 不能修复。
* 文档 KG 公开入口只接受单个 `document_chunk`；跨切片只有规范化后完全同名、同类型实体会因稳定 ID 合并，没有文档级 entity resolution、关系 Reduce 或整批 snapshot。
* 当前前端没有任何资源行多选；后端只有 FAQ batch status 是同步批量事务，其余“批量”多为浏览器或服务层逐项循环。

### Research References

* [`research/document-level-kg-industry-patterns.md`](research/document-level-kg-industry-patterns.md) — Microsoft、Neo4j、AWS 都采用切片级抽取加文档/语料级归并和来源链路；推荐文档父任务、受约束实体 resolution 和原子 snapshot。
* [`research/list-multiselect-industry-patterns.md`](research/list-multiselect-industry-patterns.md) — Carbon、Fluent、Ant、Gmail 与 Google AIP 的多选、选择范围和批量 API 约定。
* [`research/list-surface-audit.md`](research/list-surface-audit.md) — 全站列表分类、五个一级业务资源表、次级维护表面与后端批量能力审计。
* [`research/tooltip-and-drawer-focus-industry-patterns.md`](research/tooltip-and-drawer-focus-industry-patterns.md) — Radix warm-up/skip-delay 与 WAI-ARIA 抽屉初始焦点规范。
* [`research/3d-knowledge-graph-visualization.md`](research/3d-knowledge-graph-visualization.md) — Three.js、react-force-graph-3d、Neo4j Bloom 和 GraphXR 的技术与交互方案；推荐 Three.js 生态渲染 + Bloom 式渐进探索。
* [`research/neo4j-project-fit-evaluation.md`](research/neo4j-project-fit-evaluation.md) — 基于当前 schema、事务、工作负载和 Neo4j 2026.06 官方能力，比较 PostgreSQL-only、Neo4j 图读模型和统一知识读层；推荐把图读模型作为条件触发的目标，而不是现在迁移主库。
* [`research/retrieval-bm25-and-scheme-b-boundary.md`](research/retrieval-bm25-and-scheme-b-boundary.md) — 追踪全部正式检索入口、状态门禁、score/rerank/评测缺口，并比较 `pg_search`、`pg_textsearch`、`pg_trgm` 与 VectorChord-BM25；推荐 PostgreSQL `pg_search + pdb.jieba` POC，Neo4j 仍只做图读模型。

### Feasible Approaches：文档级 KG

**A. 自动遍历切片、沿用当前逐片 upsert**

* 改动小，但没有真正 Reduce，最后完成切片覆盖描述/别名，部分结果和 revision 抖动会外露；不采用。

**B. 文档父任务 + 确定性 Reduce**

* 逐切片 Map 到 staging，按精确 name/type 和 canonical triple 合并，全部成功后原子替换；稳定但无法处理中文别名。

**C. 文档父任务 + 确定性预归并 + 受约束 entity resolution（推荐）**

* 模型只能把已有 local entity ID 分组，不能新增实体、关系或 evidence；canonical 名称和最终 ID 由代码确定；关系 Reduce 只重映射并去重 Map 明确抽出的关系。

### Feasible Approaches：列表多选范围

**A. 所有可变业务资源列表（推荐）**

* 第一阶段统一覆盖文档、FAQ、KG 实体、KG 关系、评测用例；别名、切片和会话只在具备明确批量维护动作后进入专用模式；排除导航、引用和诊断列表。

**B. 所有视觉列表**

* 连证据、候选、步骤、来源、设置和导航都加 checkbox；大量表面没有合法共同动作，会产生无意义或危险的选择状态；不推荐。

**C. 仅给当前最急的文档列表多选**

* 交付快，但无法形成用户要求的全局一致契约，后续每页仍会重复设计；不推荐。

### Feasible Approaches：Neo4j 存储边界（方案 B 已确认）

**A. 继续只用 PostgreSQL + pgvector**

* 当前单事务最干净，也足以支撑 1–2 hop bounded subgraph；但复杂多跳、路径和 GDS 能力继续增长时维护成本会上升。

**B. PostgreSQL 唯一写模型 + Neo4j 可重建图查询模型（推荐目标）**

* PostgreSQL 保留来源、任务、审核、revision、evidence 和检索投影；transactional outbox 只把 usable graph 投影到 Neo4j。达标后子图/路径 API 一次性只读 Neo4j，删除 PostgreSQL 运行路径，不做双写、双读或 fallback。

**C. PostgreSQL 业务写模型 + Neo4j 统一知识查询层**

* chunk、vector、Lucene BM25、KG traversal 全进 Neo4j，GraphRAG 上限最高；但当前所有 live status、parent/child、embedding stale 和来源失效都要跨库投影，范围和风险接近重写检索存储层，暂不推荐。

**Research conclusion**：Neo4j 有原生多跳、路径、GDS 和图探索生态优势；当前却是 0 usable graph、0 KG eval、正式入口全部 `use_kg=False`。用户后续已确认采用方案 B；实施仍必须先完成 PostgreSQL P0、隔离 POC、outbox/rebuild/checksum 和 fail-closed 门，不能把“已选架构”误解为无验证直接切库。

### Neo4j 存储边界确认（2026-07-15）

* 用户确认采用方案 B：PostgreSQL 是唯一来源、任务、审核、revision、evidence 和检索投影写模型；Neo4j 是 transactional outbox 驱动、可全量重建的图查询模型。
* 图查询切换后不保留 PostgreSQL 运行路径或静默 fallback；投影落后/Neo4j 不可用必须显式 fail closed。
* BM25 不因采用方案 B 自动迁入 Neo4j。当前 lexical 仍是 `ILIKE`，真正中文 BM25 未实现；其唯一存储/索引边界需完成检索审计后单独确认。

### 检索与 BM25 重新审计（2026-07-15）

* 当前 BM25 没有修复：FAQ/document 和 KG lexical 仍是 weighted `ILIKE`；`to_tsvector('simple', search_text)` GIN 从未被运行查询使用。
* 全部正式入口已统一到 `HybridRetrievalService`；FAQ/document live status、embedding ready、document file/chunk enabled、direct child 和 parent-context 分离基本正确。
* 当前仍有四项高优先级缺口：通用中文不会被分词；`%/_` 会被 `ILIKE` 当通配符；`top_score` 混用异构 raw score；候选数小于等于 `top_k` 时 rerank 不运行且 provider relevance score 被丢弃。
* 当前 PostgreSQL 为 16.14，只安装 `plpgsql,vector`；retrieval eval case/run 和 query analytics 均为 0，因此排序质量、索引计划和规模延迟没有真实证据。
* BM25 必须使用一致 tokenizer 并持有 TF、DF 与文档长度，但推荐由 BM25 倒排索引内部保存 token/postings/statistics；应用继续保存 canonical `search_text` 原文，不额外维护可见分词列。

### Feasible Approaches：中文 lexical / BM25（历史比较，方案 A 已确认）

**A. PostgreSQL `pg_search + pdb.jieba`，形成 `retrieval_hybrid_v2`（推荐）**

* 当前 v0.24.2 支持 PG16/Ubuntu 24.04，提供真正 BM25 和官方 Jieba；保持 FAQ/document 检索在 PostgreSQL，不扩大 Neo4j 方案 B。
* 需要安装扩展、preload、重启、真实 PG 集成测试和 AGPL-3.0/商业许可结论；离线 A/B 达标后一次硬切并删除 weighted `ILIKE`、闲置 GIN、模糊 `top_score` 和 rerank 跳过语义，不保留 fallback。

**B. 升级 PostgreSQL 17/18 后使用 `pg_textsearch + zhparser`**

* 真正 BM25、k1/b 可调且采用 PostgreSQL License；但当前 PG16 不兼容，意味着同时承担 major upgrade、zhparser 与 BM25 三项部署变化，中文组合也必须重新验证。

**C. 本轮只实施 Neo4j 方案 B，继续暂缓 BM25**

* 不新增临时 `pg_trgm` 层，当前 weighted `ILIKE` 保持现状直到独立任务；范围最小，但中文 lexical、无索引扫描和 score/rerank 缺口都会继续存在，不能宣称检索已完善。

**推荐边界（后续已确认）**：选择 A；把 `retrieval_hybrid_v2` 与 Neo4j 图投影拆成两个独立实施单元，最终都只保留唯一运行路径。先建立中文金标和 PostgreSQL 集成门，再硬切 BM25；Neo4j v1 始终不投影 FAQ/document chunk、embedding 或主检索全文数据，只允许对已投影图属性建立同步 seed-search index。

### 中文 BM25 方案确认（2026-07-15）

* 用户确认采用方案 A：PostgreSQL `pg_search + pdb.jieba`，并要求开始实施。
* canonical `search_text` 继续保存原文；token、postings、TF/DF 和文档长度由 BM25 index 内部持有，业务表不增加人工分词字符串列。
* PostgreSQL 承担 FAQ/document BM25、pgvector、RRF、rerank 和 parent context；Neo4j 方案 B 不接管 chunk/full-text 主检索。
* 达标后只保留 `retrieval_hybrid_v2`，不保留 weighted `ILIKE`、`simple` GIN、backend switch、双读或静默 fallback。
* Community 扩展先用于本地实现与验证；未来外部闭源交付前必须另行完成 AGPL 合规或商业许可决策。

### Follow-up Decision（ADR-lite）

**Context**：文档解析生命周期、跨切片 KG、Tooltip 初始焦点、批量操作和 3D 图谱需要统一当前契约，不能继续由页面轮询、单切片入口或客户端单项循环拼接。

**Decision**：用户确认全部采用推荐方案：文档 KG 采用方案 C，entity resolution 首版限定单文档；批量选择首版只选当前页；Tooltip/Drawer 使用共享静态初始焦点、pointer 500ms 首次延迟和 400ms warm-up；同步短操作原子 batch，长任务持久 parent job + items。3D 可视化作为后期独立阶段，具体渲染组合待本次调研方案确认。

**Consequences**：公开单切片文档 KG 入口将被删除；候选只在文档 snapshot 完整完成后进入审核；列表不会隐式跨页操作；Canvas 3D 不替代表格审核与 evidence 视图。

## Assumptions (temporary)

* P0 先保证现有 KG 数据和审核状态不会污染检索，再改善可观察性和操作体验。
* BM25 是独立的检索数学口径与部署选择，不应借 KG 修复顺便无评测切换。
* Neo4j 方案 B 已确认；其图读模型与 PostgreSQL lexical/BM25 是两个独立实施单元，不允许静默扩大成统一知识读层。

## Confirmed Scope

* 用户于 2026-07-14 确认按推荐范围修复。
* 本轮完成 KG 闭环和统一检索 correctness。
* 真正中文 BM25 作为独立实施单元采用 PostgreSQL `pg_search + pdb.jieba`，不与 Neo4j 图投影混成统一知识读层。
* 0→1 功能采用唯一的新契约，不保留旧同步入口或推测性兼容路径。

## Requirements（已确认）

### P0：KG 正确性与可验证闭环

* 抽取失败必须在前端明确显示失败原因，不得显示“抽取完成”。
* 评测 UI 必须能显式选择 baseline 或 KG debug，并把 `use_kg` 传到后端。
* FAQ 和整篇文档都必须从其正常管理页面提供可用的 KG 抽取入口，不要求用户进入单切片工具栏、猜测或手抄截断 ID。
* 关系进入 usable 前，头尾实体必须满足明确的可用状态约束。
* 实体退回待审核或停用时，关联关系及其检索投影不得继续作为可用事实召回。
* KG 检索必须尊重证据来源的实时状态：FAQ usable、文档和切片未禁用；失去全部有效证据的事实不可召回。
* 缺少证据的实体或关系不得确认；证据抽屉必须能打开或复制精确来源标识。
* 重复抽取更新已确认实体/关系时，审核表、证据和投影不得静默漂移；必须选择保持已确认快照或显式标 stale/needs_review。
* 实体/关系确认必须提交用户实际看到的 `expected_revision`；审核快照变化后返回 409，刷新并重新人工确认，不能以默认值或兼容入口继续。
* 所有 KG 多表写遵循全局实体后关系的确定性锁序；纯锁定端点不得被误当成来源失效实体，不得用 deadlock retry/backoff 代替正确锁序。
* 抽取接口不得长时间阻塞 ASGI 事件循环；任务状态应可查询，并支持失败重试。
* 子图 API 固定只返回 usable 事实，并区分中心实体不存在、存在但孤立、查询失败三种状态。
* KG 评测结果应以原始 FAQ/文档证据是否被扩召回为主要口径，不能只把合成 KG chunk 当作成功命中。

### P0：统一检索 correctness

* 无 child、单 child、多 child 和 delimiter child 四类文档均必须有可直接召回的知识单元。
* FAQ 正文编辑后，旧统一知识单元必须立即不可检索，直到新 embedding/投影就绪。
* 会进入 embedding/search_text 的假设问题变化时，对应知识单元必须变 stale。
* 明确后台助手、CLI、微信、MCP 是否统一到同一检索服务，并用契约测试锁定。

### P1：中文 lexical / BM25（方案 A 已确认）

* PostgreSQL `pg_search + pdb.jieba` 是 FAQ/document lexical 的唯一目标；应用保存 canonical `search_text`，token/postings/TF/DF/document length 由 BM25 index 管理。
* BM25、pgvector 和可选 KG debug 只按 channel rank 进入 RRF，不直接比较异构 raw score。
* 删除模糊 `score/top_score`，明确保存 RRF、rerank 与各 channel score；rerank 已配置且至少两条候选时必须参与最终排序。
* 在真实 PostgreSQL 上验证 lifecycle、partial index、`EXPLAIN (ANALYZE, BUFFERS)`、中文 expected IDs 和延迟后一次硬切，删除 weighted `ILIKE`、闲置 GIN 与 runtime fallback。

### P1：Neo4j 图读模型（方案 B 已确认）

* PostgreSQL 是唯一来源、审核和 KG 写模型；同一 domain transaction 写 ID-reference outbox。
* Neo4j 只投影有实时 evidence 的 usable `KgEntity` 与 `KG_RELATION`，不投影 FAQ/document/chunk/evidence/embedding 或主检索全文数据。
* Projector 必须幂等，支持 pending 扫描、checkpoint、rebuild/checksum 和崩溃恢复。
* 图 API 切换后只读 Neo4j；projection lag、rebuild、failed 或 unavailable 时返回 503，不查询 PostgreSQL fallback。

### P0：文档流程与 KG 来源治理（后续已确认）

* MinerU 解析由可恢复的后端任务持续推进；页面、抽屉和浏览器生命周期不得控制任务是否继续执行。
* 文件列表名称右侧显示紧凑解析进度，抽屉显示完整阶段/页数/百分比；两处读取同一后端任务状态。
* 删除来源后，零 evidence 的实体、关系和投影转为 `disabled`；仍有其他 evidence 的共享事实转为 `needs_review`；实时精确重算来源数量。
* 删除 mutation 完成后立即失效文档、KG 实体、KG 关系和子图缓存。

### P0：全站资源列表批量操作（范围已确认）

* 所有具备合法共同批量动作的可变业务资源列表必须支持多选；第一阶段覆盖文档、FAQ、KG 实体、KG 关系和评测用例。
* 检索别名、文档切片和会话仅在专用维护模式中提供相应批量动作；导航、只读引用、候选、诊断和设置表面明确排除。
* 选择必须由稳定资源 ID 驱动，KG 审核还必须保存用户所见 `review_revision`。
* 同库短操作使用真正的同步原子 batch API；任一 ID、revision 或领域门禁失败则整批不变，不允许客户端 N 次单项请求伪装批量事务。
* 解析、Embedding、评测运行和 KG 抽取等外部/长操作使用持久 parent job + items，允许显式 partial success 和失败项重试；关闭浏览器后继续。
* 选中后展示数量和所有选中项共同合法的动作；危险动作数量化确认，不适用项不得静默跳过。
* 首版表头全选只作用于当前已渲染页；翻页、查询、筛选或实体/关系 tab 改变时清空选择。跨全部筛选结果必须等各列表具备真实服务端总数与查询契约后再单独实现。

### P0：文档级 KG（方案已确认）

* 文档 KG 采用“文档父任务 + 确定性预归并 + 受约束 entity resolution”；产品入口以整篇文件为边界，逐切片只是后端内部 Map；删除公开单切片抽取入口，不保留兼容路径。
* 每条候选事实保留精确 file/chunk/页码/章节和原文位置证据；Reduce 不得改写或制造 evidence。
* 文档结果只在全部 Map/Resolve/Reduce 成功后原子替换；失败或文档指纹变化时旧 snapshot 保持不动，不暴露 partial candidate。
* 跨切片实体归并与关系生成必须分离：实体 resolution 可以合并同一对象，关系只能来自 Map 明确抽出的事实，首版不凭跨切片推理新增关系。
* entity resolution 首版只在单文档内运行；模型只能对现有 local entity ID 做等价分组，不能新增事实或跨类型合并，canonical 名称与最终 ID 由代码确定。

### P0：Drawer 初始焦点与 Tooltip（方案已确认）

* 复杂 Drawer 打开后初始焦点落在标题或内容容器，不落在 Copy ID、危险按钮或 DOM 中首个交互控件。
* Tooltip pointer 首次进入等待 500ms；成功展示后 400ms 内浏览相邻 trigger 即时显示；不实现累计查看次数状态。
* 键盘 focus 仍即时显示 Tooltip，Tab 顺序、关闭后焦点恢复和辅助技术语义不得受损。

### P2：3D 知识图谱可视化（后期独立阶段）

* 3D 是 usable 图谱的探索视图，不替代实体/关系审核表、批量操作和 evidence 抽屉。
* 后期实现必须采用成熟 3D/WebGL 图渲染方案并遵循 search-first、bounded scene、渐进展开和语义图例，不默认加载整库。
* Canvas 之外保留语义 HTML 搜索、图例、已选详情和表格视图；WebGL 不可用时显示明确状态。
* 用户已确认后期采用推荐技术组合：Three.js + `react-force-graph-3d`，交互参考 Neo4j Bloom；该独立阶段不阻塞本轮检索 v2 与 Neo4j 图读模型。

## 实现清单（代码已完成，最终验收待验证）

* [x] 模型/provider/超时/JSON 失败通过异步任务终态在 KG 页和文档切片入口显示失败，不把 failed 当作完成。
* [x] 评测页可运行 baseline 与 KG debug，并按同一用例、不同策略保留候选和指标。
* [x] 未确认端点、停用端点或无有效来源证据的关系不会进入 KG 扩展召回。
* [x] 重复抽取、实体降级、来源删除/重解析会同步使相关审核快照和投影失效或退回待审核。
* [x] 审核 revision、409 冲突、全局 entity→relation 两阶段锁序及 lock-only endpoint 语义已由单元测试和真实 PostgreSQL 测试锁定。
* [x] 分页收缩、筛选请求、子图 404/失败/isolated/connected 已有不同状态。
* [x] 无结构块、单结构块、多结构块和 delimiter 切分均生成 parent + 至少一个 direct child。
* [x] FAQ 正文或假设问题变化会让旧统一知识行立即不可检索，等待新 embedding。
* [x] 正式入口全部复用唯一 `HybridRetrievalService`，显式 `use_kg=False`；只有评测 KG debug 传 `True`。
* [x] 评测、检索、来源序列化均使用 canonical `RetrievedKnowledgeChunk`，parent 与 direct candidates 分离。
* [x] JSONB parse progress、文件 chunker、`ParsedBlock` 边界和 KG 私有入口已清除臆造兼容、默认补值和无增值方法别名。

## 最终验证清单（2026-07-15）

* [x] 完整 `python -m pytest` 为 567 passed / 4 skipped，`python -m ruff check .` 与 `python -m cyclops check-config` 通过。
* [x] 前端 `npm test` 为 71 passed，`npm run typecheck`、`npm run lint`、`npm run build` 通过并重建实际 ASGI 静态产物。
* [x] 单元与 SQL 契约测试覆盖 v2 历史清理、精确 KG evidence expansion、usable-only subgraph、document direct-child/parent 查询和正式入口显式关闭 KG。
* [x] 一次性 PostgreSQL 17 + pgvector 验证 schema 连续迁移、parse_progress CHECK、FAQ 投影回填、生产 confirm 并发无死锁及 relation-only lock-only endpoint 不失效（4 passed）。
* [x] Playwright + 最新 `dist` 验证 KG 页加载、证据页码 0、revision=7 confirm 请求体、HTTP 409 提示、抽屉保持待审核且没有成功 toast/状态漂移。
* [x] 契约测试与生产引用扫描确认默认智能问答、CLI、微信和 MCP 均显式关闭 KG，parent 未进入搜索结果、指标和 analytics。
* [ ] 真实业务材料上的模型抽取质量、证据跳转和 baseline/KG debug 收益仍属于上线前人工验收；本轮遵守“不向业务库写测试数据”，未伪造生产 FAQ/KG 记录。

## 后续 Acceptance Criteria（随方案确认收敛）

* [ ] 关闭文档抽屉、离开文档页及关闭浏览器后，processing 解析任务仍推进到 completed/failed，重启服务后能恢复未完成任务。
* [ ] 文档列表名称右侧显示当前解析百分比，抽屉显示同一任务的阶段、页数和百分比，终态停止轮询。
* [ ] 删除唯一来源后相关 KG owner/projection 为 disabled、evidence 为 0、实时来源数为 0；共享来源事实仅退回 needs_review。
* [ ] 打开任一复杂抽屉且鼠标未移动时没有 tooltip 自动出现；Tab 仍可访问 Copy ID 并获得即时键盘提示。
* [ ] 第一阶段纳入的每个业务资源表支持逐行、表头三态选择、已选数量、取消和合法批量动作。
* [ ] 同步批量动作对缺失 ID 或 stale revision 全部回滚；异步批次可在刷新后恢复并逐项展示结果。
* [ ] 文档级 KG 任务展示 `processed_chunks/total_chunks` 和 Map/Resolve/Reduce 阶段，最终只提交一个文档 snapshot。
* [ ] 同一实体的中文别名在受约束 resolution 后映射到稳定 canonical entity；不同类型实体不会自动合并。
* [ ] Reduce 后每条关系的 evidence 仍能定位到真实原文；没有 evidence 的新关系不能产生或确认。
* [ ] `retrieval_hybrid_v2` 使用 PostgreSQL BM25 index + pgvector + RRF；正式代码不再包含 FAQ/document weighted `ILIKE`、闲置 `simple` GIN 或 lexical backend switch。
* [ ] BM25 index/query 使用同一 `pdb.jieba`，禁用来源零泄漏；真实 PostgreSQL EXPLAIN、中文 expected IDs 与 P95 门通过。
* [ ] 对外排名只返回命名明确的 RRF/rerank/channel score，parent 没有伪 `score=1.0`，候选数不大于 top-k 时仍可 rerank。
* [ ] Neo4j outbox/projector/rebuild/checksum 通过重复消费、并发提交和四类崩溃恢复测试；节点/边 parity 为 100%。
* [ ] 图 API 只读 Neo4j，pending/rebuilding/failed/unavailable 均明确 503；正式代码不存在 PostgreSQL subgraph runtime 或 backend fallback。

## Definition of Done

* 用户已确认本轮范围和 lexical/BM25 方向。
* `docs/changes/20260714-092948-knowledge-graph-retrieval-review/` 中计划、确认和验证记录同步更新。
* 数据库变更提供幂等迁移；不兼容的旧评测派生行按 `contract_version` 清理，synthetic expected ids 通过精确关系清理，不增加旧格式兼容读取。
* 行为变更先补失败测试，再实现修复。
* 使用只读/测试数据验证，不向业务库写入假 FAQ、假实体或假关系。
* 明确回滚策略；不默认打开 KG 影响正式客服回答。
* 两个新增实施单元分别通过真实 PostgreSQL / Neo4j 集成门；切换提交删除旧 runtime，不以 fallback 代替回滚。

## Out of Scope (explicit)

* 3D 图谱已确认为后期演进方向；在本轮重新确认范围前不直接实现，当前只完成权威方案调研和数据/API 可扩展性设计。
* Neo4j 本轮只实现方案 B 图读模型、bounded subgraph 与 graph seed search；完整 GraphRAG、GDS、社区报告和 3 跳以上推理不进入本阶段。
* 不切换正式问答默认 KG 策略。
* 不把 FAQ/document/chunk 投影到 Neo4j，不新增独立搜索引擎或应用侧人工分词列。

## Technical Notes

* 审查记录：[`research/review-20260714.md`](research/review-20260714.md)
* lexical 方案：[`research/chinese-lexical-options.md`](research/chinese-lexical-options.md)
* 文档级 KG 行业调研：[`research/document-level-kg-industry-patterns.md`](research/document-level-kg-industry-patterns.md)
* 列表多选行业调研：[`research/list-multiselect-industry-patterns.md`](research/list-multiselect-industry-patterns.md)
* 全站列表审计：[`research/list-surface-audit.md`](research/list-surface-audit.md)
* Tooltip/Drawer 调研：[`research/tooltip-and-drawer-focus-industry-patterns.md`](research/tooltip-and-drawer-focus-industry-patterns.md)
* 3D 图谱调研：[`research/3d-knowledge-graph-visualization.md`](research/3d-knowledge-graph-visualization.md)
* Neo4j 项目适配评估：[`research/neo4j-project-fit-evaluation.md`](research/neo4j-project-fit-evaluation.md)
* 检索/BM25 与方案 B 边界：[`research/retrieval-bm25-and-scheme-b-boundary.md`](research/retrieval-bm25-and-scheme-b-boundary.md)
* `retrieval_hybrid_v2` 设计：`docs/changes/20260714-092948-knowledge-graph-retrieval-review/retrieval-hybrid-v2-design.md`
* Neo4j 方案 B 设计：`docs/changes/20260714-092948-knowledge-graph-retrieval-review/neo4j-graph-read-model-design.md`
* 主要后端：`cyclops/db/kg.py`、`cyclops/kg.py`、`cyclops/kg_ai.py`、`cyclops/db/knowledge.py`、`cyclops/admin_server.py`、`cyclops/retrieval.py`
* 主要前端：`web/src/pages/KnowledgeGraphPage.tsx`、`web/src/pages/EvaluationPage.tsx`、`web/src/api/hooks.ts`
* schema：`sql/001_init.sql`
* 统一检索规格：`.trellis/spec/backend/cyclops-retrieval-contracts.md`
