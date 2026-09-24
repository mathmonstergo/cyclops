# 知识图谱与混合检索审查后续计划

## 修改目标

已完成第一阶段知识图谱与统一检索正确性修复。当前新增两个已确认、相互隔离的实施单元：以 PostgreSQL `pg_search + pdb.jieba` 建立 `retrieval_hybrid_v2`，以及以 transactional outbox 建立 Neo4j 方案 B 图读模型；两者达标后分别硬切并删除旧 runtime。

## 影响范围

* KG 数据与状态：`kg_entities`、`kg_relations`、`kg_evidence`、KG 投影。
* 并发审核：`review_revision`、confirm 409、entity→relation 全局锁序和 lock-only endpoint。
* KG 抽取任务：失败语义、异步执行、任务查询和重试。
* KG 管理页与评测页：失败状态、策略开关、证据跳转和分页边界。
* 统一检索：文档 parent/child、FAQ stale、假设问题 stale、RAG 入口一致性。
* lexical 检索：删除当前 `ILIKE` 与闲置 GIN，切换已确认的中文 BM25 路线。
* 检索 v2：`pg_search` 部署、Jieba BM25 index、canonical hit/score、RRF/rerank、analytics 和评测 v3。
* Neo4j 图读模型：outbox、checkpoint、projector、rebuild/checksum、graph-property search、子图 API 和 fail-closed 运维。
* 导入严格契约：`parse_progress` JSON object 与文件级 canonical chunker。

## 初始审查结论（实现前历史记录）

* 当前 KG 后端和页面不是空壳，但从现有 UI 无法启用 `use_kg` 评测，因此“抽取 -> 审核 -> 验证收益”没有闭环。
* 抽取失败会被 UI 当成成功；当前库 10 个任务中 9 个 failed，问题已实际影响使用。
* 关系端点状态、来源实时状态和重复抽取后的投影一致性缺少门禁。
* 文档删除/重解析不会失效 KG 证据；子图状态参数和缺证据确认也缺少后端门禁。
* FAQ 管理页没有可用的完整 ID/直接抽取入口，证据抽屉也不能跳转或复制精确来源。
* 当前关键词检索不是 BM25，也没有使用已创建的全文 GIN 索引。
* 统一检索另有无-child 文档不可召回、FAQ 编辑后旧知识单元仍可命中的高优先级问题。

## 已完成实现

