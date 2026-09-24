# KG 来源计数与失效正确性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development task-by-task. 本计划在当前连续工作区执行；不得创建兼容方法、旧状态路径或自动提交 Git commit。

**Goal:** 让实体 `source_count` 始终等于实时有效去重来源数，在来源删除、禁用、正文变化或重解析后把零证据事实置为 `disabled`、共享来源事实置为 `needs_review`，并在来源重新启用时恢复精确计数但不自动恢复 `usable`。

**Architecture:** PostgreSQL 继续是唯一 KG 写模型。每个 mutation 在既有 entity→relation 全局锁序内先完成 evidence 删除/插入，再按实时来源门禁集中重算 owner 状态、计数与 revision，最后同步当前临时 synthetic projection；Unit 2 硬切 Neo4j 时删除该临时 projection 写入，不为它保留适配层。

**Tech Stack:** Python 3.11、psycopg 3、PostgreSQL、pytest、React/TypeScript、Vitest。

---

## File map

- Modify: `cyclops/db/kg.py` — 唯一来源失效、evidence mutation、实体计数与实体/关系状态重算。
- Modify: `cyclops/kg.py` — 删除模型候选 payload 中错误的 `source_count` 派生。
- Modify: `cyclops/db/imports.py`, `cyclops/db/faq.py` — disable/enable/status/content mutation 都进入唯一 reconciliation。
- Modify: `sql/001_init.sql` — 对现存错误 count/status/projection 做幂等精确修复。
- Modify: `tests/test_db.py` — recording-connection SQL/顺序/无兼容契约测试。
- Modify: `tests/test_kg.py` — parser 不再产生数据库派生计数。
- Modify: `tests/test_kg_postgres_concurrency.py` — 真实 PostgreSQL 生命周期、共享来源与锁序验证。
- Modify: `web/src/api/schemas.ts`, `web/src/api/hooks.ts`, `web/src/pages/KnowledgeGraphPage.tsx` — live count、evidence validity 和统一 KG cache invalidation。
- Create: `web/src/pages/kg/kg-cache-contract.test.ts` — 所有来源 mutation 的 cache 刷新契约。
- Modify: `web/src/api/schemas-contract.test.ts`, `web/src/pages/kg/helpers.test.ts` — 历史 evidence 与实时 count 不混用。
- Modify: `.trellis/spec/backend/cyclops-db-contracts.md` — 完成后记录精确计数和失效状态契约。
- Modify: `docs/changes/20260714-092948-knowledge-graph-retrieval-review/update-plan.md` — 记录 Unit 0 实际验证结果。

### Task 1: 用失败测试锁定实时计数与两种失效终态

- [x] **Step 1: 在 `tests/test_db.py` 写 recording tests**

新增测试，明确要求 evidence mutation 发生在 owner 重算之前，并禁止旧 `GREATEST`：

```python
def test_source_invalidation_deletes_evidence_before_reconciling_owner_state():
    """正文失效必须先删除旧证据，再按剩余实时证据决定 owner 状态。"""
    conn = _RecordingConnection(
        [
            ("SELECT DISTINCT evidence.entity_id AS id", [{"id": "kg_ent_1"}]),
            ("SELECT DISTINCT rel.id, rel.head_entity_id", []),
            ("FOR UPDATE OF ent", {"id": "kg_ent_1"}),
            ("rel.head_entity_id = ANY", [{"id": "kg_rel_1"}]),
            ("FOR UPDATE OF rel", {"id": "kg_rel_1"}),
        ]
    )
    db = Database("postgresql://unused")
    db._reconcile_kg_source_change_in_conn(
        conn,
        source_type="faq",
        source_ids=["faq_1"],
        delete_evidence=True,
    )
    delete_index = next(
        index for index, (sql, _params) in enumerate(conn.calls)
        if "DELETE FROM kg_evidence" in sql
    )
    entity_index = next(
        index for index, (sql, _params) in enumerate(conn.calls)
        if sql.lstrip().startswith("UPDATE kg_entities")
    )
    relation_index = next(
        index for index, (sql, _params) in enumerate(conn.calls)
        if sql.lstrip().startswith("UPDATE kg_relations")
    )
    assert delete_index < entity_index < relation_index


def test_kg_entity_upsert_does_not_preserve_historical_source_count():
    """候选 upsert 不得用 GREATEST 保留已经不存在的来源数。"""
    assert "GREATEST" not in Database._upsert_kg_entity_sql()


def test_parse_kg_extraction_response_does_not_emit_source_count():
    """模型解析只产生事实与 evidence，实时来源计数只能由数据库计算。"""
    parsed = parse_kg_extraction_response(
        {
            "entities": [{
                "name": "报告导出",
                "entity_type": "feature_ui_action",
                "aliases": [],
                "description": "导出入口",
                "confidence": 0.9,
                "evidence": [{"excerpt": "点击报告页的导出按钮。"}],
            }],
            "relations": [],
        },
        source={
            "source_type": "faq",
            "source_id": "faq_1",
            "source_chunk_id": None,
            "source_title": "如何导出报告",
            "section_path": [],
            "page_start": None,
            "page_end": None,
        },
    )
    assert "source_count" not in parsed["entities"][0]
```

