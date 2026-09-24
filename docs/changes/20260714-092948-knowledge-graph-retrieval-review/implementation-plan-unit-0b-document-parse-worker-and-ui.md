# 持久文档解析、列表进度与 Drawer/Tooltip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development task-by-task. 本计划在当前连续工作区执行；不得保留 GET 驱动解析、同步重解析或网页 `BackgroundTasks` 执行路径，不自动提交 Git commit。

**Goal:** 让 MinerU/本地 Markdown 解析在离开页面、关闭浏览器和服务重启后仍由后端持续推进，并在文档名右侧与详情 Drawer 展示同一持久任务进度；同时修正 Drawer 初始焦点导致的 Tooltip 自动弹出。

**Architecture:** `import_parse_jobs` 是唯一解析任务真相，`import_files` 只保留文档业务状态。POST 在事务内创建 queued job，ASGI lifespan 启动的可恢复 worker 以 lease 原子领取，在事务外调用 MinerU，再将进度或最终切片原子落库。列表、Drawer 和任务 GET 只读数据库；共享 Drawer 把程序化初始焦点放在静态内容容器，Tooltip 使用 500ms 首次延迟和 400ms warm-up。

**Tech Stack:** Python 3.11、FastAPI lifespan、asyncio、psycopg 3、PostgreSQL 16、React 19.2、TanStack Query、Radix Dialog/Tooltip、pytest、Node test runner。

**2026-07-18 后端状态：** Task 1–3 与 Task 6 Step 1 已完成。worker 在长 provider 调用期间按 lease 三分之一间隔续租，claim 瞬时失败会等待后重试；终态统一 file→chunks→job 锁序并在锁前/锁后两次校验 fence，解析指纹覆盖全部 MinerU 输出配置。验证为后端目标集 `479 passed`、完整 Python `663 passed / 10 skipped`、隔离 PostgreSQL `3 passed`、Ruff 与配置检查通过。Task 4–5 和浏览器验收继续等待用户确认后的布局图。

---

## File map

- Modify: `sql/001_init.sql` — 新增唯一 `import_parse_jobs` 当前 schema，删除 `import_files` 上的 provider/job 运行字段。
- Modify: `cyclops/db/imports.py` — 入队、lease claim/reclaim、进度回写、失败和切片原子完成。
- Create: `cyclops/import_parse_worker.py` — 单一持久解析 worker 循环与可控启停生命周期。
- Modify: `cyclops/admin_server.py` — 创建/读取任务及执行单步 provider 逻辑；删除 GET 中的 MinerU 副作用和同步 reparse 入口。
- Modify: `cyclops/asgi_app.py` — lifespan 启停 worker，只暴露当前的 job 创建/只读路由。
- Modify: `cyclops/config.py` — 如需运维可调整，只增加持久 worker 轮询间隔和 lease 时长两个明确配置。
- Modify: `.env.example` — 记录持久 worker 轮询与 lease 的生产默认值。
- Modify: `tests/test_db.py`, `tests/test_admin_server.py`, `tests/test_asgi_app.py` — 持久任务、恢复、只读 GET 与 lifespan 行为。
- Create: `tests/test_import_parse_worker.py` — worker 连续推进、停机与异常隔离。
- Modify: `web/src/api/schemas.ts`, `web/src/api/hooks.ts` — `ImportParseJob` 当前 DTO 与只读轮询 hook。
- Modify: `web/src/pages/DocumentsPage.tsx`, `web/src/pages/documents/document-list.tsx`, `web/src/pages/documents/document-drawer.tsx` — 同一 job 的紧凑/完整进度。
- Modify: `web/src/components/ui/drawer.tsx`, `web/src/components/layout/app-shell.tsx` — 静态初始焦点和 Tooltip 500/400ms。
- Create: `web/src/components/ui/drawer-focus-contract.test.ts`, `web/src/pages/documents/parse-progress.test.ts` — 前端焦点/进度契约。
- Modify: `.trellis/spec/backend/cyclops-document-parser-contracts.md`, `.trellis/spec/frontend/components.md` — 完成后记录唯一当前契约。

