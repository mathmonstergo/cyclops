# 资源列表多选与原子/持久批量操作 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task, and use superpowers:test-driven-development for every behavior change. Steps use checkbox (`- [ ]`) syntax for tracking；本项目不自动提交 Git commit，不保留客户端逐项循环、万能 action endpoint、旧路由别名或同步长任务 fallback。

**Goal:** 为文档、FAQ、KG 实体、KG 关系和检索评测用例提供一致的当前页多选体验、真正原子的同步批量命令，以及关闭浏览器后仍可完成和重试的持久异步批次。

**Architecture:** 前端使用稳定资源 ID 维护当前已渲染页的三态选择，并由每页纯函数求交集，只显示全部选中项共同合法的动作。短事务由每种资源/语义的显式 batch endpoint 在一个 PostgreSQL 事务中锁定、验证并整体提交；解析、Embedding、KG 抽取、评测运行和文件清理由闭集 `batch_operation_jobs + batch_operation_job_items` 持久化，ASGI lifespan worker 负责推进，父任务状态从逐项事实派生。

**Tech Stack:** Python 3.11、FastAPI、psycopg 3、PostgreSQL 16、React 19、TypeScript 6、TanStack Query、Radix Dialog、pytest、Node test runner、Chrome DevTools。

---

## 固定范围与执行前置

本计划只覆盖第一阶段五类一级业务资源：文档、FAQ、KG 实体、KG 关系、retrieval eval case。KG evidence、文档切片、检索别名、会话、候选/诊断/设置表面不增加通用 checkbox；首版也不提供“选择全部筛选结果”。

执行顺序固定为：

1. 先完成 Unit 0 KG 来源正确性，使文档/FAQ 状态变化能在同一事务精确重算 KG。
2. 先完成 Unit 0b 的 `import_parse_jobs` 和可恢复解析 worker；批量解析只创建并关联这些 canonical child jobs，不再实现第二套解析状态机。
3. 先完成文档级 KG 的 canonical `kg_extraction_jobs`，其公开来源只允许 `faq` 或整篇 `document`；批量 KG 抽取只创建并关联这些 child jobs，不恢复单切片公开入口。
4. 再执行本计划。旧同步整文件 Embedding、FAQ `embed-pending`、浏览器评测循环和公开 KG job 创建路由在本单元硬切，不同时运行两套入口。

批量上限固定为 `100` 项，与首版“当前页”最大响应量一致。请求中的 ID 必须为非空字符串、无重复、数量 `1..100`；不接受数组下标、query/filter 或 `all=true`。

## File map

### Backend and schema

- Modify: `sql/001_init.sql` — 创建 batch parent/items、active/claim index、JSON object check、eval batch idempotency key；删除已被当前批次替代的旧派生入口所需 schema。
- Create: `cyclops/batch_operations.py` — 闭集 operation/status 常量、严格 payload 解析、资源指纹和 job 响应映射；不暴露万能 action dispatcher 给 HTTP。
- Create: `cyclops/db/batch_jobs.py` — parent/items 创建、读取、资源近期任务、lease claim、fenced transition、child-job reconciliation 和 retry persistence。
- Modify: `cyclops/db/__init__.py` — 组合 `BatchJobMixin`，只导出当前 batch DTO/错误类型。
- Modify: `cyclops/db/imports.py` — 文档批量启停/删除、全集 KG/knowledge 清理、cleanup job 同事务写入、批量解析/Embedding job 快照。
- Modify: `cyclops/db/faq.py` — FAQ 严格原子状态批改、Embedding/KG batch job 快照；删除静默缺失 ID 行为和旧候选扫描入口。
- Modify: `cyclops/db/kg.py` — entity/relation 批量 confirm/status，全集 revision/门禁检查及全局 entity→relation 锁序。
- Modify: `cyclops/db/retrieval_meta.py` — eval case 原子状态批改、batch run 输入指纹和按 batch item 幂等保存运行。
- Create: `cyclops/batch_worker.py` — 可恢复 batch item worker；外部调用不持有事务锁，所有落库使用 lease token/fingerprint fence。
- Modify: `cyclops/admin_server.py` — 每种资源/语义的显式命令、job 创建/读取/重试；删除同步长任务与浏览器伪批量对应方法。
- Modify: `cyclops/asgi_app.py` — 注册当前 batch routes，在 lifespan 启停 worker，删除旧长任务创建 routes。
- Modify: `cyclops/config.py`, `.env.example` — `BATCH_WORKER_POLL_INTERVAL_SECONDS` 和 `BATCH_WORKER_LEASE_SECONDS` 严格配置。
- Modify: `.trellis/spec/backend/cyclops-db-contracts.md`, `.trellis/spec/backend/cyclops-asgi-admin-contracts.md` — 完成后记录原子/异步批次和锁序契约。

### Backend tests

- Create: `tests/test_batch_operations.py` — payload、父/子状态派生、retry、worker、fingerprint/fence 和 cleanup 路径单元测试。
- Create: `tests/test_batch_operations_postgres.py` — 真实 PostgreSQL 原子回滚、并发锁序、lease recovery、幂等 eval run 和 cleanup outbox 测试。
- Modify: `tests/test_db.py` — 各 domain mixin 的 SQL/方法契约和旧入口零存在测试。
- Modify: `tests/test_admin_server.py` — 显式 AdminApp 方法、错误映射、长任务硬切测试。
- Modify: `tests/test_asgi_app.py` — route 表、单请求 batch、lifespan worker 和旧 route 零存在测试。
- Modify: `tests/test_config.py` — batch worker 配置范围测试。
- Modify: `tests/test_kg_postgres_concurrency.py` — batch entity/relation 与现有单项审核并发时的全局锁序回归。

### Frontend

- Modify: `web/src/api/schemas.ts` — selection 所需活动任务字段、batch job/item DTO 和严格 request types。
- Modify: `web/src/api/hooks.ts` — 显式 batch mutations、recent/job polling、统一终态 cache invalidation；删除旧长任务 hooks。
- Create: `web/src/components/batch/page-selection.ts` — stable-ID selection reducer、三态、当前页 toggle、scope 清空和消失项 reconcile。
- Create: `web/src/components/batch/selection-checkbox.tsx` — native checkbox 的 `indeterminate`/ARIA 当前契约。
- Create: `web/src/components/batch/batch-action-bar.tsx` — 紧凑 `已选择 N 项`、共同动作和取消选择。
- Create: `web/src/components/batch/batch-danger-dialog.tsx` — 带资源类型、数量和影响的危险动作确认。
- Create: `web/src/components/batch/batch-job-status.tsx` — 持久任务进度、逐项错误和失败/冲突项 retry。
- Modify: `web/src/pages/DocumentsPage.tsx`, `web/src/pages/documents/document-list.tsx`, `web/src/pages/documents/document-drawer.tsx` — 文档多选、动作矩阵、异步 cleanup/parse/embed/KG job。
- Create: `web/src/pages/documents/batch-actions.ts` — 文档共同合法动作纯函数。
- Modify: `web/src/pages/FaqsPage.tsx`, `web/src/pages/faqs/faq-list.tsx`, `web/src/pages/faqs/faq-drawer.tsx` — FAQ 多选和持久 Embedding/KG job；删除全库 `embed-pending` 按钮。
- Create: `web/src/pages/faqs/batch-actions.ts` — FAQ 共同合法动作纯函数。
- Modify: `web/src/pages/KnowledgeGraphPage.tsx`, `web/src/pages/kg/helpers.ts` — entity/relation 独立选择、revision snapshot 和批量审核。
- Create: `web/src/pages/kg/batch-actions.ts` — KG 共同合法动作和 wire item 构造。
- Modify: `web/src/pages/EvaluationPage.tsx`, `web/src/pages/evaluation/case-list.tsx`, `web/src/pages/evaluation/batch-panel.tsx` — eval case 多选和持久运行；诊断面板不再拥有“运行全部筛选结果”命令。
- Delete: `web/src/pages/evaluation/batch-state.ts`, `web/src/pages/evaluation/batch-state.test.ts` — 删除只存在浏览器内的伪 batch 进度真相。
- Create: `web/src/pages/evaluation/batch-actions.ts` — eval case 共同合法动作纯函数。
- Modify: `.trellis/spec/frontend/components.md` — 完成后记录 selection scope、batch bar 与危险确认。
- Modify: `docs/changes/20260714-092948-knowledge-graph-retrieval-review/update-plan.md`, `docs/changes/20260714-092948-knowledge-graph-retrieval-review/confirmation.md` — 实施完成后记录实际 wire 与验证证据。