同时把现有“统一回退 `needs_review`”断言改为检查重算 SQL，不接受 `disabled` 的调用方兜底分支。

- [x] **Step 2: 在 `tests/test_kg_postgres_concurrency.py` 写真实 PostgreSQL 失败测试**

用同一实体/关系分别建立以下数据：

```text
owner A: 只有待删除来源 imp_only
owner B: 来源 imp_deleted + faq_shared
entity B: faq_shared 下两条 evidence，但 source locator 相同
```

删除/禁用 `imp_deleted` 后断言：

```python
assert only_owner == {
    "status": "disabled",
    "source_count": 0,
    "review_revision": old_revision + 1,
}
assert shared_owner == {
    "status": "needs_review",
    "source_count": 1,
    "review_revision": old_revision + 1,
}
assert shared_relation["status"] == "needs_review"
```

再覆盖同一 `(source_type, source_id, COALESCE(source_chunk_id, ''))` 的重复 evidence 只计一次。

- [x] **Step 3: 运行测试并确认 RED 原因正确**

Run:

```bash
python -m pytest tests/test_db.py -k "source_invalidation or source_count" -q
TEST_DATABASE_URL="$TEST_DATABASE_URL" python -m pytest tests/test_kg_postgres_concurrency.py -k "source_count or source_invalidation" -q
```

Expected: 新测试因当前先统一写 `needs_review`、后删 evidence，以及 `GREATEST` 保留旧计数而失败；不能接受 fixture/schema 错误作为 RED。

### Task 2: 实现唯一的 evidence 后重算路径

- [x] **Step 1: 在 `cyclops/db/kg.py` 增加两条集中 SQL**

新增并只保留以下当前契约：

```python
@staticmethod
def _reconcile_kg_entities_after_evidence_change_sql() -> str:
    """按实时有效且去重的来源重算实体计数，并撤销旧审核结果。"""
    return """
    WITH live_counts AS (
        SELECT target.id,
               (COUNT(DISTINCT (
                   valid_ev.source_type,
                   valid_ev.source_id,
                   COALESCE(valid_ev.source_chunk_id, '')
               )) FILTER (WHERE
                   (valid_ev.source_type = 'faq' AND faq.status = 'usable')
                   OR (
                       valid_ev.source_type = 'document'
                       AND imp.id IS NOT NULL
                       AND chunk.id IS NOT NULL
                       AND imp.is_disabled = false
                       AND chunk.is_disabled = false
                   )
               ))::integer AS source_count
        FROM unnest(%(entity_ids)s::text[]) AS target(id)
        LEFT JOIN kg_evidence valid_ev ON valid_ev.entity_id = target.id
        LEFT JOIN faq_documents faq
          ON valid_ev.source_type = 'faq'
         AND faq.id = valid_ev.source_id
        LEFT JOIN import_files imp
          ON valid_ev.source_type = 'document'
         AND imp.id = valid_ev.source_id
        LEFT JOIN import_chunks chunk
          ON valid_ev.source_type = 'document'
         AND chunk.id = valid_ev.source_chunk_id
         AND chunk.file_id = imp.id
        GROUP BY target.id
    )
    UPDATE kg_entities entity
    SET source_count = live_counts.source_count,
        status = CASE WHEN live_counts.source_count = 0
                      THEN 'disabled' ELSE 'needs_review' END,
        review_revision = entity.review_revision + CASE
            WHEN entity.id = ANY(%(entity_revision_ids)s::text[]) THEN 1 ELSE 0 END,
        updated_at = now()
    FROM live_counts
    WHERE entity.id = live_counts.id
    RETURNING entity.id, entity.status, entity.source_count, entity.review_revision
    """


@staticmethod
def _reconcile_kg_relations_after_evidence_change_sql() -> str:
    """按实时关系证据重算终态；端点变化只会撤销 usable，不制造证据。"""
    return """
    WITH live_counts AS (
        SELECT target.id,
               (COUNT(valid_ev.id) FILTER (WHERE
                   (valid_ev.source_type = 'faq' AND faq.status = 'usable')
                   OR (
                       valid_ev.source_type = 'document'
                       AND imp.id IS NOT NULL
                       AND chunk.id IS NOT NULL
                       AND imp.is_disabled = false
                       AND chunk.is_disabled = false
                   )
               ))::integer AS evidence_count
        FROM unnest(%(relation_ids)s::text[]) AS target(id)
        LEFT JOIN kg_evidence valid_ev ON valid_ev.relation_id = target.id
        LEFT JOIN faq_documents faq
          ON valid_ev.source_type = 'faq'
         AND faq.id = valid_ev.source_id
        LEFT JOIN import_files imp
          ON valid_ev.source_type = 'document'
         AND imp.id = valid_ev.source_id
        LEFT JOIN import_chunks chunk
          ON valid_ev.source_type = 'document'
         AND chunk.id = valid_ev.source_chunk_id
         AND chunk.file_id = imp.id
        GROUP BY target.id
    )
    UPDATE kg_relations relation
    SET status = CASE WHEN live_counts.evidence_count = 0
                      THEN 'disabled' ELSE 'needs_review' END,
        review_revision = relation.review_revision + CASE
            WHEN relation.id = ANY(%(relation_revision_ids)s::text[]) THEN 1 ELSE 0 END,
        updated_at = now()
    FROM live_counts
    WHERE relation.id = live_counts.id
    RETURNING relation.id, relation.status, relation.review_revision
    """
```

