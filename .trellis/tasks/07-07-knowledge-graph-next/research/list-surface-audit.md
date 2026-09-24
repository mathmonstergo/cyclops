# 全站列表与批量契约审计

审计日期：2026-07-15（Asia/Shanghai）

本审计覆盖 `web/src` 的用户可见列表表面，以及对应 admin/database 批量能力。当前前端没有任何资源行多选实现；后端只有 FAQ batch status 是真正面向业务资源的同步批量事务。

## 一级业务资源列表（建议第一阶段统一多选）

| 资源 | 前端表面 | 当前单项动作 | 分页/筛选现状 | 推荐批量动作 | 后端现状 |
| --- | --- | --- | --- | --- | --- |
| 文档 | `DocumentsPage.tsx`、`documents/document-list.tsx` | 解析、整文件 Embedding、生成问题、下载、启停、删除 | 查询/解析状态；最多 100 条，无分页 UI | 启用/禁用、生成 Embedding、删除；解析走任务队列 | 全部只有单文件 API；整文件 Embedding 是逐切片提交 |
| FAQ | `FaqsPage.tsx`、`faqs/faq-list.tsx` | 保存、状态、AI 优化、Embedding、KG 抽取 | 服务端查询/状态/Embedding；30 条分页 | 批量可用/待复核/禁用、选中项 Embedding | 有 `batch-status` 真事务，但缺失 ID 会静默忽略；`embed-pending` 是逐项循环 |
| KG 实体 | `KnowledgeGraphPage.tsx::EntityTable` | confirm、退回待审核、停用、证据/局部关系 | 服务端状态/类型；30 条分页；文本只过滤当前页 | 批量 confirm/待审核/停用 | 无 batch；confirm 必须逐项带 `expected_revision` |
| KG 关系 | `KnowledgeGraphPage.tsx::RelationTable` | confirm、退回待审核、停用、证据 | 服务端状态/类型；30 条分页；文本只过滤当前页 | 批量 confirm/待审核/停用 | 无 batch；需要端点/证据和 revision 原子校验 |
| 评测用例 | `EvaluationPage.tsx`、`evaluation/case-list.tsx` | 运行、编辑、启停、设置期望命中 | 状态服务端；最多 100 条；文本只过滤当前结果，无分页 UI | 运行选中项、批量启用/禁用 | “批量运行”只是浏览器顺序调用单条 run，无持久 batch job |

## 次级资源（按专用维护场景第二阶段处理）

| 资源 | 当前表面 | 判断 |
| --- | --- | --- |
| 检索别名 | `evaluation/alias-panel.tsx`，UI 只显示前 16 条，当前列表只返回 active | 批量启停有意义，但应先让 disabled 可见并补齐分页/搜索，随后再做多选 |
| 文档切片 | `documents/chunk-browser.tsx::ChunkNav` | 是文档子资源，但当前首先承担单选导航；若增加批量启停/Embedding，应进入显式“批量维护模式”，不常驻 checkbox；文档级 KG 后不再提供切片 KG 批量入口 |
| 会话 | `assistant/conversation-list.tsx`，Zustand/localStorage | 更适合“管理会话/清空历史”专用命令；若未来提供选中删除，可在管理模式出现，不套用后端业务表格契约 |

## 不应加入通用多选的列表表面

- KG evidence、实体抽屉局部关系：附属 provenance/投影，应从主实体/关系列表维护。
- 评测候选、KG trace facts、召回通道、诊断卡：运行快照或派生诊断，不是独立资源。
- 助手消息、消息快速导航、处理步骤、命中来源：内容序列、导航索引或回答引用。
- 文档源块、假设问题、任务进度：切片内部结构或临时状态；若未来转为可审核资源，需要先建立独立生命周期。
- 设置卡片、FAQ/评测指标、标签 chips、Sidebar、命令面板、筛选项、分页项、上传临时文件、Toast、Markdown 内列表/表格：异构 singleton、聚合、控件选项或用户内容。

