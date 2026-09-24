# 文档级知识图谱行业模式调研

调研日期：2026-07-15（Asia/Shanghai）

## 问题

当前项目以 `document_chunk` 为公开来源，一次任务只抽取一个切片。需要判断成熟 GraphRAG / KG Builder 如何把一篇产品手册的多个切片连接起来，以及本项目应采用什么唯一新契约。

## 官方实现观察

### Microsoft GraphRAG

Microsoft 的默认索引流明确区分 `Document` 与 `TextUnit`：文档先被切成 TextUnit，TextUnit 同时承担抽取输入和 provenance 来源引用。标准流程不是把整篇文档一次性交给模型，而是：

1. 每个 TextUnit 独立抽取一个实体/关系子图。
2. 相同 `title + type` 的实体被合并，描述先聚合成数组。
3. 相同 `source + target` 的关系被合并，描述先聚合成数组。
4. 再用模型把同一实体或关系的多条描述汇总成单一描述。
5. 完整图形成后才进行社区检测和社区报告汇总。

这说明成熟方案的核心是“切片级 Map + 图级 merge/summarize”，而不是多个单切片结果按完成顺序直接 upsert。Microsoft 同时明确保留 Document→TextUnit 链接，以便从知识项回溯原始文本。

FastGraphRAG 是另一条成本更低的路线：实体使用 NLP 名词短语，关系主要来自 TextUnit 共现，通常配合更小的 50–100 token 切片。它适合全局总结，但图更嘈杂，不适合本项目要求人工审核且强调事实关系的管理图谱。

### Neo4j GraphRAG KG Builder

Neo4j 的官方 KG Builder 将流程拆为 loader、splitter、schema builder、lexical graph、entity/relation extractor、pruner、writer 和 entity resolver。与本项目最相关的约束是：

- schema 可以只从输入提取一次，再作为所有切片共同的抽取约束，避免不同切片各自发明类型体系；
- lexical graph 显式保留 `Document`、`Chunk`、相邻切片关系和切片到文档关系；
- 实体/关系仍逐切片抽取；
- pipeline 默认执行 entity resolution，基础策略是相同 label 和 name 精确合并；还提供基于 spaCy embedding 的语义匹配与 RapidFuzz 字符串匹配；
- entity resolver 是单独阶段，职责是识别“代表同一现实对象”的节点，而不是让最后一个切片覆盖前一个切片的数据。

Neo4j 的做法证明：精确同名合并只是最低基线，别名/近义词需要显式 resolution 阶段；同时相邻切片和文档归属应作为来源结构保存，而不是伪装成业务事实关系。

### Amazon Bedrock Knowledge Bases + Neptune Analytics

AWS 的托管 GraphRAG 在 ingestion 时从上传文档自动抽取实体、事实和关系，并明确以“跨多个 document chunks、跨多个 document sources、结合章节标题等结构元素”为目标。API 配置把 enrichment strategy 命名为 `CHUNK_ENTITY_EXTRACTION`，说明底层仍以 chunk 为抽取单位。

查询时先做向量检索，再取得与命中文档切片相连的图节点或 chunk identifier，随后遍历图并扩展相关切片。AWS 的公开文档没有披露实体消歧算法，但产品语义是语料级图与跨切片检索，而不是让用户逐切片维护图谱。

## 行业共同模式

1. **用户入口是文档/数据源，内部执行才是逐切片。** 切片是实现细节和证据单元，不是最终产品操作边界。
2. **Map 后一定有全局阶段。** 至少要做确定性实体合并、关系端点重映射、重复关系聚合；高阶方案再做描述总结、实体消歧和社区分析。
3. **Document、Chunk 与事实分层。** 文档归属、相邻切片和章节结构用于 provenance/context，不应被混成业务实体关系。
4. **实体 resolution 是独立能力。** 精确 name/type 合并可自动执行；语义或模糊合并有误合并风险，应可约束、可解释、可审核。
5. **关系必须有原始证据。** 跨切片共享实体能把局部图连起来，但不能据此凭空生成新的业务关系。新增推断关系需要单独契约和多条明确证据。
6. **整批发布。** 文档 Map/Reduce 完成后一次替换该文档的 KG snapshot，避免多个切片依次提交导致审核 revision 抖动和中间态外露。