实现时把共享 live-source predicate 提取为一个 SQL 生成 helper，避免实体计数、关系状态、审核门禁出现三份不同定义；新/修改 Python 方法必须带中文 docstring。

- [x] **Step 2: 用一个方法协调 owner 重算与临时 projection 状态**

把 `_apply_kg_source_invalidation_in_conn()` 替换为唯一新方法：

```python
def _reconcile_kg_owners_after_evidence_change_in_conn(
    self,
    conn: Any,
    *,
    entity_ids: list[str],
    relation_ids: list[str],
    entity_revision_ids: list[str],
    relation_revision_ids: list[str],
) -> None:
    """在 owner 已按全局顺序锁定后，重算 canonical 状态并同步当前投影。"""
```

该方法先重算实体，再重算全部直接/incident 关系；临时 `knowledge_chunks` 状态直接使用 SQL 返回的 canonical status，不得再次猜测 `needs_review`。普通来源状态/正文变化把 semantic target 放入 revision IDs；snapshot replacement 的已有 candidate 已由 upsert 递增、新 candidate 保持 revision 1，因此 candidate 不在 reconciliation 中再次递增。不保留旧方法别名。

- [x] **Step 3: 调整来源失效与 snapshot replacement 的 mutation 顺序**

固定顺序为：

```text
collect semantic targets
→ lock/upsert all entities in ID order
→ refresh + lock/upsert all relations in ID order
→ delete scoped old evidence（需要时）
→ insert replacement evidence（snapshot replacement 时）
→ reconcile exact entity source_count/status
→ reconcile direct + incident relation status
→ synchronize current projection status
```

删除 `_apply_kg_source_invalidation_in_conn()` 和旧 `_invalidate_kg_sources_in_conn()`，唯一入口命名为 `_reconcile_kg_source_change_in_conn()`；不保留 wrapper。`_upsert_kg_entity_sql()` 删除 `GREATEST` 和 payload count merge；`_kg_entity_params()` 不从 evidence 长度生成 count。最终值必须来自同一事务的 live evidence recount。候选、旧 owner 和共享 owner全部走同一重算方法。

- [x] **Step 4: 运行 GREEN 和现有锁序回归**

Run:

```bash
python -m pytest tests/test_db.py -k "kg and (source or snapshot or relation_only or review_revision)" -q
TEST_DATABASE_URL="$TEST_DATABASE_URL" python -m pytest tests/test_kg_postgres_concurrency.py -q
```

Expected: 全部通过；relation-only 来源的 endpoint 仍只参与锁，不改变实体 status/revision/source_count。

### Task 3: 验证所有来源 mutation 都进入唯一重算路径

- [x] **Step 1: 增加参数化行为测试**

覆盖并断言同一连接/事务内调用：

```text
FAQ 正文变化或删除
FAQ status 离开 usable
文档删除
文档文件禁用
文档文件重新启用
文档切片禁用
文档切片重新启用
文档重解析 replace_import_chunks
抽取 snapshot replacement
```

重新启用来源会让保留的 evidence 重新参与实时计数：count>0 的 owner 进入 `needs_review` 并递增一次 revision，绝不自动恢复 `usable`。setter 只在 stored enabled/disabled 值实际变化时触发 reconciliation；重复提交同一值不改变 revision。FAQ 从非 usable 回到 usable 使用同一规则。

- [x] **Step 2: 增加幂等 schema 数据修复**

