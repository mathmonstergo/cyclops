# Confirmation

## 2026-07-14

* 用户要求查看项目情况、继续开发，并重点 review 整个知识图谱板块。
* 用户询问文档 embedding 如何使用 BM25，以及 BM25 是否需要预先存储分词。
* 已完成只读代码审查、质量门和数据库状态统计。
* 已确认当前实现不是 BM25；运行时 lexical 检索为固定权重 `ILIKE`，`to_tsvector('simple', search_text)` GIN 索引未被使用。
* 已提出推荐范围：先修复 KG 闭环和统一检索 correctness，把真正中文 BM25 拆为评测后的独立改动。
* 用户回复“修”，确认按推荐范围继续：本轮先完成 KG 闭环和统一检索 correctness，真正中文 BM25 独立立项评测后再实现。
* 确认时间：2026-07-14（Asia/Shanghai）。
* 后续如发现必须扩大到新的搜索扩展、独立搜索服务或默认启用 KG，将重新向用户确认。
* 用户补充项目级约束：所有 0→1 开发只保留最干净的单一新契约，不增加旧兼容、同步适配入口、双路径或静默 fallback。
* 据此确认 KG 请求采用显式 `source_type + source_id`，不保留只传 `source_id` 的自动推断形状；任务只走 POST queued、后台执行、GET 轮询。
* KG 候选完成只走带必填 source guard 的原子入口；进入 `usable` 只走 confirm，普通 status 入口只负责退回待审核或停用。

## 2026-07-15 follow-up

* 用户再次明确“本项目所有从 0→1 开发都不需要兜底兼容，要朝最干净方向制作”，并指出同步包装类方法不应保留；随后要求继续完成开发并授予 full access。
* 据此继续执行唯一当前契约：KG confirm 必填且只接受 `expected_revision`，不提供空 body、`{}`、默认 revision、id-only overload 或旧路由；快照冲突返回 409 并要求刷新后重新人工确认。
* 技术实现采用确定性全局 entity→relation 两阶段锁序并禁止 deadlock retry；这是为满足已确认的 KG correctness/无兼容范围所做的并发一致性设计，不改变默认问答仍关闭 KG 的产品边界。
* 同一无兼容约束扩展到本轮复扫发现的臆造路径：`parse_progress` 只允许 JSON object，文件级 chunker 只读取持久化 canonical 枚举，不再接受不存在的字符串记录或静默 `naive` 回退。
* 确认时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 document workflow follow-up

* 用户确认 MinerU 文档解析由后端持续推进；离开文档页或关闭浏览器后任务也必须继续查询进度并最终落盘，前端不再承担任务执行权。
* 用户确认列表页必须显示解析进度，但不新增独立列；紧凑进度条放在文件名称右侧并与名称贴近，抽屉继续显示完整阶段与页数详情。
* 用户确认硬删除文档/FAQ 来源后不删除 KG owner 历史；失去全部证据的实体、关系及投影自动转为 `disabled`，仍有其他来源证据的共享事实保留并退回 `needs_review`。
* Tooltip 初始焦点和整篇文档 KG 抽取语义仍待逐项确认，确认前不实施。
* 确认时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 industry research and list selection follow-up

* 用户要求先调研成熟大厂/设计系统如何处理跨切片知识图谱、Tooltip 和列表批量操作。
* 用户明确提出“只要有列表就得支持多选操作功能”。该原始要求已记录；“列表”是否限定为具备合法共同批量动作的可变业务资源列表，仍待用户确认，当前未擅自扩大到导航、引用、候选、诊断和设置表面。
* 已完成 Microsoft GraphRAG、Neo4j、AWS、IBM Carbon、Microsoft Fluent、Ant Design、Gmail、Google AIP、Radix 和 WAI-ARIA 官方资料调研，并完成全站列表/API 只读审计。
* 调研阶段没有修改业务代码、数据库或运行服务；后续设计确认前不实现新的大范围功能。
* 记录时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 multiselect scope confirmation and 3D evolution

* 用户确认全站多选采用推荐范围：所有具备合法共同批量动作的可变业务资源列表必须支持多选；导航、只读引用、候选、诊断和设置表面不加入无意义复选框。
* 第一阶段业务资源表固定为文档、FAQ、KG 实体、KG 关系和评测用例；检索别名、文档切片和会话在具备明确批量维护动作后使用专用管理模式。
* 用户提出知识图谱后期需要 3D 点线关系可视化，并要求参考网上成熟、权威的实现方案。
* 3D 可视化先作为明确的后续演进方向进行官方资料和技术选型调研；是否进入本轮实现及具体交互仍待后续逐项确认。
* 确认时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 Neo4j architecture reassessment

* 用户询问为何不引入 Neo4j，并要求重新评估 Neo4j 对本项目的优点后共同决定是否更换。
* 已完成当前 schema/事务边界、真实 KG 工作负载、统一检索链路，以及 Neo4j 2026.06 Cypher、vector/full-text、GraphRAG、GDS、Community/Enterprise 运维边界的只读调研。
* 当前仅形成三种可选边界和推荐意见，没有把“暂不引入”改写成用户确认，也没有修改业务代码、数据库或服务。
* 当时待确认的核心决策是继续 PostgreSQL-only，或把“PostgreSQL 唯一写模型 + Neo4j 可重建图读模型”确认为达到 POC 门槛后的目标架构；该历史待确认项已被下节的方案 B 确认取代。
* 记录时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 Neo4j scheme B confirmation and BM25 re-audit