## 唯一 wire contract

```python
ImportParseJob = {
    "id": str,
    "file_id": str,
    "status": "queued" | "submitting" | "polling" | "finalizing" | "completed" | "failed",
    "chunker_type": "naive" | "manual" | "qa" | "table",
    "input_fingerprint": str,
    "provider_batch_id": str | None,
    "provider_file_name": str | None,
    "progress": dict[str, Any],
    "percent": int,
    "error": str | None,
    "created_at": datetime,
    "updated_at": datetime,
}
```

```text
POST /api/import/files/{file_id}/parse-jobs
body: {"chunker_type":"naive"}   # 必填 canonical 值，不用缺失字段推断
response: queued ImportParseJob

GET /api/import/parse-jobs/{job_id}
response: current ImportParseJob          # 严格只读

GET /api/import/files/{file_id}
response: {"file": ImportFile, "parse_job": ImportParseJob | null}

GET /api/import/files
response.items[*].parse_job: ImportParseJob | null
```

`GET /api/import/files/{file_id}/parse-status` 和 `POST /api/import/files/{file_id}/reparse` 删除；不保留路由别名、前端 fallback 或 AdminApp wrapper。

### Task 1: 用 schema 失败测试锁定持久任务

- [x] **Step 1: 在 `tests/test_db.py` 和 schema 文本测试写 RED**

```python
def test_import_parse_job_schema_has_one_active_job_and_object_progress():
    """解析任务独立持久化，每个文件同时只能有一个活跃 generation。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS import_parse_jobs" in schema
    assert "jsonb_typeof(progress) = 'object'" in schema
    assert "import_parse_jobs_one_active_per_file_idx" in schema
    assert "parse_batch_id" not in import_file_table_block(schema)
    assert "parse_file_name" not in import_file_table_block(schema)
```

```python
def test_claim_import_parse_job_uses_skip_locked_and_lease():
    """多 worker 必须原子领取，崩溃后只允许过期 lease 重领。"""
    sql = normalize_sql(Database._claim_import_parse_job_sql())
    assert "for update skip locked" in sql
    assert "lease_expires_at" in sql
    assert "next_poll_at <= now()" in sql
```