这些表面没有同类资源的共同合法批量动作。增加 checkbox 会造成选中后无动作、作用域不清或误改派生数据。

## 当前后端批量能力事实

### 真正同步事务

- FAQ `POST /api/faqs/batch-status` 使用一次事务更新 FAQ，并同步知识投影和来源 KG 回退；但目前对不存在 ID 静默忽略、没有 revision，前端也未接入。
- 文档整份替换切片与 KG 抽取 snapshot replacement 是内部原子事务，不是列表批量 API。
- 文档候选生成的 parent job/items 创建是原子的，但执行逐项提交，允许部分失败。

### 名义批量但实际逐项提交

- FAQ `embed-pending` 顺序调用单条 embedding。
- 文档整份 embedding 按切片逐项提交。
- 文档假设问题生成按切片部分成功。
- 评测批量运行由前端顺序调用单用例接口，刷新后丢失批次状态。
- 候选 FAQ 保存与候选状态更新分属两个事务，单项原子性尚未成立。

## 推荐批量 API 边界

### 同库短操作：同步原子

适用于启用/禁用、审核状态、删除业务记录等纯数据库操作：

- 每种资源和语义使用明确 endpoint，不做万能 `action` 字段；
- body 只接受非空、去重、有限数量的稳定 ID；审核资源同时带每项 `expected_revision`；
- 锁内验证全部 ID 存在、revision 和领域门禁全部成立；
- 任一目标缺失或冲突，整个事务回滚；禁止静默跳过或 HTTP 207；
- 400 表示 payload/状态非法，404 表示任一目标不存在，409 表示任一 revision 变化，200 表示全部成功；
- KG batch 必须一次收集全集并遵守 entity→relation 确定性锁序，不能事务内循环调用单项方法。

### 外部调用/长任务：持久异步批次

适用于解析、Embedding、问题生成、评测运行和 KG 抽取：

- 创建 parent job 和全部 items 的事务必须原子；
- 每个 item 独立持久化 queued/running/succeeded/failed/conflict/skipped；
- worker claim 原子，外部调用期间不持有数据库锁；
- 浏览器关闭后继续执行；刷新后可按 job/resource 恢复观察；
- partial success 明确显示逐项原因；重试只生成失败/冲突项的新 job，不重复成功项。

### 文档删除的特殊边界

当前单文档删除先提交数据库，再 `unlink()` 原件，PostgreSQL 与文件系统并不原子。批量删除不能声称所有介质完全同步回滚。推荐数据库事务一次删除全部业务数据并写 cleanup/outbox；文件删除由可重试 item 完成，UI 区分“业务数据已删除”和“文件清理失败”。

## 第一阶段统一选择契约

- 只覆盖文档、FAQ、KG 实体、KG 关系、评测用例。
- selection state 以稳定 ID 为键；KG 额外保存用户看到的 revision snapshot。
- 表头 checkbox 只选择当前已渲染页；当前列表存在 30/100/16 条截断和本地过滤，第一阶段不声称跨页全选。
- 翻页、修改查询/筛选、切换实体/关系 tab 时清空选择；实体与关系使用独立选择集合。
- 选中后展示 `已选择 N 项` 和所有项目共同合法的动作；不适用动作不显示，不做点击后静默跳过。
- 危险动作写明资源类型、数量和后果并二次确认。
- 自动刷新可以保留仍存在的选中 ID；资源消失或 revision 变化时移出选择并提示刷新。

## 结论

“只要有列表就多选”若解释为所有视觉列表，会覆盖大量导航、引用和诊断表面，缺少合法动作。推荐把可执行规范写成：

> 所有存在合法共同批量动作的可变业务资源列表必须支持多选；导航、只读引用、派生诊断与异构设置列表不提供通用多选。

这既完整覆盖用户真正需要提效的资源表，也能形成可复用、可测试的产品契约。