1. KG 抽取已改为唯一异步状态机：请求显式提交 `source_type + source_id`，POST 只返回 queued，后台执行，GET 轮询终态；失败任务保留有界错误并通过新任务重试。
2. 模型 JSON、来源 fingerprint、证据 locator、审核确认、关系端点、实体降级、重复抽取及来源删除/重解析均增加严格门禁；没有来源推断、同步包装、通用完成接口或 `status=usable` 旁路。
3. FAQ 与文档正常管理页面提供直接 KG 抽取，旧手填 FAQ ID 入口已删除；证据可打开精确 FAQ/切片或复制 locator，关系确认同时检查证据与头尾状态。
4. KG 召回已改为 fact 诊断 + 原始证据扩展：精确连接 live FAQ 或 document direct child，按原始知识行去重，KG 只提供一次 best-rank RRF vote，评测指标不再使用合成 KG chunk。
5. usable-only 子图已固定为唯一 API 契约，拒绝 `status` 查询字段，明确区分缺失/不可用中心、isolated、connected 和查询失败。
6. 评测已升级为 v2：运行 payload 只能为 `{}` 或 `{"use_kg": true}`，保存 `contract_version=2`，历史无效派生运行幂等清理，case 列表只返回按固定策略排序的 `latest_runs`；前端 override、汇总和诊断均按 case + strategy 隔离。
7. 文档知识身份已明确区分 import file、import chunk 与 knowledge row；每个来源切片始终生成 parent + 至少一个 direct child，child 使用稳定负索引，parent 只进入答案上下文。
8. FAQ 正文、假设问题和文档投影的 stale/ready 状态已按统一知识行校正；后台助手、CLI、微信和 MCP 已统一到 `HybridRetrievalService` 与 `RetrievedKnowledgeChunk`。
9. `RetrievedDocument`、FAQ-only `Database.search()`、内部 question/answer 属性别名和各入口复制的检索流程已删除；正式入口显式 `use_kg=False`，不保留兼容/fallback。
10. 当前 lexical 仍明确命名为 PostgreSQL `ILIKE` 混合检索；真正中文 BM25 本轮未实现，留待基线稳定后的独立任务。
11. KG confirm 已固定为 `{"expected_revision": 正整数}` 唯一请求体；审核行初始 revision 1，来源替换/降级/级联失效递增，锁内冲突统一映射 409，前端原样提交并要求刷新后重新审核。
12. KG snapshot replacement 已改为全局两阶段锁协调：所有实体按 ID lock/upsert，实体阶段完成后重读 incident relations，所有关系再按 ID lock/upsert；relation-only endpoints 只参与锁仲裁，不修改状态、revision、时间或实体投影；禁止 deadlock retry。
13. 删除剩余臆造兼容：`parse_progress` 不再解析字符串/接受 JSONB 标量，文件 chunker 不再从全局或 UI `naive` 补旧/未知值且显式 null/空白/大小写别名会报错，解析完成后的 chunker 只接受 `ParsedBlock`，无引用 KG 投影 helper 和 FAQ KG builder 方法别名已删除。
14. 新增真实 PostgreSQL 17 + pgvector 测试：上一版 schema 连续迁移、parse progress object CHECK、FAQ 投影回填、生产 `confirm_kg_relation(expected_revision=...)` 与 snapshot replacement 的真实锁等待无死锁、relation-only lock-only endpoint 不失效；一次性环境结果为 `4 passed`，容器已删除。

## 实现目标

* 用户能看到抽取失败原因并重试，不再把失败误认为成功。
* 未审核、已停用或失去有效来源的图谱事实不会参与召回。
* 评测页能明确比较 baseline 与 KG debug，判断图谱是否改善原始证据召回。
* 文档与 FAQ 的编辑/embedding 状态和统一检索结果保持一致。
* lexical 检索名称、算法和索引行为一致，不再把固定 `ILIKE` 加权误称为 BM25。

## 2026-07-14 第一阶段用户确认（历史基线）

* 已确认：本轮完成 KG 闭环 + 统一检索 correctness。
* 已确认：真正中文 BM25 独立立项，在基线评测后选择实现。
* 本轮不增加 `pg_trgm`、中文 FTS 或独立搜索引擎。
* 0→1 KG 只保留一套显式异步契约：请求必须提交 `source_type + source_id`，POST 返回 queued，后台执行，GET 轮询终态。
* 不保留来源类型自动推断、同步执行包装、通用任务状态更新、公开候选保存旁路或 `status=usable` 确认旁路。
* 统一检索只保留 `HybridRetrievalService.retrieve(..., include_parent_context=..., use_kg=...)`，调用方必须显式传 `use_kg`；不保留 `RetrievedDocument`、FAQ-only search 或 dict/属性别名适配。
* 评测只保留 v2 当前契约；不读取、补齐或展示旧运行形状，不保留 singular `latest_run` 或 `use_kg=false` payload。
* 用户于 2026-07-15 再次强调“本项目所有从 0→1 开发都不需要兜底兼容，要朝最干净方向制作”，因此 confirm 不增加 bodyless/default revision，导入 JSONB/chunker 也不保留不存在的旧形状。

## 第一阶段完成状态（历史基线）

范围内代码实现、测试资产、code-spec 与实际 ASGI 静态产物已同步，真正中文 BM25 未进入实现。最终全量门、真实 PostgreSQL 定向门和 revision/409 浏览器 smoke 均已通过；本轮不提交、不归档，也未向业务数据库写入测试数据。

