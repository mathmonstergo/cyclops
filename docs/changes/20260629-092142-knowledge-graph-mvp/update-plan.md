# 知识图谱 MVP

> [!IMPORTANT]
> 本文是历史实现记录。KG 当前唯一契约已由
> `docs/changes/20260714-092948-knowledge-graph-retrieval-review/` 取代；其中同步抽取、来源推断和旧状态流转不再可执行。

## 目标

设计并落地第一版轻量知识图谱能力：从已审核知识中抽取实体/关系候选，经过人工审核后形成可追溯结构化知识，后续用于检索增强和客服排查。

## 影响范围

* 数据库 schema：可能新增实体、关系、证据、审核状态相关表。
* AI 抽取逻辑：新增结构化实体/关系候选生成和 JSON 解析。
* 后端 API：候选列表、审核、状态流转、可能的检索扩展接口。
* 检索链路：后续可能在 `retrieval.py` / `rag.py` 中加入 KG 扩召回。
* 管理后台 UI：候选审核列表、实体/关系详情、来源证据追溯。
* 后续可视化：预留局部子图 API 和稳定节点/边数据结构，3D 图谱拆到后续阶段。
* 评测：用现有效果验收工具验证 KG 扩召回收益。

## 初步步骤

1. 已调研本地 RAGFlow GraphRAG 实现，确认其采用“KG 产物写回 doc store + use_kg 合成上下文”的模式。
2. 已调研主流 KG / GraphRAG / 可视化实践，确认 3D 图谱应作为后续消费层，不应替代第一版审核和证据底座。
3. 已确认 MVP 路径采用“独立审核表 + 确认后投影为 KG chunk + 预留局部子图 API”。
4. 已确认第一版实体类型、关系类型采用固定客服领域枚举。
5. 已确认第一版接入最小 KG 检索增强，但仅通过显式开关用于评测/调试，不默认影响客服回答链路。
6. 设计数据模型和状态流转，并预留局部子图查询 API。
7. 设计 AI 抽取输入/输出 JSON 契约。
8. 如涉及 UI，先给用户 UI prompt，等用户确认布局图后再实现。
9. 按 TDD 补后端 schema/API/解析/检索测试。
10. 实现后端和前端最小闭环。
11. 运行后端与前端质量门，并记录验证结果。

## 需要用户确认

* 第一版是否接受“不做 3D，只预留局部子图 API；3D 放到后续阶段”的范围。
* 当前无阻塞范围问题；进入实现前需按 Trellis 读取项目规范并拆分实现步骤。

## 预期效果

* AI 抽取不会直接污染正式知识库。
* 每个实体/关系都能追溯到 FAQ 或文档切片。
* 用户能审核、禁用或确认图谱候选。
* 第一版产出的实体/关系结构可被后续 2D/3D 图谱复用，不需要重建事实来源。
* 实体/关系类型固定，便于审核筛选、检索扩展和后续图谱可视化配色。
* KG 最小检索增强可通过显式开关在评测/调试中验证，不默认影响所有客服回答。
* 后续可以用评测工作台判断 KG 扩召回是否有效。

## 当前确认状态

已确认 MVP 路径；后端第一版底座和前端审核工作台已实现并通过验证。

## 实施记录