### Frontend tests

- Modify: `web/src/api/schemas-contract.test.ts` — batch DTO、必填字段和已删除旧 wire。
- Create: `web/src/api/batch-hooks-contract.test.ts` — 每个 batch hook 只发送一个显式 endpoint 请求。
- Create: `web/src/components/batch/page-selection.test.ts` — stable ID、三态、scope/reconcile。
- Create: `web/src/components/batch/batch-ui-contract.test.ts` — ARIA、数量确认和无“全部匹配结果”。
- Create: `web/src/pages/documents/batch-actions.test.ts` — 文档动作交集。
- Create: `web/src/pages/faqs/batch-actions.test.ts` — FAQ 动作交集。
- Create: `web/src/pages/kg/batch-actions.test.ts` — KG revision/门禁动作交集。
- Create: `web/src/pages/evaluation/batch-actions.test.ts` — eval status/run 动作交集。
- Create: `web/src/pages/batch-client-loop-contract.test.ts` — 静态禁止 `Promise.all(ids.map(mutate))`、`for ... mutateAsync` 和旧 `handleRunBatch`。

## 唯一 persistence contract

`batch_operation_jobs` 只保存父任务身份和不可变请求；父状态与计数由 item 实时聚合，避免 parent counter 成为第二份可漂移真相。

```sql
CREATE TABLE IF NOT EXISTS batch_operation_jobs (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (
        operation IN (
            'document_parse',
            'document_embedding',
            'document_kg_extraction',
            'faq_embedding',
            'faq_kg_extraction',
            'retrieval_eval_run',
            'document_file_cleanup'
        )
    ),
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    retry_of_job_id TEXT REFERENCES batch_operation_jobs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT batch_operation_jobs_request_object_check
        CHECK (jsonb_typeof(request_payload) = 'object'),
    UNIQUE (id, operation)
);

CREATE TABLE IF NOT EXISTS batch_operation_job_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'processing', 'succeeded', 'failed', 'conflict', 'skipped')
    ),
    input_fingerprint TEXT NOT NULL,
    input_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    child_job_id TEXT,
    error_code TEXT,
    error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, operation)
        REFERENCES batch_operation_jobs(id, operation) ON DELETE CASCADE,
    CONSTRAINT batch_operation_job_items_input_object_check
        CHECK (jsonb_typeof(input_payload) = 'object'),
    CONSTRAINT batch_operation_job_items_progress_object_check
        CHECK (jsonb_typeof(progress) = 'object'),
    CONSTRAINT batch_operation_job_items_result_object_check
        CHECK (jsonb_typeof(result) = 'object'),
    UNIQUE (job_id, resource_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS batch_operation_items_one_active_idx
    ON batch_operation_job_items(operation, resource_id)
    WHERE status IN ('queued', 'processing');

CREATE INDEX IF NOT EXISTS batch_operation_items_claim_idx
    ON batch_operation_job_items(status, next_attempt_at, created_at, id);

ALTER TABLE retrieval_eval_runs
    ADD COLUMN IF NOT EXISTS batch_item_id TEXT
        REFERENCES batch_operation_job_items(id);

CREATE UNIQUE INDEX IF NOT EXISTS retrieval_eval_runs_batch_item_idx
    ON retrieval_eval_runs(batch_item_id)
    WHERE batch_item_id IS NOT NULL;
```

`operation` 是数据库内闭集 discriminator，只用于持久 worker dispatch；HTTP 不提供 `POST /api/batch`、`resource_type` 或任意 `action` 字段。`resource_id` 不建跨五张业务表的多态外键，因为删除文档后 cleanup/history 必须保留；创建 job 时在同一事务锁定真实资源并验证。

父任务状态固定按 item 派生：

```text
全部 queued                                      -> queued
任一 queued/processing                          -> processing
全部 succeeded/skipped                          -> completed
有 succeeded/skipped 且有 failed/conflict       -> partial
终态且没有 succeeded/skipped                    -> failed
```

item 语义固定为：provider/网络错误是 `failed`；入队后资源正文、revision、状态或门禁变化是 `conflict`；同一指纹下目标已被其他 worker 完成是 `skipped`；retry 只从 `failed|conflict` 创建新父任务，旧任务与成功项不改写。

## 唯一 wire contract

### Shared response

```typescript
export type BatchOperation =
  | 'document_parse'
  | 'document_embedding'
  | 'document_kg_extraction'
  | 'faq_embedding'
  | 'faq_kg_extraction'
  | 'retrieval_eval_run'
  | 'document_file_cleanup'

export type BatchItemStatus =
  | 'queued'
  | 'processing'
  | 'succeeded'
  | 'failed'
  | 'conflict'
  | 'skipped'

export interface BatchOperationJobItem {
  id: string
  resource_id: string
  resource_label: string
  status: BatchItemStatus
  progress: Record<string, unknown>
  result: Record<string, unknown>
  child_job_id: string | null
  error_code: string | null
  error: string | null
  attempt_count: number
  updated_at: string
}

export interface BatchOperationJob {
  id: string
  operation: BatchOperation
  status: 'queued' | 'processing' | 'completed' | 'partial' | 'failed'
  request_payload: Record<string, unknown>
  total_count: number
  queued_count: number
  processing_count: number
  succeeded_count: number
  failed_count: number
  conflict_count: number
  skipped_count: number
  retry_of_job_id: string | null
  items: BatchOperationJobItem[]
  created_at: string
  updated_at: string
}
```

### 同步原子 endpoints