## 2026-07-15 后续扩展计划（调研与确认阶段）

用户现场测试后新增了文档后台解析、列表进度、来源删除后的 KG 状态、Tooltip 初始焦点、文档级 KG 和全站列表多选要求。该范围涉及任务持久化、数据库/API、KG 抽取模型和多页 UI，当前按项目约定先完成调研与设计确认，不直接叠加实现。

### 已确认修改目标

1. MinerU 解析由后端持久任务持续推进，离开页面或关闭浏览器后仍完成；列表文件名右侧显示紧凑进度条，抽屉保留阶段/页数/百分比。
2. 删除来源后，零 evidence 的 KG 实体、关系及投影自动 disabled；仍有其他来源的共享事实退回 needs_review；精确重算来源数并刷新前端 KG 缓存。
3. 继续遵守 0→1 唯一当前契约，不新增单切片/整篇、前端轮询/后端 worker 或单项循环/批量 API 的双路径兼容。

### 已完成调研

* Microsoft GraphRAG、Neo4j KG Builder、AWS Bedrock GraphRAG 的文档级 Map/Resolve/Reduce 与 provenance 模式。
* IBM Carbon、Microsoft Fluent、Ant Design、Gmail 和 Google AIP 的选择范围、批量工具栏和后端原子/异步 batch 语义。
* Radix Tooltip warm-up/skip-delay 与 WAI-ARIA Dialog 初始焦点规范。
* 全站前端列表表面和对应后端批量能力审计。
* Three.js、react-force-graph-3d、d3-force-3d、Neo4j Bloom、GraphXR 与 Canvas accessibility 的 3D 图谱方案调研。

研究记录位于 `.trellis/tasks/07-07-knowledge-graph-next/research/`：

* `document-level-kg-industry-patterns.md`
* `list-multiselect-industry-patterns.md`
* `list-surface-audit.md`
* `tooltip-and-drawer-focus-industry-patterns.md`
* `3d-knowledge-graph-visualization.md`
* `neo4j-project-fit-evaluation.md`

### 已确认推荐方向

1. 文档 KG 只保留整篇文件公开入口；后端逐切片 Map、单文档内受约束 entity resolution、关系确定性 Reduce，全部完成后一次提交 snapshot；删除公开单切片入口。
2. “所有列表支持多选”解释为所有具备合法共同批量动作的可变业务资源列表；第一阶段覆盖文档、FAQ、KG 实体、KG 关系和评测用例，排除导航、引用、候选和诊断表面。
3. 首版表头全选只作用于当前已渲染页；待所有列表具备真实服务端总数和筛选契约后，再增加 Gmail 式“选择全部匹配结果”。
4. Drawer 初始焦点放到标题/内容容器，Tooltip pointer 首次延迟 500ms、warm-up 400ms；保留键盘 focus 即时提示。
5. 同库短操作使用同步原子 batch API；解析、Embedding、评测运行、KG 抽取使用持久 parent job + items 和显式 partial success。

### 3D 后期推荐方向（待本轮确认）

1. 使用 Three.js 作为 3D/WebGL 底座、`react-force-graph-3d` 作为 React 图渲染/交互层、d3-force-3d 作为布局引擎。
2. 产品交互参考 Neo4j Bloom：搜索进入 bounded scene、渐进展开邻居、图例过滤、聚焦选择、详情/evidence 抽屉。
3. 3D 作为独立后期阶段，不阻塞本轮文档级 KG 和数据正确性；数据库选型独立决策，不能只因 3D 展示自动引入 Neo4j，也不引入商业 GraphXR 依赖。

### Neo4j 再评估（历史调研，后续已确认方案 B）