* 新增 `cyclops/kg.py`：固定 KG 实体/关系枚举，解析模型 JSON 输出，生成稳定实体/关系 ID，强制证据必填，候选默认 `needs_review`。
* 新增 `cyclops/kg_ai.py`：调用 Chat 模型从单条 FAQ 或文档切片抽取 KG 候选，要求输出固定 JSON schema。
* 新增 `KnowledgeGraphMixin`：保存 KG 抽取候选到 `kg_entities`、`kg_relations`、`kg_evidence`；确认实体/关系后分别投影为 `knowledge_chunks.source_type='kg_entity'` / `kg_relation`。
* 更新 `sql/001_init.sql`：新增 KG 抽取任务、实体、关系、证据表和状态/类型索引。
* 新增 `POST /api/kg/extraction-jobs`：第一版同步执行单来源抽取任务，支持显式 `source_type=faq` / `source_type=document_chunk`，也支持只传 `source_id` 由后端自动识别；任务状态记录 `queued`、`processing`、`completed`、`failed`。
* 新增 KG 审核后端 API：`GET /api/kg/entities`、`GET /api/kg/relations`、确认实体/关系、更新实体/关系状态。
* 审核列表返回证据数组；禁用或待复核实体/关系会同步更新对应 `kg_entity` / `kg_relation` 投影状态。
* 更新默认检索 SQL：普通 `search_knowledge` / `search_knowledge_text` 显式排除 `kg_entity` / `kg_relation`，避免 KG 默认影响客服回答链路。
* 更新评测运行：`POST /api/retrieval/eval-cases/{case_id}/run` 支持 `{"use_kg": true}`，仅在显式开启时调用 KG 召回，并记录 `retrieval_hybrid_v1_kg_debug`。
* 新增局部子图入口：`GET /api/kg/subgraph`，支持中心实体、状态、实体类型、关系类型、跳数和数量限制，供后续 2D/3D 可视化复用。
* 更新 `.trellis/spec/backend/cyclops-db-contracts.md`，记录 KG 审核、投影和显式检索契约。
* 新增 React 管理页 `/knowledge-graph`：沿用现有左侧导航、顶部工具栏、单列表和右侧抽屉结构，不做常驻三栏。
* 前端新增 KG 类型和 React Query hooks：实体/关系列表、确认、状态更新、局部子图、单来源抽取任务。
* 审核 UI 支持实体/关系 tab、状态筛选、类型筛选、当前页搜索、分页、刷新、AI 抽取候选入口。
* 右侧详情抽屉展示实体/关系元数据、证据列表、审核动作；实体已确认后展示局部关系邻域。
* 更新 `.trellis/spec/frontend/components.md`，记录 KG 审核 UI 必须使用“工具栏 + 单列表 + 右抽屉”的约定，3D 可视化作为后续视图。
* 2026-07-01 code review 记录了 10 个 KG 后端问题；已通过后续 `fix(kg)` / `test(kg)` 提交修复确认/状态投影、subgraph hops、JSON fence、job 状态、检索融合兼容等问题，并补充回归测试。
* 2026-07-02 复查 review 修复后的工作树，清理 ruff 报告的 3 个未使用变量/导入，不改变业务行为。
* 2026-07-02 优化文档切片到 KG 抽取的体验：文档抽屉显示并可复制文件 ID，切片工具栏显示并可复制切片 ID，且可从当前可用切片直接触发 `document_chunk` KG 抽取。
* 更新 `.trellis/spec/frontend/components.md`，记录文档/切片 ID 和 KG 单切片抽取入口的 UI 约定。
* 2026-07-02 继续精简切片抽屉工具栏：页码改为 `p14-15` 格式，不再显示 `text` 等 block type；切片工具栏只显示 `复制ID` + icon，完整切片 ID 放在 hover 和复制成功 toast 中。
* 2026-07-02 简化 KG 页面 AI 抽取弹层：移除 FAQ / 文档切片下拉，只保留一个来源 ID 输入；后端根据 `source_id` 自动识别 FAQ 或文档切片，并保留旧 `source_type` payload 兼容。
* 2026-07-06 修复 KG/文档前端页码展示边界：`page_start=0` 不再被当作空值，证据摘要和切片定位会保留封面页或零基页码。
* 2026-07-06 更新 `.trellis/spec/frontend/components.md`，记录 KG/文档证据页码定位必须用显式 nullish 判断，不能用 truthy 判断吞掉 `0`。

## 验证记录

* `conda run -n customer-service-agent python -m pytest`：275 passed。
* `conda run -n customer-service-agent python -m ruff check .`：All checks passed。
* `conda run -n customer-service-agent python -m cyclops check-config`：config ok。
* `pnpm test src/pages/kg/helpers.test.ts`：8 passed。
* `pnpm lint`：通过。
* `pnpm build`：通过；Vite 仍提示单包体积超过 500 kB 的既有构建警告。
* Playwright CLI 打开 `http://127.0.0.1:5173/static/dist/#/knowledge-graph`：页面标题、左侧“知识图谱”导航、实体/关系 tab、空态和“抽取候选”弹层均可见。
* `curl http://127.0.0.1:8765/#/knowledge-graph`：200 text/html。
* `curl http://127.0.0.1:8765/api/kg/relations?limit=1`：200 application/json。
* 2026-07-02 复验：
  * `conda run -n customer-service-agent python -m pytest`：279 passed。
  * `conda run -n customer-service-agent python -m ruff check .`：All checks passed。
  * `conda run -n customer-service-agent python -m cyclops check-config`：config ok。
  * `pnpm test src/pages/kg/helpers.test.ts`：3 frontend test files passed。
  * `pnpm lint`：通过。
  * `pnpm build`：通过；Vite 仍提示单包体积超过 500 kB。
* 2026-07-02 文档切片 KG 入口验证：
  * `pnpm test src/pages/documents/kg-actions.test.ts`：4 frontend test files passed。
  * `pnpm lint`：通过。
  * `pnpm build`：通过；Vite 仍提示单包体积超过 500 kB。
  * Playwright CLI 打开 `/documents` 并打开文档抽屉：快照确认可见“文件 ID”“复制文件 ID”“切片 ID”“复制切片 ID”和“KG 抽取”。
* 2026-07-02 切片工具栏与单 ID 抽取入口验证：
  * `pnpm test src/pages/documents/kg-actions.test.ts`：4 frontend test files passed。
  * `conda run -n customer-service-agent python -m pytest tests/test_admin_server.py -k "kg_extraction_job" -q`：5 passed, 78 deselected。
  * `conda run -n customer-service-agent python -m pytest`：280 passed。
  * `conda run -n customer-service-agent python -m ruff check .`：All checks passed。
  * `conda run -n customer-service-agent python -m cyclops check-config`：config ok。
  * `pnpm lint`：通过。
  * `pnpm build`：通过；Vite 仍提示单包体积超过 500 kB。
  * Playwright CLI 打开 `/knowledge-graph`：快照确认“抽取候选”弹层只保留“来源 ID”输入，没有 FAQ / 文档切片下拉。
  * Playwright CLI 打开 `/documents` 文档抽屉：快照确认切片工具栏显示 `p1-3`、`复制ID`、`KG 抽取`，不显示 `text` 或长切片 ID；点击复制后 toast 显示完整 `chunk_c9ca64929a98`。
