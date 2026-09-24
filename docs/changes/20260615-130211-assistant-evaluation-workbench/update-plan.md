# 平台问答效果验收工具

> [!IMPORTANT]
> 本文仅记录早期评测工作台。当前只使用 v2 `latest_runs` 多策略契约，详见
> `docs/changes/20260714-092948-knowledge-graph-retrieval-review/`；不得恢复 singular `latest_run` 或旧候选 DTO。

## 修改目标

在内部平台增加一个问答/检索效果验收工作台，让团队能维护标准问题、运行检索评测、查看命中来源和指标，从而判断知识库资料、切块、关键词、别名和检索策略是否有效。

## 用户确认的范围

用户确认可以继续下一阶段。前一阶段已明确：当前优先内部平台问答效果，不处理微信、CLI、MCP、外部 API、权限、限流、审计；知识图谱和图谱可视化作为后续方向。

## 影响范围（计划）

* 后端：复用或小幅扩展 `AdminApp` 检索评测 API。
* 数据库：优先复用 `retrieval_eval_cases`、`retrieval_eval_runs`、`retrieval_aliases`。
* 前端：新增独立评测工作台页面、API hooks、类型定义。
* 测试：补充后端/前端关键行为验证。

## 具体步骤

1. MVP 范围已收敛：只做检索评测工作台，不同步做完整问答回放，不实现 agentic planner / tool loop。
2. 第一版只支持运行单条评测用例；批量运行全部 active 用例后置。
3. 梳理现有 eval-cases/run/aliases API 返回结构，确定是否需要补最近运行结果读取或编辑接口。
4. 设计页面信息层级：用例列表、编辑区、运行结果、候选来源、别名词典。
5. 补后端缺口和测试。
6. 补前端类型、hooks、页面和构建验证。

## 预期效果

* 内部能用固定问题集稳定复现“命中/没命中/错命中”。
* 能明确是资料缺失、切块不准、关键词别名缺失，还是检索策略问题。
* 为后续知识图谱增强提供可量化对照集。
* 为后续 agentic RAG 评测保留 `strategy` / `run_type` 和运行轨迹展示扩展点，但本次不扩大实现范围。

## 需要用户确认的问题

当前无阻塞问题。第一版已确认采用 Approach A：只做检索评测工作台，不跑完整 LLM 回答生成；页面入口采用独立页面。批量运行全部 active 用例后置，第一版先做单条运行。

## Research References

* `.trellis/tasks/06-15-assistant-evaluation-workbench/research/agentic-rag-patterns.md`：成熟项目中 agentic RAG 的共识是“LLM 决策循环 + 工具调用 + 检索/改写/打分/重试/轨迹”，本任务只预留命名和运行详情扩展点。

## Implementation Plan

### Step 1: 后端评测运行回放

* 先在 `tests/test_admin_server.py` 增加失败测试，要求 `AdminApp.list_retrieval_eval_cases()` 返回数据库层提供的 `latest_run` 字段，页面刷新后仍能显示最近一次评测结果。
* 先在 `tests/test_admin_server.py` 更新单条运行测试，要求 `strategy` 使用 `retrieval_hybrid_v1`，避免后续 agentic 运行模式接入时含义不清。
* 在 `customer_service_agent/db/retrieval_meta.py` 的 `list_retrieval_eval_cases()` 增加 latest run 查询：每个 case 只取 `retrieval_eval_runs.created_at DESC` 最新一条，字段包含 `id`、`case_id`、`strategy`、`retrieved_items`、`metrics`、`analysis`、`created_at`。
* 在 `customer_service_agent/admin_server.py` 把单条 run 记录的 `strategy` 改为 `retrieval_hybrid_v1`。
* 运行聚焦测试：`python -m pytest tests/test_admin_server.py -q -k "retrieval_eval"`。

### Step 2: 前端布局确认

* 按项目 UI 规则，先给用户一段可用于生成后台评测页布局图的 prompt。
* 用户确认布局图后，再实现前端页面、API schema/hooks、路由/sidebar 入口。

### Step 3: 前端评测工作台实现