| Resource / semantic | Endpoint | Exact body | Success | Transaction gate |
| --- | --- | --- | --- | --- |
| 文档启停 | `POST /api/import/files/batch-disabled` | `{"ids":["imp_1"],"is_disabled":true}` | `{"count":1,"items":[ImportFile]}` | 全部 ID 存在；排序锁文件；一次 KG reconcile；同值为显式幂等返回 |
| 文档删除 | `POST /api/import/files/batch-delete` | `{"ids":["imp_1"]}` | `{"count":1,"deleted_ids":["imp_1"],"cleanup_job":BatchOperationJob}` | 全部存在且无 active operation；一次清理 DB/KG；同事务写 cleanup parent/items |
| FAQ 状态 | `POST /api/faqs/batch-status` | `{"ids":["faq_1"],"status":"usable"}` | `{"count":1,"items":[Faq]}` | status 仅 `usable|needs_review|disabled`；全部存在；usable 门禁变化一次 reconcile |
| KG entity confirm | `POST /api/kg/entities/batch-confirm` | `{"items":[{"id":"kg_ent_1","review_revision":4}]}` | `{"count":1,"items":[KgEntity]}` | 全部 revision/live evidence/confirm context 有效 |
| KG entity status | `POST /api/kg/entities/batch-status` | `{"items":[{"id":"kg_ent_1","review_revision":4}],"status":"disabled"}` | 同上 | status 仅 `needs_review|disabled`；全部 revision 有效；needs_review 要 live evidence |
| KG relation confirm | `POST /api/kg/relations/batch-confirm` | `{"items":[{"id":"kg_rel_1","review_revision":7}]}` | `{"count":1,"items":[KgRelation]}` | 全部 revision/live evidence/两端 usable |
| KG relation status | `POST /api/kg/relations/batch-status` | `{"items":[{"id":"kg_rel_1","review_revision":7}],"status":"disabled"}` | 同上 | status 仅 `needs_review|disabled`；全部 revision 有效；needs_review 要 live evidence |
| eval case 状态 | `POST /api/retrieval/eval-cases/batch-status` | `{"ids":["eval_1"],"status":"disabled"}` | `{"count":1,"items":[RetrievalEvalCase]}` | status 仅 `active|disabled`；全部 ID 存在 |

任一请求字段多余、ID 重复、数量越界或状态非法返回 400；任一 ID 缺失返回 404；任一 KG revision、资源状态、active job 或领域门禁在锁内变化返回 409。三类失败都必须回滚全部选中项，不返回 207，不返回逐项“成功部分”。

### 持久异步 endpoints

| Operation | Create endpoint and exact body | Retry endpoint and exact body |
| --- | --- | --- |
| 文档解析 | `POST /api/import/files/parse-batch-jobs` `{"ids":["imp_1"]}` | `POST /api/import/files/parse-batch-jobs/{job_id}/retry` `{"item_ids":["batch_item_1"]}` |
| 文档 Embedding | `POST /api/import/files/embedding-batch-jobs` `{"ids":["imp_1"]}` | `POST /api/import/files/embedding-batch-jobs/{job_id}/retry`，body 同上 |
| 文档级 KG | `POST /api/import/files/kg-extraction-batch-jobs` `{"ids":["imp_1"]}` | `POST /api/import/files/kg-extraction-batch-jobs/{job_id}/retry`，body 同上 |
| FAQ Embedding | `POST /api/faqs/embedding-batch-jobs` `{"ids":["faq_1"]}` | `POST /api/faqs/embedding-batch-jobs/{job_id}/retry`，body 同上 |
| FAQ KG | `POST /api/faqs/kg-extraction-batch-jobs` `{"ids":["faq_1"]}` | `POST /api/faqs/kg-extraction-batch-jobs/{job_id}/retry`，body 同上 |
| eval baseline | `POST /api/retrieval/eval-cases/run-batch-jobs` `{"ids":["eval_1"]}` | `POST /api/retrieval/eval-cases/run-batch-jobs/{job_id}/retry`，body 同上 |
| eval KG debug | 同一 create endpoint，body 只能为 `{"ids":["eval_1"],"use_kg":true}` | retry 继承原父任务 request，不接受新 strategy 字段 |
| 文件清理 | 只由文档删除事务内部创建，无公开 create route | `POST /api/import/files/cleanup-batch-jobs/{job_id}/retry`，body 同上 |

读取 routes 固定为：

```text
GET /api/batch-operation-jobs/{job_id}
GET /api/import/files/batch-jobs?limit=10
GET /api/faqs/batch-jobs?limit=10
GET /api/retrieval/eval-cases/batch-jobs?limit=10
```

资源近期任务 endpoints 的 operation 集合由服务端固定，客户端不能传 operation/action 查询扩大范围。创建请求先在一个事务中锁定并验证全部资源，再原子创建 parent 和全部 items；执行允许 partial success。retry 请求中的每个 item 必须属于同一个原 job、operation 匹配且处于 `failed|conflict`，否则整个 retry 创建回滚。

下列公开写路由在本单元删除，不保留 handler wrapper、前端 fallback 或 route alias；所有原单项按钮使用相应 batch create endpoint 并提交单元素 `ids`：

```text
POST /api/import/files/{file_id}/parse-jobs
POST /api/import/files/{file_id}/embed
POST /api/faqs/{faq_id}/embed
POST /api/faqs/embed-pending
POST /api/retrieval/eval-cases/{case_id}/run
POST /api/kg/extraction-jobs
GET /api/kg/extraction-jobs/{job_id}
GET /api/import/parse-jobs/{job_id}
```

`POST /api/import/chunks/{chunk_id}/embed` 是切片级维护命令，不是整文件批量兼容入口，继续保留。KG 实体/关系抽屉的单项审核路由也是明确的单资源命令，继续保留；列表 batch bar 即使只选一项也必须调用 batch endpoint。

## 页面动作矩阵

共同合法动作以“全部选中项都满足条件”为准；不静默跳过不适用项。`active_batch_operations` 由列表 API 对当前页一次性聚合返回，页面不靠本地 mutation 猜测其他浏览器的任务状态。

| Surface | Action | 所有选中项必须满足 | Mode / confirmation |
| --- | --- | --- | --- |
| Documents | 启用 | `is_disabled=true` | 同步原子，无危险确认 |
| Documents | 禁用 | `is_disabled=false` 且无 active parse/KG | 同步原子；确认“禁用 N 个文档，检索与 KG 来源立即失效” |
| Documents | 删除 | 无 active operation/child job | 同步 DB + cleanup job；确认数量、不可逆和异步文件清理 |
| Documents | 解析 | 未禁用、parser 为 canonical 支持值、无 active parse/embed/KG | 持久 job；逐文件使用其已保存 `chunker_type` |
| Documents | Embedding | 未禁用、`status in {needs_review,completed}`、pending/stale/failed 数量大于 0、无 active parse/embed | 持久 job |
| Documents | 提取 KG | 未禁用、已解析且 `chunk_count>0`、无 active parse/KG | 持久 document-level job |
| FAQs | 设为可用/待复核 | 每项 `status` 不等于目标；KG job 不在 processing | 同步原子 |
| FAQs | 禁用 | 每项非 disabled；KG job 不在 processing | 同步原子；确认“禁用 N 个 FAQ，不再参与检索” |
| FAQs | Embedding | 非 disabled、embedding 非 ready、无 active FAQ embedding | 持久 job |
| FAQs | 提取 KG | `status=usable`、无 active FAQ KG | 持久 job |
| KG entities | Confirm | `needs_review`、`has_valid_evidence=true`、`source_count>0` | 同步原子；每项带页面看到的 revision |
| KG entities | 退回待审核 | 非 needs_review 且有 live evidence | 同步原子；每项带 revision |
| KG entities | 停用 | 非 disabled | 同步原子；数量确认；每项带 revision |
| KG relations | Confirm | `needs_review`、live evidence、head/tail 均 usable | 同步原子；每项带 revision |
| KG relations | 退回待审核 | 非 needs_review 且有 live evidence | 同步原子；每项带 revision |
| KG relations | 停用 | 非 disabled | 同步原子；数量确认；每项带 revision |
| Eval cases | 启用 | 全部 disabled | 同步原子 |
| Eval cases | 禁用 | 全部 active | 同步原子；数量确认 |
| Eval cases | 运行 | 全部 active、无 active eval run | 持久 job；创建时固定当前 baseline/KG debug |