* 用户确认采用方案 B：PostgreSQL 保持唯一来源/审核写模型，Neo4j 作为可重建图查询读模型；不得演变为两个权威库、长期双读或 PostgreSQL 图查询 fallback。
* 用户要求同时重新检查现有检索逻辑是否完善，并确认此前 BM25 问题是否已经修复。
* 当前已知基线是 lexical 仍为 PostgreSQL `ILIKE`，真正中文 BM25 尚未实现；本轮将重新追踪运行路径、状态门禁、索引与测试后再确定唯一实现边界。
* 方案 B 的图读模型范围已经确认；BM25 留在 PostgreSQL 检索层、进入 Neo4j 图读模型，还是作为后续统一知识读层能力，仍需基于审计结果单独确认，不能静默把方案 B 扩为方案 C。
* 确认时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 retrieval/BM25 audit result

* 代码、测试与只读数据库审计确认 BM25 尚未实现：FAQ/document 与 KG lexical 都仍为 weighted `ILIKE`，schema 中的 `to_tsvector('simple', search_text)` GIN 没有运行查询使用。
* 已确认全部正式入口统一到 `HybridRetrievalService`，实时 FAQ/document 状态、embedding、文件/切片禁用、direct child 和 parent context 门禁基本正确；但中文分词、最终 score 语义、rerank 完整性和真实规模评测仍不完善。
* 当前 PostgreSQL 为 16.14，只安装 `plpgsql,vector`；当前 retrieval evaluation case/run 和 query analytics 都为 0，不能声称现有检索质量已通过业务验证。
* 已形成三种候选：A 为 PostgreSQL `pg_search + pdb.jieba` 的 `retrieval_hybrid_v2`（推荐）；B 为升级 PG17/18 后采用 `pg_textsearch + zhparser`；C 为仅实施 Neo4j 方案 B 并继续暂缓 BM25。
* 方案 A 不要求应用额外保存分词列；canonical 原文继续保存到 `search_text`，token、倒排 postings、TF/DF 和文档长度由 BM25 索引内部管理。
* 该条是确认前的历史记录：当时只记录审计结论和推荐，尚未把方案 A 记为用户确认，也未安装扩展、修改业务代码、数据库或服务；后续确认见下一节。
* 记录时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 retrieval_hybrid_v2 implementation confirmation

* 用户在了解 `pg_search` Community 的 AGPL-3.0 来源、BM25 在 PostgreSQL 内部执行而不是外部模型 API 后，明确回复可以接受此前推荐方案并要求直接开始。
* 据此确认 PostgreSQL `pg_search + pdb.jieba` 为 FAQ/document 中文 BM25 的唯一目标；不新增 Elasticsearch/OpenSearch 等第二套全文服务，也不把文档 chunk 投影到 Neo4j。
* `retrieval_hybrid_v2` 达到真实 PostgreSQL、中文金标、生命周期和延迟门后一次硬切；删除 weighted `ILIKE`、闲置 `simple` GIN、运行时 backend switch 与 fallback。
* Neo4j 继续严格采用方案 B：PostgreSQL 唯一写模型，Neo4j 只保存可重建 usable graph；两个实施单元分别设计、分别验证。
* 当前采用 Community 扩展用于本地项目实现与验证；若未来把含该扩展的闭源产品交付外部客户，仍需在交付前完成 AGPL 合规或切换商业许可，不能把本次技术确认视作法律结论。
* 确认时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 implementation start confirmation

* 用户在方案 B、BM25 原理与 AGPL-3.0 边界说明完成后再次回复“可以接手你之前的推荐方案，直接开始做吧”，授权按已确认设计进入代码实施和本地依赖部署。
* 实施顺序固定为 KG 来源正确性 → retrieval hybrid v2 → Neo4j 图读模型；不把错误的 canonical KG 状态先同步到 Neo4j。
* 来源重新启用时，保留的 live evidence 只使 owner 恢复为 `needs_review`，不自动恢复 `usable`；正文变化已删除 evidence 时仍需重新抽取。
* 继续遵守不增加旧兼容、双路径、backend switch 或静默 fallback，且不自动 Git commit。
* 确认时间：2026-07-15（Asia/Shanghai）。

## 2026-07-15 Unit 0 implementation record

* 已按上述授权完成 KG 实时来源计数、历史 evidence 有效性、子图 live count 和统一前端 cache invalidation。
* 实施中没有增加字段默认、方法别名或第二条执行路径；质量复审发现的 `evidence_count` 缺字段默认 0 也已删除。
* 本节只记录已确认方案的实施结果，没有新的用户决策或范围扩张。

## 2026-07-18 Unit 0c continuation

* 用户在要求检查当前进度后连续回复“继续”，并确认已授予 full access。
* 据此恢复已确认的文档级 KG 方案 C，从后端 Unit 0c Task 1 开始实施 evidence 精确 offset 与纯函数 Map/Resolve/Reduce；不扩大到未确认的新产品范围。
* Unit 0c 前端仍遵守布局图确认门槛，本次恢复不据此直接修改 React 布局。
* 确认时间：2026-07-18（Asia/Shanghai）。