## 当前项目差距

- 公开 API 和 UI 都是单切片入口；任务只调用一次模型。
- 实体 ID 只按规范化 name + type，中文别名、简称和近义词不会合并。
- 同一实体/关系的 description、aliases、confidence 会被后完成切片覆盖，没有 Reduce。
- 关系只有在单个切片内被模型明确抽出时才产生；当前所谓跨切片连接只是完全同名实体的稳定 ID 碰巧重合。
- evidence 有 file/chunk/章节/页码/excerpt，但 excerpt 未验证为原文子串，也没有字符 offset。
- 多个切片分别完成会多次修改同一 owner 的 revision，并可能暴露部分文档结果。
- FastAPI `BackgroundTasks` 没有重启恢复；整篇文档会产生多次模型调用，必须升级为可恢复父任务，而不是延长前端轮询。

## 可选方案

### A. 仅自动遍历切片并沿用当前 upsert

文档按钮在后端逐切片调用现有逻辑，仍以每片独立提交。

- 优点：改动最少。
- 缺点：没有真正 Reduce；字段最后写入者获胜；中间态、revision 抖动和部分失败语义都不正确。
- 结论：不推荐，也不符合“干净的 0→1 唯一契约”。

### B. 文档父任务 + 确定性 Reduce

逐切片 Map 到 staging；按 name/type 精确合并实体、按 canonical triple 合并关系；全部成功后原子替换文档 snapshot。

- 优点：正确性和成本可控；易测试和幂等。
- 缺点：仍无法合并“控制台 / 管理后台”等别名。
- 适合：首个可靠基线。

### C. 文档父任务 + 确定性预归并 + 受约束实体 resolution（推荐）

在 B 上增加一个受约束 resolution 阶段：模型只能把已存在的 local entity ID 分组，不得新增实体、关系或证据；类型冲突不自动合并；canonical 名称和最终 ID 由代码确定。关系 Reduce 只重映射并去重 Map 已发现的关系。

- 优点：能处理中文别名，又把模型自由度限制在等价分组；最接近 Microsoft/Neo4j 的成熟分层。
- 缺点：比 B 多一次文档级模型调用；误合并仍需进入人工审核。
- 建议：本轮只做单文档内 resolution；跨文档/FAQ 同义实体另做人工可控契约。

## 推荐的新唯一契约（待用户确认）

- 文档只暴露 `POST /api/import/files/{file_id}/kg-extraction-jobs`，严格空对象请求体；删除公开 `document_chunk + chunk_id` 抽取入口，不留兼容路径。
- FAQ 使用独立的 FAQ 资源路由，不再保留客户端传 `source_type` 的通用入口。
- 一篇文档一条可恢复父任务：`queued → mapping → resolving → reducing → completed | failed`。
- 父任务保存不可变 chunk manifest（ID、顺序、文本指纹、页码/章节、文件指纹）；同一文件只允许一个 active generation。
- Map 只抽取单切片明确支持的候选；evidence 在后端匹配原文并保存 `char_start/char_end`。
- 所有 Map 成功后才做 resolution/Reduce；任一 Map 失败或 manifest 改变则整篇失败，旧 snapshot 保持不动。
- Reduce 不新增跨切片事实关系；只做实体等价映射、canonical triple 去重和 evidence 稳定并集。
- 最终 snapshot 替换与 job completed 同事务；没有任何逐切片候选提前进入审核队列。
- `source_count` 不再使用历史最大值；需要明确按 distinct chunk/document 计算，并单独提供 evidence count。

## 来源

- Microsoft GraphRAG, “Indexing Dataflow”: https://microsoft.github.io/graphrag/index/default_dataflow/
- Microsoft GraphRAG, “Indexing Methods”: https://microsoft.github.io/graphrag/index/methods/
- Neo4j GraphRAG Python, “Knowledge Graph Builder”: https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_kg_builder.html
- Neo4j 官方源码对应文档：https://github.com/neo4j/neo4j-graphrag-python/blob/main/docs/source/user_guide_kg_builder.rst
- AWS Bedrock, “Build a knowledge base with Amazon Neptune Analytics graphs”: https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs.html
- AWS Bedrock, “Create an Amazon Bedrock knowledge base with Amazon Neptune Analytics graphs”: https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs-build.html