1. Neo4j 的真实正向价值是原生多跳/路径、vector + Lucene full-text + graph traversal、GDS 社区/中心性以及更自然的图探索查询层；它不会自动解决抽取、evidence、审核状态或 3D WebGL 渲染。
2. Neo4j 2026.06 当前实现的 full-text 使用 Lucene 10.4 默认 BM25；内置 `cjk` analyzer 生成 CJK bi-gram。应用不必另存 token，Lucene 在倒排索引内部保存词频/文档频率/长度统计，但领域中文分词质量仍需金标评测。
3. 当前只读现场为 0 usable graph、0 KG eval、0 query analytics，正式入口全部关闭 KG；当前 1–2 hop/40–200 edge 查询没有性能故障证据，不能据此声称换库更快或回答更准。
4. 推荐目标是 PostgreSQL 保持唯一来源/审核写模型，Neo4j 在门槛达成后作为 transactional outbox 驱动、可重建、无 PostgreSQL 查询 fallback 的图读模型；不推荐 Neo4j 直接成为 KG 审核主库。
5. 当前先完成 P0 与真实评测集；出现 3+ hop 路径、GDS、KG 进入正式问答或 PostgreSQL 达不到 SLA 等条件后做隔离 POC，正确性、禁用数据零泄漏和目标查询性能达标再切换。

### 后续步骤

1. 用户逐项确认上述范围边界和推荐方案。
2. 更新 confirmation、PRD 和最终设计文档，消除旧单切片要求与文档级唯一入口之间的历史冲突。
3. 按 UI 约定提供可交给视觉 AI 的布局 prompt，等待布局图与功能确认。
4. 编写分阶段实现计划和失败测试，再进入后端任务、KG、批量 API、共享选择组件与页面改造。
5. 完成 Python/React/真实 PostgreSQL/浏览器验证后再启动服务供用户验收；不自动 Git commit。

## 最终验证结果

* Python：567 passed / 4 skipped；Ruff 与 `cyclops check-config` 通过。
* 前端：71 passed；typecheck、lint、build 通过，`cyclops/static/dist` 已重建。
* PostgreSQL：一次性 PostgreSQL 17 + pgvector 环境 4 passed，覆盖连续迁移、revision/FAQ 投影、parse-progress CHECK、生产 confirm 真实锁等待、无死锁和 lock-only endpoint 不失效；容器和隔离 schema 已删除。
* 浏览器：最新 `dist` 加载 KG 页后，revision=7 候选发出精确 `{"expected_revision":7}`；模拟 409 时显示刷新提示，抽屉保持待审核，无成功 toast 或状态漂移；浏览器与临时服务已清理。
* 静态复审：744 个新增/语义变化 Python 函数/方法均有中文 docstring；同步包装、默认 revision、旧模型/属性别名、字典 ParsedBlock 适配和 chunker 构建回退均无残留。
* 正式入口：契约测试和引用扫描确认后台助手、CLI、微信、MCP 均显式关闭 KG；parent 只进入 prompt/source context，不进入搜索结果、指标或 analytics。

## 未做事项与风险

* 真正中文 BM25、中文 tokenizer、倒排索引和搜索扩展均未实现；当前 lexical 仍是 `ILIKE`。
* 真实业务材料上的模型抽取质量、证据跳转和 baseline/KG debug 收益仍需上线前人工验收；本轮为避免污染业务库，只使用单元测试、一次性 PostgreSQL schema 与浏览器 mock 数据。
* 历史评测运行属于可重跑派生数据；v2 迁移会删除无法安全转换的旧运行，而不是增加兼容读取或“无效”展示状态。
* 若数据库曾被手工写入非 object 的 JSONB `parse_progress`，新 CHECK 会明确阻止迁移；项目没有合法标量历史，因此不自动改写为 `{}`，需要先审计并显式修正异常数据。
* 后续扩展尚处于方案确认阶段；文档 durable worker、文档级 KG、Tooltip 修复和列表多选均未开始编码。
* 文档、评测和别名列表当前存在 100/16 条截断或本地过滤；在服务端查询总数与分页契约修正前，不能把表头全选描述为全部筛选结果。