* 新增 `web/src/pages/EvaluationPage.tsx` 和 `web/src/pages/evaluation/*`，采用与智能问答页一致的路由内布局：左侧 240px 工作栏 + 右侧主面板 header + 内容区。
* 在 `web/src/api/schemas.ts` 增加 `RetrievalEvalCase`、`RetrievalEvalRun`、`RetrievalAlias` 类型。
* 在 `web/src/api/hooks.ts` 增加 eval cases、save case、run case、aliases、save alias hooks。
* 在 `web/src/main.tsx` 和 `web/src/components/layout/sidebar.tsx` 增加 `/evaluation` 路由和导航入口。
* 验证：`npm run build`，并按现有前端 lint 状态说明是否存在历史 lint 问题。

## 实施记录

* 已新增 `.trellis/tasks/06-15-assistant-evaluation-workbench/research/agentic-rag-patterns.md`，记录 LangGraph、LlamaIndex、Haystack、Dify、AutoGen、CrewAI 等项目对 agentic RAG 的定义和组件拆分。
* 已将 PRD 收敛为：第一版只做单条检索评测运行，批量运行全部 active 用例后置。
* 已修改 `RetrievalMetaMixin.list_retrieval_eval_cases()`：用例列表返回 `latest_run`，便于页面刷新后回放最近一次指标、候选和分析。
* 已修改 `AdminApp.run_retrieval_eval_case()`：运行策略名从 `hybrid_v1` 收敛为 `retrieval_hybrid_v1`，为未来 `agentic_rag_v1` 留出清晰扩展口径。
* 已补充后端测试，覆盖 `latest_run` 查询和运行策略名。
* 已新增 `ui-layout-prompt.md`，用于按项目 UI 规则先生成/确认评测工作台布局图。
* 已新增 `/evaluation` 前端页面、路由入口、侧栏导航、API 类型和 hooks。
* 已按用户最新反馈把评测页布局收敛到智能问答页骨架：全局顶栏补“效果验收”标题，路由内左栏固定 240px，右侧主面板 header 起点和高度与智能问答页一致。
* 已将用例编辑和别名词典都做成右侧抽屉，避免主页面常驻右栏导致页面切换位置不一致。
* 已更新 `.trellis/spec/frontend/components.md`，沉淀“功能页路由内左栏 + 主面板 header 需要对齐智能问答页”的项目级布局约定。

## 验证记录

* `conda run -n customer-service-agent python -m pytest tests/test_db.py::test_list_retrieval_eval_cases_includes_latest_run tests/test_admin_server.py::test_admin_app_run_retrieval_eval_case_records_hybrid_result -q`：先按 TDD RED 失败，再实现后通过，最终 `2 passed`。
* `conda run -n customer-service-agent python -m pytest tests/test_admin_server.py -q -k "retrieval_eval"`：`4 passed, 61 deselected`。
* `conda run -n customer-service-agent python -m pytest tests/test_db.py -q -k "retrieval_eval"`：`2 passed, 31 deselected`。
* `conda run -n customer-service-agent python -m ruff check customer_service_agent/admin_server.py customer_service_agent/db/retrieval_meta.py tests/test_admin_server.py tests/test_db.py`：通过。
* `npm run build`（`web/`）：通过，Vite 提示单个 chunk 超过 500 kB，为现有打包体积提示。
* `npx eslint src/pages/EvaluationPage.tsx src/pages/evaluation/alias-panel.tsx src/pages/evaluation/case-drawer.tsx src/pages/evaluation/case-list.tsx src/pages/evaluation/result-panel.tsx src/pages/evaluation/helpers.ts src/api/hooks.ts src/api/schemas.ts src/components/layout/sidebar.tsx src/components/layout/topbar.tsx src/main.tsx`（`web/`）：通过。
* `npm run lint`（`web/`）：未通过，失败点为既有 `react-refresh/only-export-components` 和 `react-hooks/set-state-in-effect` 规则问题，集中在 `components/ui/*`、`FaqsPage.tsx`、`assistant/provider-drawer.tsx`、`documents/chunk-browser.tsx`、`faqs/faq-drawer.tsx`，不属于本次评测页新增文件。
* Playwright 静态构建检查：`/assistant` 与 `/evaluation` 在 1024x720 下均为全局侧栏 196px、路由内左栏 240px、主面板 header `x=436 y=42 h=62`；`/evaluation` 别名词典抽屉宽 520px，不挤压主布局。