选择 scope 固定为当前真正渲染的资源 ID：KG/eval 的本地文本过滤后结果，不是服务端原始数组；表头 checkbox 只 toggle 这些 visible IDs。query、status、embedding/type filter、page、page size、KG tab 或 eval strategy 变化都清空选择；同一 scope 的轮询刷新保留仍存在且 revision 未变的 ID，消失或 KG revision 变化的项移出并提示“列表已更新，请重新选择”。

## Task 1: 用失败测试锁定 schema、types 和无兼容边界

- [ ] **Step 1: 在 `tests/test_db.py` 写 schema RED**

增加文本断言，完整锁定两表、闭集 operation、JSON object、partial active index 和 eval idempotency key：

```python
def test_batch_operation_schema_has_parent_items_and_no_polymorphic_resource_fk():
    """批次用 parent/items 保存历史，删除资源后 item 仍可读。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS batch_operation_jobs" in schema
    assert "CREATE TABLE IF NOT EXISTS batch_operation_job_items" in schema
    assert "batch_operation_items_one_active_idx" in schema
    assert "jsonb_typeof(request_payload) = 'object'" in schema
    assert "jsonb_typeof(input_payload) = 'object'" in schema
    assert "retrieval_eval_runs_batch_item_idx" in schema
    assert "resource_id TEXT NOT NULL" in schema
    assert "resource_id TEXT REFERENCES" not in schema
```

- [ ] **Step 2: 在 `tests/test_admin_server.py` / `tests/test_asgi_app.py` 写 strict wire RED**

测试 `ids=[]`、重复 ID、101 项、非字符串、额外字段均 400；KG item 缺 `review_revision`、布尔 revision、重复 ID 均 400。route 表必须包含本计划列出的显式 routes，并断言六个旧长任务 POST routes 与两个 child-job GET routes 都不存在。

- [ ] **Step 3: 在 `web/src/api/schemas-contract.test.ts` 写 DTO RED**

断言 `BatchOperationJobItem.error_code/attempt_count/progress`、父任务七个计数和 `active_batch_operations` 是必填字段；断言旧 hooks/wire 名称不存在：

```typescript
for (const oldName of [
  'useEmbedPendingFaqs',
  'useRunRetrievalEvalCase',
  'useCreateKgExtractionJob',
  'useStartImportParseJob',
  'useEmbedImportFile',
  'useEmbedFaq',
]) {
  assert.doesNotMatch(hooksSource, new RegExp(`export function ${oldName}\\b`))
}
```

- [ ] **Step 4: 运行 RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py -k "batch_operation or batch_payload or route" -q
cd web
npm test -- src/api/schemas-contract.test.ts
```

Expected: 因 batch tables/types/routes 尚不存在且旧长任务 routes 仍存在而失败；不接受 import error 或测试语法错误作为 RED。

- [ ] **Step 5: 实现 schema、闭集常量和严格 parser**

`cyclops/batch_operations.py` 提供且只提供当前类型：

```python
MAX_BATCH_ITEMS = 100
BATCH_ITEM_ACTIVE_STATUSES = frozenset({"queued", "processing"})
BATCH_ITEM_RETRYABLE_STATUSES = frozenset({"failed", "conflict"})

def parse_batch_ids(payload: Mapping[str, Any], *, allowed_fields: set[str]) -> list[str]:
    """解析非空无重复稳定 ID；关键约束是拒绝多余字段和隐式字符串转换。"""

def parse_kg_review_items(payload: Mapping[str, Any]) -> list[dict[str, int | str]]:
    """读取 id/review_revision 快照；关键约束是每项恰好两个字段。"""
```

`parse_batch_ids` 不使用 `str(value)` 宽松转换；重复项报 400，不自动去重。加入本计划 persistence DDL；所有新增/修改 Python 方法写中文 docstring。

- [ ] **Step 6: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_admin_server.py -k "batch_operation or batch_payload" -q
.venv/bin/python -m ruff check cyclops/batch_operations.py cyclops/db tests/test_db.py tests/test_admin_server.py
```

Expected: schema/parser tests PASS。

## Task 2: 实现 parent/items、lease fence、读取和 retry 真相

- [ ] **Step 1: 在 `tests/test_batch_operations.py` 写 DB RED**

覆盖以下行为：

```python
def test_batch_parent_status_is_derived_from_item_facts():
    """parent 不维护可漂移计数，partial 必须来自逐项终态。"""

def test_claim_batch_item_uses_skip_locked_lease_and_closed_operation():
    """多 worker 只能领取一个 item，过期 lease 可恢复。"""

def test_batch_item_transition_requires_current_lease_token():
    """旧 worker 的 fenced write 不得覆盖新 lease 的结果。"""

def test_batch_item_heartbeat_extends_only_current_lease():
    """阻塞 provider 调用期间只有当前 worker 可续租。"""

def test_retry_creates_new_parent_with_only_failed_and_conflict_items():
    """retry 不改旧 job，也不重复 succeeded/skipped item。"""
```

SQL 文本明确断言 `FOR UPDATE SKIP LOCKED`、`lease_expires_at < now()`、`WHERE lease_token = %(lease_token)s` 和稳定 `ORDER BY created_at, id`。

- [ ] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_batch_operations.py -k "parent_status or claim or lease or retry" -q
```

Expected: `BatchJobMixin` 尚不存在。

- [ ] **Step 3: 实现 `cyclops/db/batch_jobs.py`**

当前 public DB methods 固定为：

```python
def get_batch_operation_job(self, job_id: str) -> dict[str, Any] | None:
    """聚合父任务与全部 item，关键约束是状态和计数只由 item 派生。"""