## 2026-07-15 方案 B 与检索/BM25 重新审计

### 已确认架构

* Neo4j 采用方案 B：PostgreSQL 是唯一来源、任务、审核、revision、evidence 和正式检索写模型；Neo4j 是 transactional outbox 驱动、可全量重建的 usable graph 查询模型。
* 图 API 切换后删除 PostgreSQL runtime graph query；Neo4j 不可用、投影落后、重建或 checksum 失败时显式 503，不做 backend switch、双读或 fallback。
* Neo4j v1 不投影 FAQ、document、chunk、evidence、embedding、全文索引、任务、审核或评测；把这些能力迁入 Neo4j 属于方案 C。

### 检索审计结论

* 统一入口、canonical model、FAQ/document 实时状态门禁、direct child、parent context 分离、RRF 和 KG evidence expansion 基本正确。
* BM25 未实现；正式 lexical 和 KG fact lexical 均是 weighted `ILIKE`，闲置 `simple` GIN 未被使用，普通中文 query 没有通用 tokenizer。
* 仍需收口 `%/_` literal、异构 `top_score`、rerank 在候选数不大于 top-k 时跳过、provider relevance score 丢失、parent `score=1.0`、配置范围以及真实 PostgreSQL/金标评测。
* 当前 PostgreSQL 16.14 仅安装 `plpgsql,vector`，retrieval eval case/run 与 analytics 均为 0。

### 推荐的后续实施单元（已确认，书面设计复核通过）

1. 独立 `retrieval_hybrid_v2`：以 PostgreSQL `pg_search + pdb.jieba` 做隔离 POC；建立真实中文金标、索引/生命周期/延迟门，达标后一次硬切并删除 weighted `ILIKE` 与闲置 GIN，不保留运行时旧路径。
2. 独立 Neo4j 方案 B：先修复 KG `source_count` 精确重算和来源失效状态，再实现 outbox、projector、checkpoint、rebuild/checksum、fail-closed 与图 API 硬切。
3. 两个实施单元保持清晰存储边界：PostgreSQL 承担 pgvector、BM25、RRF、rerank、parent 与 KG fact → live evidence；Neo4j 只承担 usable entity/relation 的图遍历与后续 GDS/3D 查询。

详细代码证据、扩展比较和切换门槛见 `.trellis/tasks/07-07-knowledge-graph-next/research/retrieval-bm25-and-scheme-b-boundary.md`。

### 2026-07-15 实施计划状态

用户已再次确认接手推荐方案并要求直接开始。书面设计经过 retrieval/Neo4j/cross-spec 复核后没有阻塞项，现按依赖顺序执行：

1. `implementation-plan-unit-0-kg-source-correctness.md`：先修 live evidence、精确 `source_count`、disabled/needs_review、重新启用和前端 cache/count 契约。
2. `implementation-plan-unit-1-retrieval-hybrid-v2.md`：硬切 `pg_search 0.24.2 + pdb.jieba`、canonical score/rank、analytics v2 和 eval v3。
3. `implementation-plan-unit-2-neo4j-graph-read-model.md`：outbox/checkpoint/projector/rebuild/fenced reader，parity 后同次删除 PostgreSQL graph runtime 与 synthetic KG chunks。

三个单元都使用失败测试先行；当前连续工作区包含此前已确认但未提交的实现基线，因此不从旧 HEAD 新建 worktree，也不自动提交用户改动。

## 2026-07-15 Unit 0 completion