* 2026-07-02 切片工具栏高度回调验证：
  * 切片标题增加约 8 个中文字符的视觉宽度上限，避免长标题挤压右侧控件。
  * 工具栏 padding、文字按钮和图标按钮改为紧凑高度，保持单行工具条观感。
  * `pnpm lint`：通过。
  * `pnpm build`：通过；Vite 仍提示单包体积超过 500 kB。
  * Playwright CLI 打开 `/documents` 文档抽屉：快照确认切片定位、`复制ID`、`KG 抽取`、`Embedding`、禁用和编辑按钮仍在同一行。
* 2026-07-02 文件 ID 与 hover 样式统一验证：
  * 文件 ID 复制入口移动到文档抽屉标题右侧，并复用切片处 `复制ID` + icon + 完整 ID tooltip/toast 的格式。
  * 文档抽屉和切片工具栏移除浏览器原生 `title` tooltip，统一使用项目 `Tooltip` / `Popover` 样式。
  * `pnpm test src/pages/documents/kg-actions.test.ts`：11 passed。
  * `pnpm lint`：通过。
  * `pnpm build`：通过；Vite 仍提示单包体积超过 500 kB。
  * Playwright CLI 打开 `/documents` 文档抽屉：快照确认文件标题右侧可见 `复制ID`；hover 文件 ID 显示项目 tooltip 和完整 `imp_5c6d8ff6119a`；hover `KG 抽取` 显示项目 tooltip。
* 2026-07-06 收尾验证：
  * `npx --yes pnpm@10.10.0 --dir web test -- src/pages/kg/helpers.test.ts src/pages/documents/kg-actions.test.ts`：先新增 `page_start=0` 用例并观察到 2 个预期失败；修复后 37 passed。
  * `conda run -n customer-service-agent python -m pytest`：303 passed。
  * `conda run -n customer-service-agent python -m ruff check .`：All checks passed。
  * `conda run -n customer-service-agent python -m cyclops check-config`：config ok。
  * `npx --yes pnpm@10.10.0 --dir web test`：37 passed。
  * `npx --yes pnpm@10.10.0 --dir web lint`：通过。
  * `npx --yes pnpm@10.10.0 --dir web build`：通过；本次构建未出现 Vite chunk size 警告，最大 JS chunk 为 376.46 kB。
  * `git diff --check`：通过，无空白错误。

## 未做事项

* 尚未实现 3D 可视化；当前只预留局部子图 API 和稳定节点/边结构。
* 当前抽取 job 为单来源同步任务，尚未做批量文件抽取和后台异步队列。
* 本地 KG 候选数据为空，未向业务库写入假数据；右侧抽屉的真实数据点选需要先产生实体/关系候选。

## 本地 RAGFlow 调研摘要

* `rag/svr/task_executor.py` 在 `task_type == "graphrag"` 时创建 KB 级 GraphRAG 任务。
* `rag/graphrag/general/index.py` 从已有文档 chunk 构建文档子图，再合并为 KB 全局图。
* `rag/graphrag/utils.py` 将实体和关系写为特殊 chunk，使用 `knowledge_graph_kwd` 区分 `graph`、`subgraph`、`entity`、`relation`、`community_report`。
* `rag/graphrag/search.py` 检索实体、关系和社区报告后，返回一条 `Related content in Knowledge Graph` 合成上下文。
* RAGFlow 的 `use_kg` 是检索/聊天侧开关；生成侧配置包括实体类型、Light/General、实体消歧和社区报告。
* 本项目不能直接照搬 RAGFlow，因为本项目要求 AI 生成结果先人工审核，再进入正式可检索知识。

## 主流实践与 3D 可视化调研摘要

* Microsoft GraphRAG 先做 TextUnits、实体、关系、claims、社区聚类和查询增强，图形展示只是理解结构的辅助层。
* Google Knowledge Graph Search API 强调实体查询和 API 访问，Google RAG Engine 强调 corpus/import/retrieval 等数据和检索底座。
* 云厂商和图数据库的 GenAI 方案通常先把图谱存储、查询、权限和质量做好，再提供图浏览器或图探索工具。
* 3D 图谱适合后续探索、演示和局部排查，不适合第一版承担审核主流程。
* 本项目当前前端没有 Three.js、3d-force-graph、D3、Cytoscape、Sigma 等依赖；做 3D 会引入新的前端渲染栈和浏览器验证成本。
* 推荐路线：第一版做可信 KG 底座和局部子图 API；第二阶段做图谱治理 UI；第三阶段做 3D 可视化图谱。