def list_batch_operation_jobs(
    self,
    *,
    operations: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    """按服务端固定 operation 集合列近期任务，不接受客户端 action。"""

def claim_batch_operation_item(self, *, lease_seconds: int) -> dict[str, Any] | None:
    """原子领取 queued/到期 processing item，并生成新的 fencing token。"""

def update_batch_operation_item(
    self,
    item_id: str,
    *,
    lease_token: str,
    status: str,
    progress: dict[str, Any],
    result: dict[str, Any],
    error_code: str | None,
    error: str | None,
    next_attempt_at: datetime | None,
) -> dict[str, Any]:
    """只允许当前 lease 写 item；终态必须清除 lease。"""

def renew_batch_operation_item_lease(
    self,
    item_id: str,
    *,
    lease_token: str,
    lease_seconds: int,
) -> bool:
    """为当前 token 续租；token 已失效时返回 false，调用方不得再写结果。"""
```

claim 条件同时覆盖 `lease_token IS NULL` 和 lease 已过期的 queued/processing item；等待 child job 的 processing item 写 `next_attempt_at` 后必须主动清空 lease。每个 action-specific create/retry 方法调用 private `_insert_batch_job_in_conn(...)`；不得新增接受任意 `operation` 的 AdminApp 或 HTTP 写入口。所有 error 截断到 1000 字符，result/progress 必须是 object；父响应的 `updated_at` 固定取 `GREATEST(parent.updated_at, max(item.updated_at))`。

- [ ] **Step 4: 实现 retry 的全量验证**

retry 固定顺序：锁原 parent → 排序锁全部请求 item → 验证 operation/status/归属 → 排序锁当前业务资源 → 重跑 action-specific 门禁并生成新 fingerprint → 创建新 parent/items → commit。任一 item 非 retryable、资源已删除或 active unique 冲突都整体回滚。

- [ ] **Step 5: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_batch_operations.py -q
.venv/bin/python -m ruff check cyclops/db/batch_jobs.py cyclops/batch_operations.py tests/test_batch_operations.py
```

Expected: parent status、claim、fence、retry 全部 PASS。

## Task 3: 文档批量启停、原子删除和异步文件清理

- [ ] **Step 1: 写文档事务 RED**

在 `tests/test_db.py` 增加 fake-connection 顺序测试，在 `tests/test_batch_operations_postgres.py` 增加真实事务测试：

```python
def test_batch_delete_documents_rolls_back_when_any_id_is_missing(pg_db):
    """缺一个文档时，其他文档、向量、KG evidence 和 cleanup job 都不能变化。"""

def test_batch_delete_documents_commits_business_delete_and_cleanup_job_together(pg_db):
    """DB 删除成功必须同时存在一个 cleanup parent 和每文件一个 item。"""

def test_batch_set_documents_disabled_reconciles_all_sources_once(pg_db):
    """多文档启停以一次全集 KG reconcile 提交，不循环单项事务。"""
```

记录连接调用顺序必须是：排序锁全部文件 → exact count → active-job gate → KG 文件/切片全集锁 → 一次 source reconcile → knowledge delete/update → import file delete/update → cleanup parent/items → commit。

- [ ] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_batch_operations_postgres.py -k "batch_delete_documents or batch_set_documents" -q
```

- [ ] **Step 3: 实现文档 DB methods**

```python
def batch_set_import_files_disabled(
    self,
    ids: list[str],
    *,
    is_disabled: bool,
) -> list[dict[str, Any]]:
    """在一个事务启停全部文档，并对真实门禁变化做一次 KG 全集重算。"""

def batch_delete_import_files(self, ids: list[str]) -> dict[str, Any]:
    """原子删除业务数据并写 cleanup job；关键约束是请求线程不 unlink。"""
```

不得在事务内循环调用 `delete_import_file()` 或 `set_import_file_disabled()`。`batch_delete_import_files` 把每个文档的原件绝对路径和 `upload_dir/mineru-assets/<safe-id>` 写入 cleanup item `input_payload.paths`；路径必须先经 `ensure_upload_path_within()` 验证。不存在的路径由 cleanup worker 当作幂等成功，不把 DB 记录恢复。

- [ ] **Step 4: 实现显式 Admin/API**

`AdminApp.batch_set_import_files_disabled()` 与 `AdminApp.batch_delete_import_files()` 严格校验 exact body。DELETE confirmation 成功响应明确写 `cleanup_job`；旧单文档 DELETE 仍是合法详情命令，但改用同一个 private in-connection delete primitive，并同样返回单 item cleanup job，不能继续请求内 `Path.unlink()`。

- [ ] **Step 5: 实现 cleanup worker handler**

`document_file_cleanup` item 每次 claim 处理一个文档的 paths；只允许删除 upload root 内文件/目录。每条路径不存在记入 `result.missing_count`；删除成功记 `deleted_count`；权限/IO 错误写 `failed` 和 `error_code="file_cleanup_failed"`。retry 新建 item 时复用原安全路径快照。

- [ ] **Step 6: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py tests/test_batch_operations_postgres.py -k "batch_delete_documents or batch_set_documents or cleanup" -q
```

Expected: 缺失/active gate 回滚，成功删除同时产生可重试 cleanup job，请求线程零 `unlink()`。

## Task 4: FAQ 与 eval case 同步状态必须真正原子

- [ ] **Step 1: 写 FAQ/eval RED**

```python
def test_batch_faq_status_missing_id_rolls_back_faq_projection_and_kg(pg_db):
    """FAQ 缺失目标不能让已存在 FAQ 或其 projection/KG 门禁先变化。"""

def test_batch_eval_status_missing_id_rolls_back_all_cases(pg_db):
    """评测用例状态批改不能静默忽略缺失 ID。"""
```

另测 ID 顺序不影响返回稳定排序、同值目标是显式幂等且不改 `updated_at`、payload 多余字段 400。

- [ ] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_admin_server.py -k "batch_faq_status or batch_eval_status" -q
```

Expected: 当前 FAQ 方法会静默忽略缺失 ID，eval 无 batch 方法。

- [ ] **Step 3: 硬化 FAQ 当前方法并删除旧扫描路径**

用 `batch_set_faq_status(ids, status)` 替代 `update_faq_statuses`：排序 `FOR UPDATE`，先比对 exact ID set，再更新 changed rows、同步 FAQ projections，并对 usable gate 真正变化的全集只调用一次 `_reconcile_kg_source_change_in_conn`。删除 `AdminApp.embed_pending()`、`Database.list_embedding_candidates()` 和 `/api/faqs/embed-pending`，不留别名。

- [ ] **Step 4: 实现 eval case 状态批改**

`batch_set_retrieval_eval_case_status(ids, status)` 排序锁全部 case、验证 exact set 后一次 UPDATE；只接受 `active|disabled`。同值行不更新时间，但成功 response 返回全部请求项。

- [ ] **Step 5: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py -k "batch_faq_status or batch_eval_status or embed_pending" -q
```

Expected: 原子回滚和旧入口零存在均 PASS。

## Task 5: KG batch review 遵循全局 entity→relation 锁序

- [ ] **Step 1: 写 revision/门禁原子 RED**

在 `tests/test_db.py` 增加：

```python
def test_batch_confirm_entities_rolls_back_when_one_revision_is_stale():
    """任一 stale revision 不能确认其他实体或生成 projection。"""

def test_batch_confirm_relations_rolls_back_when_one_endpoint_is_not_usable():
    """任一关系端点门禁失败不能确认同批其他关系。"""

def test_batch_entity_status_locks_all_entities_before_incident_relations():
    """实体批改先锁有序 entity 全集，再锁有序 incident relation 全集。"""

def test_batch_relation_review_locks_all_endpoint_entities_before_relations():
    """关系审核先锁所有 endpoint entity，再锁选中 relation。"""
```

- [ ] **Step 2: 写真实并发 RED**

`tests/test_kg_postgres_concurrency.py` 同时运行：batch entity disable、batch relation confirm、单 relation confirm 和来源状态重算。设置有限 lock timeout，断言无线程 deadlock/timeout，并检查最终 revision/status/projection 与 live evidence 一致。

- [ ] **Step 3: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_db.py -k "batch_confirm_entities or batch_confirm_relations or batch_entity_status or batch_relation_review" -q
```

- [ ] **Step 4: 实现 entity batch primitives**

```python
def batch_confirm_kg_entities(
    self,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 revision 原子确认实体；全部 entity 锁定和门禁通过后才写投影。"""

def batch_set_kg_entity_status(
    self,
    items: list[dict[str, Any]],
    *,
    status: str,
) -> list[dict[str, Any]]:
    """原子退回/停用实体；先锁 entity 全集，再锁 incident relation 全集。"""
```

锁内顺序固定：normalize/sort IDs → 一条 `ORDER BY id FOR UPDATE` 锁全部 entities → exact ID/revision/context validation → fresh incident relation IDs → 排序锁 relations → 全部 updates/projections。不得事务内循环调用单项 public method。

- [ ] **Step 5: 实现 relation batch primitives**

```python
def batch_confirm_kg_relations(
    self,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 revision 原子确认关系；所有 endpoint entity 必须先于 relation 锁定。"""

def batch_set_kg_relation_status(
    self,
    items: list[dict[str, Any]],
    *,
    status: str,
) -> list[dict[str, Any]]:
    """按 revision 原子退回/停用关系，并同步全部 projection。"""
```

先无锁读取 locator 只用于收集候选 endpoint IDs；排序锁全部 endpoint entities 后，排序锁全部 relations，再复核 relation locator 未变化、revision、live evidence 和 endpoint status。所有 projection 写在同一事务。

- [ ] **Step 6: 实现显式 Admin/API 并运行 GREEN**

四个 batch endpoints 只调用对应四个 DB methods；`usable` 只允许 batch-confirm，不接受 batch-status 绕过。

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py -k "batch_kg or batch_confirm or batch_relation_review" -q
```

Expected: stale/missing/gate 任一失败全部回滚。

- [ ] **Step 7: 运行真实锁序 GREEN**

```bash
set -a
source .env
set +a
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-$DATABASE_URL}"
.venv/bin/python -m pytest tests/test_batch_operations_postgres.py tests/test_kg_postgres_concurrency.py -k "batch and (kg or lock or revision)" -q
```

Expected: PASS，无 deadlock/lock timeout。

## Task 6: 原子创建六类长任务和 action-specific retry

- [ ] **Step 1: 写 job creation RED**

每类 operation 测试创建事务内锁全部资源、缺失/门禁失败时 parent/items 数量仍为 0、成功时 item 顺序按 resource ID 稳定且指纹非空：

```text
document_parse: persisted chunker + original file fingerprint
document_embedding: selected non-ready chunk IDs + per-chunk source fingerprint
document_kg_extraction: whole-document KG source fingerprint
faq_embedding: FAQ content_hash
faq_kg_extraction: FAQ KG source fingerprint
retrieval_eval_run: question + expected IDs + status fingerprint
```

parse/KG parent 与 canonical child jobs 及 `child_job_id` 必须同事务创建；不能先提交 parent 再逐项 enqueue。

- [ ] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_batch_operations.py tests/test_db.py -k "create and batch_job" -q
```

- [ ] **Step 3: 实现六个 action-specific DB/Admin create methods**

AdminApp 固定方法名：

```text
create_document_parse_batch_job
create_document_embedding_batch_job
create_document_kg_extraction_batch_job
create_faq_embedding_batch_job
create_faq_kg_extraction_batch_job
create_retrieval_eval_run_batch_job
```

每个方法只接受本 wire 的 exact payload。baseline eval body 字段集合必须恰好为 `{ids}`；KG debug 必须恰好为 `{ids,use_kg}` 且 `use_kg is True`，不接受 `false` 或 strategy alias。DB methods 使用 private in-connection insert primitives 创建 child jobs，不增加同步执行 fallback。

- [ ] **Step 4: 实现每类显式 retry**

六个 retry Admin methods 固定 operation 并继承原 request payload；客户端不能在 retry 时更换 chunker/strategy。所有资源重做当前门禁和 fingerprint，产生新 job ID/item IDs。

- [ ] **Step 5: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_batch_operations.py tests/test_db.py tests/test_admin_server.py -k "batch_job and (create or retry)" -q
```

Expected: 六类 create/retry 的原子性和严格 wire PASS。

- [ ] **Step 6: 为当前页一次聚合 active operation**

`list_import_files()`、`list_faqs()` 和 `list_retrieval_eval_cases()` 取完当前页 IDs 后，各用一条 batch item 查询聚合 `active_batch_operations`，不做逐行查询。字段在 `ImportFile`、`Faq`、`RetrievalEvalCase` wire 中必填；测试记录 SQL 次数并断言 100 行仍只增加一次 active-operation 查询。

## Task 7: 实现可恢复 worker、逐步 Embedding、幂等 eval 和 child reconciliation

- [ ] **Step 1: 写 worker RED**

`tests/test_batch_operations.py` 用 fake DB/clock 覆盖：

```python
def test_batch_worker_continues_without_browser_polling():
    """没有 GET 请求也会领取并推进 item。"""

def test_batch_worker_recovers_expired_lease_after_restart():
    """进程退出后新 worker 使用新 token 继续，旧 token 写入失败。"""

def test_batch_worker_failure_does_not_stop_other_items():
    """单项 provider 异常写 failed，后续 item 继续。"""

def test_eval_batch_item_is_idempotent_after_crash():
    """同一 batch_item_id 最多保存一条 eval run。"""
```

- [ ] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_batch_operations.py -k "batch_worker or expired_lease or idempotent" -q
```

- [ ] **Step 3: 实现 `BatchOperationWorker` 生命周期**

```python
class BatchOperationWorker:
    """在服务生命周期推进持久批次；一个 item 异常不能终止 worker。"""

    async def run(self) -> None:
        """循环领取到期 item；同步 provider/文件操作统一放入 asyncio.to_thread。"""

    def run_available_once(self) -> bool:
        """领取并推进一个有界步骤；返回本轮是否处理过 item。"""
```

内部 dispatch 只匹配 schema 的七个 operation 常量，未知值落 `failed/error_code="unsupported_persisted_operation"` 并记录服务错误；不从 HTTP payload 构造 operation。`BATCH_WORKER_POLL_INTERVAL_SECONDS` 默认 `0.5`，只接受 `(0, 60]`；`BATCH_WORKER_LEASE_SECONDS` 默认 `300`，只接受 `[30, 3600]`。

- [ ] **Step 4: 实现 operation steps**

- `document_parse` / `document_kg_extraction` / `faq_kg_extraction`：读取已原子关联的 child job；活跃态释放 lease 并设置 `next_attempt_at`，completed 映射 succeeded，failed 映射 failed。batch worker 不调用 MinerU/LLM 第二次。
- `document_embedding`：一个 claim 最多处理一个快照 chunk；provider 调用前比对 fingerprint，写向量后保存 `progress.next_chunk_index/ready_count/skipped_count` 并释放 lease。全部 chunk 完成才 succeeded。
- `faq_embedding`：按 `content_hash` prepare/embed/fenced update；崩溃后若同 fingerprint 已 ready 则 skipped/succeeded，不重复调用 provider。
- `retrieval_eval_run`：按 case fingerprint 运行；`retrieval_eval_runs.batch_item_id` 唯一键保证 crash retry 不重复保存。运行后 fingerprint 变化写 conflict，不把旧问题结果挂到新 case。
- `document_file_cleanup`：执行 Task 3 的安全路径删除。

任何可能阻塞超过 `lease_seconds / 3` 的 provider/文件步骤都在 `asyncio.to_thread` 执行，同时 event loop 每 `lease_seconds / 3` 调用 `renew_batch_operation_item_lease()`；续租失败后等待线程结束但丢弃结果，禁止用旧 token 落库。

- [ ] **Step 5: 实现 lifespan start/stop**

`create_app()` 可注入 `batch_worker_factory` 供测试；生产在现有 parse/KG workers 之后启动 batch worker。shutdown 顺序为 stop batch worker → stop child workers → close DB pool，保证 batch reconciler 不在 child worker 停止后继续创建工作。

- [ ] **Step 6: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_batch_operations.py tests/test_asgi_app.py -k "batch_worker or lifespan or lease or idempotent" -q
.venv/bin/python -m ruff check cyclops/batch_worker.py cyclops/db/batch_jobs.py cyclops/asgi_app.py
```

Expected: 无浏览器轮询仍完成、重启恢复、单项失败隔离、shutdown 无 pending task warning。

## Task 8: 注册唯一 routes 并删除同步长任务入口

- [ ] **Step 1: 写完整 route table RED**

`tests/test_asgi_app.py` 从 `app.routes` 比对本计划所有 sync/create/retry/read paths；对六个旧 POST paths 做精确零存在断言。Fake Admin 每个 create 只记录一次调用，确保一个 HTTP 请求传入完整 ids 数组。

- [ ] **Step 2: 写 HTTP error RED**

使用 TestClient 覆盖：400 payload、404 任一 missing、409 revision/active gate、200 全成、worker job 202 queued（若当前框架保持默认 200，则显式设置 `status_code=202`）。同步 batch 禁止 207。

- [ ] **Step 3: 实现 routes 和 status code**

所有持久 create/retry 成功返回 HTTP 202 + 完整 `BatchOperationJob`；GET 返回 200；同步原子返回 200。删除旧 ASGI handlers 和对应 Admin methods，不保留调用新方法的 wrapper。

- [ ] **Step 4: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_asgi_app.py tests/test_admin_server.py -k "batch or route or long_task" -q
```

Expected: 当前 route 表和错误语义 PASS。

## Task 9: 实现共享 stable-ID selection 与紧凑 batch UI

- [ ] **Step 1: 写 selection reducer RED**

`web/src/components/batch/page-selection.test.ts` 固定：

```typescript
assert.equal(pageSelectionState(new Set(), ['a', 'b']), 'none')
assert.equal(pageSelectionState(new Set(['a']), ['a', 'b']), 'some')
assert.equal(pageSelectionState(new Set(['a', 'b']), ['a', 'b']), 'all')
assert.deepEqual([...toggleVisiblePage(new Set(['old']), ['a', 'b'])], ['a', 'b'])
assert.deepEqual([...reconcileVisibleSelection(new Set(['a', 'gone']), ['a', 'b'])], ['a'])
```

另测排序不改变选择、scope key 改变返回空集合、KG 相同 ID revision 变化会移除。

- [ ] **Step 2: 运行 RED**

```bash
cd web
npm test -- src/components/batch/page-selection.test.ts
```

- [ ] **Step 3: 实现 pure reducer 和 checkbox**

`toggleVisiblePage()` 选择时返回 visible IDs 的新集合、全选状态再次点击时返回空集合，不保留当前页之外的旧 ID。`selection-checkbox.tsx` 用 ref 设置 `element.indeterminate = state === 'some'`，表头 aria-label 为“选择当前页全部 N 项”，行 aria-label 为“选择 <resource label>”。键盘 Space toggle；checkbox click 不打开详情。

- [ ] **Step 4: 实现 batch bar / danger dialog / job status**

batch bar 固定显示：`已选择 N 项`、共同动作按钮、`取消选择`；无共同动作显示“所选项目没有共同可执行操作”并保留取消。危险 dialog 的标题、描述、确认按钮都含数量；文档删除描述必须明确“业务数据立即删除，本地原件异步清理”。job status 按 persisted counts 展示并只对 failed/conflict items提供 retry。

- [ ] **Step 5: 运行 GREEN**

```bash
cd web
npm test -- src/components/batch/page-selection.test.ts src/components/batch/batch-ui-contract.test.ts
npm run typecheck
```

Expected: stable ID、三态、ARIA 和数量确认 PASS。

## Task 10: 接入文档与 FAQ 列表及当前长任务 hooks

- [ ] **Step 1: 写文档/FAQ动作矩阵 RED**

分别构造混合 disabled、ready/non-ready、usable/needs_review、active operation 行，断言只返回本计划矩阵的交集；选中一项后不允许 resolver 查看未选行。

- [ ] **Step 2: 写 hooks 单请求 RED**

`batch-hooks-contract.test.ts` 对每个 hook source slice 断言恰好一个 `requestJson`，URL 为本计划显式 endpoint，body 是完整 `ids/items`；断言无 `.map(mutateAsync)`、无旧 hook export。

- [ ] **Step 3: 运行 RED**

```bash
cd web
npm test -- src/pages/documents/batch-actions.test.ts src/pages/faqs/batch-actions.test.ts src/api/batch-hooks-contract.test.ts
```

- [ ] **Step 4: 接入 DocumentsPage**

把 document row 从整行 `<button>` 改为非交互容器 + 独立 checkbox + 详情 button，避免 nested interactive。列表 header 最左加入三态 checkbox；原 toolbar 在有选择时切换到 batch bar。scope key 包含 query/status/page；当前 `items` 就是 visible current page。删除后清空选择，显示 cleanup job；parse/embed/KG job 完成后分别刷新 import files/chunks/KG queries。

- [ ] **Step 5: 接入 FaqsPage**

FAQ card 同样拆分 checkbox 与详情 button；列表顶部增加“选择当前页”三态控件。scope key 包含 query/status/embedding/page。删除全局 `Embedding`/`useEmbedPendingFaqs`，Embedding 与 KG 只作用于选中 IDs；drawer 单项按钮提交单元素 batch job。job completed 后刷新 FAQs，KG extraction 还调用 `invalidateKgReviewQueries()`。

- [ ] **Step 6: 运行 GREEN**

```bash
cd web
npm test -- src/pages/documents/batch-actions.test.ts src/pages/faqs/batch-actions.test.ts src/api/batch-hooks-contract.test.ts
npm run typecheck
```

Expected: 两页共同动作、单请求和 cache invalidation PASS。

## Task 11: 接入 KG entity/relation revision-aware batch review

- [ ] **Step 1: 写 KG resolver RED**

测试 confirm 只有 needs_review + live evidence 才出现；relation 任一 endpoint 非 usable 时 confirm 消失；构造 wire items 必须保留选择时 revision：

```typescript
assert.deepEqual(toKgReviewItems(selected), [
  { id: 'kg_ent_1', review_revision: 4 },
  { id: 'kg_ent_2', review_revision: 9 },
])
```

- [ ] **Step 2: 运行 RED**

```bash
cd web
npm test -- src/pages/kg/batch-actions.test.ts
```

- [ ] **Step 3: 接入 KnowledgeGraphPage**

entity/relation row 拆成 checkbox + detail button；tab 切换、query/status/type/page 改变都清空 selection。selection value 保存 `{id, review_revision}` snapshot，不只保存 ID；轮询返回同 ID 新 revision 时移出并 toast。entity 和 relation 使用各自 resolver/endpoints，mutation 成功统一 `invalidateKgReviewQueries()` 并清空当前 tab selection。

- [ ] **Step 4: 运行 GREEN**

```bash
cd web
npm test -- src/pages/kg/batch-actions.test.ts src/pages/kg/kg-cache-contract.test.ts src/pages/kg/helpers.test.ts
npm run typecheck
```

Expected: revision wire、门禁交集、tab/scope 清空和 KG cache PASS。

## Task 12: 接入 eval case 并删除浏览器伪批量

- [ ] **Step 1: 写 eval resolver 与零循环 RED**

`batch-actions.test.ts` 断言 active selection 才有 run/disable，disabled selection 才有 enable，混合 selection 没有这些动作；strategy 改变清空 selection。`batch-client-loop-contract.test.ts` 扫描非 test 源：

```typescript
assert.doesNotMatch(evaluationPageSource, /handleRunBatch/)
assert.doesNotMatch(evaluationPageSource, /for\s*\([^)]*of[^)]*\)[\s\S]{0,600}mutateAsync/)
assert.doesNotMatch(allBatchPageSource, /Promise\.all\([\s\S]{0,600}\.map\([\s\S]{0,300}mutate/)
```

- [ ] **Step 2: 运行 RED**

```bash
cd web
npm test -- src/pages/evaluation/batch-actions.test.ts src/pages/batch-client-loop-contract.test.ts
```

Expected: 当前 `handleRunBatch` 与 `for ... runCase.mutateAsync` 触发失败。

- [ ] **Step 3: 接入 EvaluationPage**

左侧 case list 增加 checkbox；有选择时左栏 filter block 切换 batch bar，当前详情选择与批量 selection 分离。单条“运行”也创建一个 ID 的 persisted run batch job；移除 `runActivity.mode='batch'`、`batchRunSnapshot`、`createEvaluationRunMutex` 对 batch 的占用和逐项 toast。`EvaluationBatchPanel` 继续展示当前结果诊断，并可展示 persisted recent run job，但不再拥有“运行当前筛选全部启用用例”按钮。

- [ ] **Step 4: 删除浏览器 batch state 文件并接 job polling**

删除 `batch-state.ts/test.ts`；`useRecentRetrievalEvalBatchJobs()` 在刷新/重开页面后恢复 active job，terminal 后一次刷新 `retrieval-eval-cases`。partial/failed 展示逐项问题和 retry，不用本地 override 伪造未完成 item。

- [ ] **Step 5: 运行 GREEN**

```bash
cd web
npm test -- src/pages/evaluation src/pages/batch-client-loop-contract.test.ts src/api/batch-hooks-contract.test.ts
npm run typecheck
```

Expected: 源码零客户端逐项 loop，持久 job 驱动进度和结果。

## Task 13: 全量验证、真实 PostgreSQL 与浏览器验收

- [ ] **Step 1: 运行 targeted backend suite**

```bash
.venv/bin/python -m pytest tests/test_batch_operations.py tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py tests/test_config.py -q
.venv/bin/python -m ruff check cyclops tests/test_batch_operations.py tests/test_batch_operations_postgres.py
```

Expected: PASS。

- [ ] **Step 2: 运行真实 PostgreSQL suite**

```bash
set -a
source .env
set +a
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-$DATABASE_URL}"
.venv/bin/python -m pytest tests/test_batch_operations_postgres.py tests/test_kg_postgres_concurrency.py -q
```

Expected: 缺失/revision/gate 全回滚、claim 无重复、lease 可恢复、KG 无死锁、eval run 按 batch item 幂等、文档 delete 与 cleanup job 同 commit。

- [ ] **Step 3: 运行 frontend/static gates**

```bash
cd web
npm test
npm run lint
npm run build
```

从仓库根目录运行：

```bash
rg -n "handleRunBatch|useEmbedPendingFaqs|/api/faqs/embed-pending|/api/kg/extraction-jobs" web/src cyclops --glob '!**/*.test.*'
rg -n -U -P "Promise\.all\([\s\S]{0,600}\.map\([\s\S]{0,300}(mutateAsync|requestJson)|for\s*\([^)]*of[^)]*\)[\s\S]{0,600}(mutateAsync|requestJson)" web/src/pages web/src/api --glob '!**/*.test.*'
```

Expected: 两条都零命中；batch hooks/pages 没有客户端逐项网络循环。

- [ ] **Step 4: 在浏览器验证五页 selection contract**

使用测试数据逐页验证：未选/部分/全选三态；表头只选择当前渲染页；query/filter/page/KG tab/eval strategy 改变清空；同 scope 自动刷新保持 stable IDs；checkbox 不打开 drawer；batch bar 只显示共同动作；危险 dialog 的资源类型、数量和影响正确。DevTools Network 中每次批量操作只能出现一个 create/status/delete HTTP 请求。

- [ ] **Step 5: 验证关闭浏览器与 partial retry**

启动至少两个 item 的 parse/embed/KG/eval job，在第一个 item processing 时关闭标签页；等待 worker 后重开同一资源页，recent jobs 必须恢复服务端进度。人为让一个 item 产生 provider failure 或 fingerprint conflict，确认父状态为 partial/failed、成功项不重跑、retry 新 job 只含勾选的 failed/conflict item。

- [ ] **Step 6: 验证文档删除介质边界**

删除两个测试文档后立即确认业务列表、切片、knowledge rows 和 KG live evidence 已在同一 DB commit 消失；cleanup job queued/processing 可见。模拟一次文件权限失败，确认业务数据不复活、item failed 可 retry；恢复权限后 retry 完成且仅删除 upload root 内路径。

- [ ] **Step 7: 运行全量质量门**

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m cyclops check-config
git diff --check
```

Expected: PASS。若 `check-config` 因尚未安装已确认的 `pg_search` 外部依赖失败，只记录该已知环境阻塞，batch 行为与其余测试仍必须通过。

- [ ] **Step 8: 更新项目规格和改动记录**

把已验证契约写入 File map 列出的 `.trellis/spec/` 文档，并更新本 change 目录的 `update-plan.md` / `confirmation.md`：记录实际 routes、测试数量、真实 PG 结果、浏览器验收结果和未实施的第二阶段资源。不得自动创建 Git commit。

## 完成定义

- 五类资源都支持 stable-ID 当前页多选、三态 checkbox、scope 清空和紧凑 batch bar。
- 首版没有跨全部筛选结果选择，也没有给证据/诊断/导航等非业务资源加 checkbox。
- 所有短动作使用一个显式 batch request + 一个数据库事务；缺失、revision 或门禁失败时零部分写入。
- KG batch 的每项 wire 都带 `review_revision`，所有路径遵循 entity→relation 全局锁序。
- parse/embed/eval/KG extract 使用持久 parent/items；关闭浏览器和服务重启后继续，partial 可解释，retry 不重复成功项。
- 文档删除的 PostgreSQL 业务提交与 cleanup job 同事务；请求线程不直接删除文件，失败可重试且不恢复业务数据。
- 源码没有 selected IDs 的客户端 N 次 mutation、万能 action endpoint、旧 route alias、同步长任务 fallback 或自动 Git commit。