* KG entity `source_count` 已固定为数据库按实时有效 distinct locator 维护的必填整数；关系列表和子图边返回必填 live `evidence_count`。
* 审核列表保留全部历史 evidence，但每条必填 `is_valid`；表格的“有效来源/有效证据”不再使用历史数组长度，Drawer 将失效来源作为低对比历史保留。
* 前端只保留 `invalidateKgReviewQueries()` 一个失效器，同时覆盖 entity/relation/subgraph、KG 审核/抽取、文档删除/启停、切片启停/正文、FAQ 保存和成功文档 snapshot replacement。
* 异步 snapshot 成功刷新由实际终态响应边界负责，不依赖可能卸载的 Drawer；三类 KG query 在重新 mount 时强制 refetch，接住后端 worker/out-of-band 变更。
* 最终 fresh 门：Python `235 passed`，真实 PostgreSQL `8 passed`，前端 `80 passed`，目标 Ruff、typecheck、lint 和 diff check 通过；独立 spec/quality review 均通过。

## 2026-07-18 Unit 0b 恢复记录

* 已从实际工作区恢复断点：`import_parse_jobs` schema、lease DB API 和定向测试已经存在，worker、原子完成、ASGI 生命周期与前端硬切尚未完成。
* DB 定向基线重新验证为 `37 passed`；后续从 Unit 0b Task 2 继续，不重做已通过的 Task 1。
* 已提供 `ui-prompt-unit-0b-document-parse.md`。在用户返回并确认布局图前，只推进不受 UI checkpoint 阻塞的后端实现，不直接修改本单元 React 布局。

## 2026-07-18 Unit 0b 后端完成记录

* `import_parse_jobs` 已成为唯一解析运行状态：POST 只创建 queued，GET 只读数据库；`parse-status`、同步 reparse 和文件表 provider/progress 字段均从后端正式路径删除。
* ASGI lifespan 启动唯一 `ImportParseWorker`；关闭时先 stop/await worker，再关闭连接池。worker 在无 HTTP 请求时持续领取任务，单项异常隔离，claim 瞬时数据库异常不会杀死循环。
* 长 MinerU 上传、轮询和下载期间新增 lease heartbeat；心跳只延长同一未过期 token，真正失去 token 的旧进程仍被所有 writer fence，避免 60 秒 lease 包裹 600 秒 I/O 时重复 provider 调用。
* 多表终态写统一为 file→chunks→job 锁序：先无锁快检 job fence，再锁不可变来源层级并二次锁定复核 job；删除也在 file/chunks 后按 ID 锁全部 parse jobs，避免外键级联产生隐式反向锁。
* 原子完成固定执行：快检 job/lease/fingerprint → 锁 file/chunks → 锁后复核 job fence → 撤销旧 KG evidence 和 owner/projection → 删除旧 knowledge/chunks → 插入全部新 chunks → 更新 file 摘要 → completed/清 lease。
* 解析指纹升级为 v2，覆盖文件字节、parser、chunker 和全部 MinerU 输出配置；设置 round-trip 同时保留 worker poll/lease，不再因保存无关设置回到 1/60 默认值。
* 新增真实 PostgreSQL 隔离 schema 测试，验证心跳后不可重领、submitting/finalizing 两次过期恢复、单 job completed、旧 chunk/knowledge/evidence 全部撤销、新 snapshot 一次可见、最后来源 KG owner/projection disabled。
* 验证结果：后端目标集 `479 passed`；完整 Python `663 passed / 10 skipped`；隔离 PostgreSQL `3 passed`；Ruff、`python -m cyclops check-config` 和 `git diff --check` 通过。
* ASGI TestClient 早期挂起已定位为受限执行环境禁止 `socketpair.send()`，导致 asyncio 跨线程 self-waker 无法唤醒；full-access 环境最小 TestClient 返回 200，`tests/test_asgi_app.py` 通过，因此未向项目代码加入环境特判或替代 transport。
* Unit 0b 尚未整体完成：React DTO/hooks、列表名称右侧进度、Drawer 同源状态、静态初始焦点、Tooltip 500/400ms、前端构建和 Playwright 验收仍等待用户返回并确认桌面/移动布局图。