在 `import_files/import_chunks` 及 `is_disabled` 都已存在后，schema 用与 runtime 相同的 live predicate 重算所有 entity count/status、relation status 和 synthetic projection status。只更新实际不一致的 owner；第一次修复时 revision 加一，第二次 `init_schema()` 不再变化。真实 PostgreSQL 测试 seed 错误 count/status 后连续初始化两次并比较完整行。

- [x] **Step 3: 运行完整 DB/KG 定向套件**

Run:

```bash
python -m pytest tests/test_db.py tests/test_kg.py tests/test_admin_server.py -k "kg or delete_import or replace_import" -q
python -m ruff check cyclops/db/kg.py tests/test_db.py tests/test_kg_postgres_concurrency.py
```

Expected: PASS，且 Ruff 无错误。

### Task 4: 让 API/UI 明确区分实时与历史证据

- [x] **Step 0: 完成 UI 布局 checkpoint**

在修改 KG 页面前给用户一份“保持现有审核表/抽屉布局，仅把证据数量改成有效来源/有效证据，并给失效历史 evidence 加低对比状态标记”的 UI prompt；收到布局图或用户明确允许沿用现有布局后再改 React 文件。后端 reconciliation 不受该 checkpoint 阻塞。

- [x] **Step 1: 先写 API/TypeScript 失败测试**

当前 wire 固定增加并要求：

```typescript
type KgEvidence = {
  id: string
  is_valid: boolean
}

type KgEntity = {
  source_count: number
}

type KgRelation = {
  evidence_count: number
}
```

列表 SQL 给每条历史 evidence 标注 `is_valid`；实体返回数据库精确 `source_count`，关系返回 live `evidence_count`。页面不得用 `evidence.length` 代替实时计数；失效 evidence 仍可审计但显示“来源已失效”。

- [x] **Step 2: 实现一个 KG cache invalidator**

```typescript
export async function invalidateKgReviewQueries(
  queryClient: QueryClient,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ['kg-entities'] }),
    queryClient.invalidateQueries({ queryKey: ['kg-relations'] }),
    queryClient.invalidateQueries({ queryKey: ['kg-subgraph'] }),
  ])
}
```

文档删除、文件/切片 enable/disable、切片正文修改、FAQ 内容/状态保存和 KG snapshot/审核 mutation 全部调用这一函数；不在各 hook 复制不完整的 query key 列表。

- [x] **Step 3: 运行前端 GREEN**

Run:

```bash
cd web && npm test && npm run typecheck && npm run lint && npm run build
```

Expected: PASS，实际 `cyclops/static/dist` 同步重建。

### Task 5: 文档化并执行静态清理门

- [x] **Step 1: 更新项目契约与 change log**

在 `.trellis/spec/backend/cyclops-db-contracts.md` 把来源失效契约更新为：精确 distinct locator 计数、零证据 disabled、共享来源 needs_review、evidence mutation 后重算、lock-only endpoint 不变。

- [x] **Step 2: 静态扫描**

Run:

```bash
rg -n "GREATEST\(kg_entities\.source_count|_apply_kg_source_invalidation_in_conn|_invalidate_kg_sources_in_conn|source_count.*len\(.*evidence" cyclops tests
git diff --check
```

Expected: 正式代码和测试均无旧计数/旧方法；`git diff --check` 无输出。

- [x] **Step 3: Unit 0 完成门**

Run:

```bash
python -m pytest tests/test_db.py tests/test_kg.py tests/test_kg_postgres_concurrency.py -q
python -m ruff check .
cd web && npm test && npm run typecheck && npm run lint && npm run build
```

只有真实 PostgreSQL 测试验证共享来源、零来源、distinct source locator 和锁序后，Unit 0 才可标记完成并开始 outbox。

## Completion record (2026-07-15)

- Backend live-state and API contract: `tests/test_db.py tests/test_kg.py` 为 `235 passed`.
- Real PostgreSQL concurrency/lifecycle gate: `tests/test_kg_postgres_concurrency.py` 为 `8 passed`.
- Frontend contract/cache/UI gate: `80 passed`; `npm run typecheck` and `npm run lint` passed.
- Read-only production-connection smoke executed entity/relation list SQL and confirmed boolean `is_valid` plus integer live counts; PostgreSQL `EXPLAIN` compiled the subgraph SQL.
- Target Ruff and `git diff --check` passed; old invalidation helpers, historical source-count preservation, evidence-length live counts, and `row.get("evidence_count", 0)` have zero production matches.
- Independent spec review found and closed the Drawer-unmount cache gap; independent quality review found and closed the mandatory subgraph count fallback. Final verdicts: spec compliant and Approved.