- [x] **Step 2: 运行 RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "import_parse_job or parse_progress" -q
```

Expected: 因 `import_parse_jobs` 和 claim SQL 尚不存在失败；不接受 import/type error 作为 RED。

- [x] **Step 3: 实现唯一 schema**

`sql/001_init.sql` 创建以下表和 partial unique index：

```sql
CREATE TABLE IF NOT EXISTS import_parse_jobs (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES import_files(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'submitting', 'polling', 'finalizing', 'completed', 'failed')
    ),
    chunker_type TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    provider_batch_id TEXT,
    provider_file_name TEXT,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT import_parse_jobs_progress_object_check
        CHECK (jsonb_typeof(progress) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS import_parse_jobs_one_active_per_file_idx
    ON import_parse_jobs(file_id)
    WHERE status IN ('queued', 'submitting', 'polling', 'finalizing');
```

`import_files` 删除 `parse_batch_id` / `parse_file_name` / `parse_progress`；一次性 schema migration 明确 drop 这三列，这是 schema 切换而不是运行时兼容。

- [x] **Step 4: 实现 DB create/claim/read 并运行 GREEN**

```python
def create_import_parse_job(
    self,
    file_id: str,
    *,
    chunker_type: str,
    input_fingerprint: str,
) -> dict[str, Any]:
    """锁定文件并创建唯一 queued 任务，任何活跃任务都明确冲突。"""

def claim_import_parse_job(self, *, lease_seconds: int) -> dict[str, Any] | None:
    """原子领取到期任务，外部调用期间只持有 lease 而不持有行锁。"""
```

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "import_parse_job or parse_progress" -q
.venv/bin/python -m ruff check cyclops/db/imports.py tests/test_db.py
```

Expected: PASS。

### Task 2: 实现可恢复 worker 和原子终结

- [x] **Step 1: 先写 worker RED**

`tests/test_import_parse_worker.py` 使用 fake clock/admin，覆盖：

```python
def test_worker_continues_without_any_http_poll():
    """浏览器不请求 GET 时，worker 仍应连续领取并推进任务。"""
    admin = FakeAdmin(claimed=[queued_job(), polling_job(), None])
    worker = ImportParseWorker(admin, poll_interval_seconds=0.01, lease_seconds=30)
    worker.run_available_once()
    worker.run_available_once()
    assert admin.processed == ["parse_job_1", "parse_job_1"]


def test_expired_lease_is_reclaimed_after_restart():
    """新 worker 可重领旧进程过期 lease，未过期则不重复执行。"""
```

- [x] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_import_parse_worker.py -q
```

Expected: `ImportParseWorker` 尚不存在。

- [x] **Step 3: 实现 `cyclops/import_parse_worker.py`**

```python
class ImportParseWorker:
    """在 ASGI 生命周期内推进持久解析任务，关闭时可控结束。"""

    async def run(self) -> None:
        """循环领取到期任务；同步 provider 调用统一放到 `asyncio.to_thread`。"""
        while not self._stop_event.is_set():
            processed = await asyncio.to_thread(self.run_available_once)
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
```

`run_available_once()` 只调用 `claim_import_parse_job()` 和 `AdminApp.process_import_parse_job(job)`，不持有自己的第二套状态。单个 job 异常必须落为 failed 或释放 lease，不得结束整个 worker。

- [x] **Step 4: 锁定 provider 阶段 RED**

`tests/test_admin_server.py` 分别断言：

```text
queued/submitting -> start_file -> polling + provider locator
polling/running -> update progress + next_poll_at + release lease
polling/done -> finalizing -> download/build chunks -> atomic complete
polling/failed -> atomic failed
Markdown queued -> local parse -> atomic complete
input_fingerprint changed -> failed/conflict; old chunks unchanged
```

GET 测试用记录 fake MinerU 断言 `get_task_status` / `download_task_result` 均未被调用。

- [x] **Step 5: 实现事务边界**

`cyclops/db/imports.py` 提供当前方法：

```python
def update_import_parse_job_progress(
    self,
    job_id: str,
    *,
    lease_token: str,
    status: str,
    progress: dict[str, Any],
    provider_batch_id: str | None,
    provider_file_name: str | None,
    next_poll_at: datetime,
) -> dict[str, Any]:
    """仅持有当前 lease 的 worker 可更新阶段，写后释放 lease。"""

def complete_import_parse_job(
    self,
    job_id: str,
    *,
    lease_token: str,
    input_fingerprint: str,
    chunks: list[dict[str, Any]],
    progress: dict[str, Any],
) -> dict[str, Any]:
    """在同一事务替换整份切片、失效旧 KG/向量并将 job 置为 completed。"""
```

完成顺序固定为：

```text
read parse job -> fast verify lease/status/fingerprint
lock import file + old chunks
lock parse job -> re-verify lease/status/fingerprint
reconcile old KG source and delete old evidence
delete old knowledge rows/chunks
insert all replacement chunks
update import_files business summary
update parse job completed + clear lease
commit
```

任何失败整个事务回滚，不暴露半套切片。

`claim_import_parse_job()` 必须领取到期且未持有有效 lease 的
`queued/submitting/polling/finalizing` 任务；其中 `finalizing` 用于恢复“已经进入终结阶段、
但进程在下载或原子提交前崩溃”的任务。未过期 lease 仍不得被第二个 worker 重复领取。

- [x] **Step 6: 运行 GREEN**

```bash
.venv/bin/python -m pytest tests/test_import_parse_worker.py tests/test_admin_server.py tests/test_db.py -k "parse_job or mineru_parse or parse_progress" -q
.venv/bin/python -m ruff check cyclops/import_parse_worker.py cyclops/admin_server.py cyclops/db/imports.py
```

Expected: PASS。

### Task 3: 在 ASGI lifespan 中唯一启动 worker

- [x] **Step 1: 先写 lifespan RED**

`tests/test_asgi_app.py` 注入 fake worker factory，断言：

```python
def test_lifespan_starts_and_stops_import_parse_worker():
    """服务启动后任务不依赖请求，关闭时等待 worker 停止。"""
    with TestClient(create_app(admin_app=fake_admin, worker_factory=factory)):
        assert worker.started is True
    assert worker.stopped is True
```

路由表测试要求新路由存在，并要求旧 `parse-status` / `reparse` 路由不存在。

- [x] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_asgi_app.py -k "parse_worker or parse_job or route" -q
```

- [x] **Step 3: 实现 lifespan**

`create_app()` 允许测试注入 `worker_factory`，生产默认构造 `ImportParseWorker(app.state.admin_app, ...)`；创建 asyncio task 后 yield，finally 先 stop/await worker，再关闭 DB pool。不使用 FastAPI `BackgroundTasks` 启动解析。

- [x] **Step 4: 运行 GREEN 和启停回归**

```bash
.venv/bin/python -m pytest tests/test_asgi_app.py tests/test_import_parse_worker.py -q
```

Expected: worker 在无 HTTP poll 时仍执行，shutdown 不遗留 pending task warning。

### Task 4: 列表名称右侧展示紧凑进度

- [ ] **Step 1: 先写 DTO/helper RED**

`web/src/pages/documents/parse-progress.test.ts` 锁定：

```typescript
test('derives compact progress only from the persistent parse job', () => {
  assert.deepEqual(compactParseProgress({ status: 'polling', percent: 37 }), {
    visible: true,
    percent: 37,
    label: '37%',
  })
  assert.equal(compactParseProgress({ status: 'completed', percent: 100 }).visible, false)
})
```

`schemas-contract.test.ts` 要求 `ImportFile.parse_job` 和 `ImportParseJob.progress/percent/status` 的当前字段，并禁止 `ImportFile.parse_batch_id/parse_file_name/parse_progress`。

- [ ] **Step 2: 运行 RED**

```bash
cd web && npm test -- src/pages/documents/parse-progress.test.ts src/api/schemas-contract.test.ts
```

- [ ] **Step 3: 实现紧凑显示**

`document-list.tsx` 的名称行保持同一 grid 列，进度条紧贴文件名：

```tsx
<div className="flex min-w-0 items-center gap-2">
  <span className="min-w-0 truncate">{file.original_name}</span>
  {progress.visible && (
    <span className="flex w-[76px] shrink-0 items-center gap-1.5" aria-label={`解析进度 ${progress.label}`}>
      <span className="h-1 flex-1 overflow-hidden rounded-full bg-(--color-surface-3)">
        <span className="block h-full bg-(--color-primary)" style={{ width: `${progress.percent}%` }} />
      </span>
      <span className="w-7 text-right text-[10px] tabular-nums text-(--color-text-faint)">
        {progress.label}
      </span>
    </span>
  )}
</div>
```

不新增列，不将 Drawer 本地 mutation pending 当作解析进度。

- [ ] **Step 4: Drawer 读同一 job**

`document-drawer.tsx` 读 `GET /api/import/files/{file_id}` 的 `parse_job`，活跃时轮询 job GET；展示阶段、`extracted_pages/total_pages`、percent 和 error。终态停止轮询，completed 时 invalidate `import-files` / file detail / chunks，failed 只显示一次失败 toast。

- [ ] **Step 5: 运行 GREEN**

```bash
cd web && npm test -- src/pages/documents/parse-progress.test.ts src/api/schemas-contract.test.ts
cd web && npm run typecheck && npm run lint
```

Expected: PASS，列表名称右侧有紧凑进度，详情只读同一 job。

### Task 5: 修正 Drawer 初始焦点与 Tooltip warm-up

- [ ] **Step 1: 先写共享契约 RED**

`drawer-focus-contract.test.ts` 通过 Testing Library 打开包含 Copy ID Tooltip 的 Drawer，断言：

```text
打开后 document.activeElement 是 Drawer 静态容器/标题，不是 Copy ID button
未发生 pointer/focus 时 Tooltip 内容不在 DOM
Tab 到 Copy ID 时 Tooltip 立即显示
关闭后焦点恢复到原 trigger
```

`app-shell` 静态契约断言 `delayDuration={500}` 和 `skipDelayDuration={400}`。

- [ ] **Step 2: 运行 RED**

```bash
cd web && npm test -- src/components/ui/drawer-focus-contract.test.ts
```

Expected: 当前 Drawer 自动聚焦首个 Copy ID，且 provider 仍为 200ms，测试失败。

- [ ] **Step 3: 实现静态初始焦点**

`DrawerContent` 为真正的 content root 添加 `tabIndex={-1}`，组合而不覆盖消费方 `onOpenAutoFocus`：

```tsx
const handleOpenAutoFocus = (event: Event): void => {
  onOpenAutoFocus?.(event)
  if (event.defaultPrevented) return
  event.preventDefault()
  contentRef.current?.focus({ preventScroll: true })
}
```

如消费方需立即录入，只允许通过明确 `initialFocusRef` 定位，不扫描 DOM 第一个可聚焦控件。当前复杂 Drawer 全部使用默认静态容器。

- [ ] **Step 4: 实现 Tooltip 时序**

```tsx
<TooltipProvider delayDuration={500} skipDelayDuration={400}>
```

不修改 Radix 的 keyboard focus 即时显示，不增加“看过 3 个”计数器。

- [ ] **Step 5: 运行 GREEN 与无障碍回归**

```bash
cd web && npm test -- src/components/ui/drawer-focus-contract.test.ts
cd web && npm run typecheck && npm run lint
```

Expected: 鼠标不动时不自动出 Tooltip，Tab/focus 仍即时展示。

### Task 6: 真实恢复、浏览器和静态清理门

- [x] **Step 1: 真实 PostgreSQL 恢复测试**

在一次性 test schema 中创建 job，worker A claim 后模拟崩溃；将 lease 推进到过期，worker B 重领、回写 running，再完成。断言：

```text
不请求任何 GET
job 只有一行，终态 completed
replacement chunks 全部可见，旧 chunks 全部不可见
import_file 终态 needs_review
旧 KG evidence 被撤销，无 partial snapshot
```

- [ ] **Step 2: 浏览器验收**

Playwright 真实流程：启动解析后关闭 Drawer，切换路由，列表进度继续变化；重开 Drawer 看到同一 job ID/阶段；打开 FAQ/KG/评测 Drawer 鼠标不动时均无 Tooltip。

- [ ] **Step 3: 静态扫描旧路径**

```bash
rg -n "parse-status|reparse_import_file|parse_batch_id|parse_file_name|BackgroundTasks.*parse|get_import_parse_status" cyclops web/src tests sql
```

Expected: 除迁移/“不存在”契约测试外零命中；正式代码没有旧执行路径。

- [ ] **Step 4: 完整门**

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py tests/test_import_parse_worker.py -q
.venv/bin/python -m ruff check .
cd web && npm test && npm run typecheck && npm run lint && npm run build
git diff --check
```

Expected: 全部通过，并重建 ASGI 实际服务的 `cyclops/static/dist`。

## Rollback

回滚只通过回退整个实施单元的 schema/code 变更并重启服务，不在运行时切回 GET 驱动或页面 `BackgroundTasks`。已存在的 provider batch 可成为孤儿外部任务，但因 PostgreSQL job generation/fingerprint 门禁不会替换当前文档 snapshot；只需在 provider 侧按运维规则清理。
