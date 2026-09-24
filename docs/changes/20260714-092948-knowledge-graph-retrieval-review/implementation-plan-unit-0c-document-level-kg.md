# Document-level KG Map / Resolve / Reduce Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development task-by-task. 先完成 Unit 0 来源实时重算与 Unit 0b 持久 worker 生命周期；本计划不保留通用 KG POST、`document_chunk` 公开入口、网页 `BackgroundTasks`、同步执行 wrapper、旧状态字段或运行时 fallback，也不自动提交 Git commit。

**Goal:** 把知识图谱抽取硬切为 FAQ 独立资源任务与整篇文档持久父任务；文档在后端完成逐切片 Map、确定性预归并、单文档受约束实体 resolution 和关系 Reduce，并在来源 manifest 未变化时一次事务发布完整审核 snapshot。

**Architecture:** PostgreSQL 继续是唯一 KG 写模型，并以 `kg_extraction_jobs` + `kg_extraction_job_items` 保存可恢复 lease、不可变文档 manifest 和不对外的 Map/resolution staging。worker 每次只推进一个可幂等阶段；模型只负责切片事实抽取和已有 local entity ID 分组，最终 canonical ID、字段归并、关系端点重映射、证据并集及 snapshot 替换全部由确定性代码完成。

**Tech Stack:** Python 3.11、FastAPI lifespan、asyncio、psycopg 3、PostgreSQL 16、OpenAI-compatible Chat、React 19.2、TanStack Query、pytest、Node test runner。

---

## File map

- Create: `cyclops/document_kg.py` — 文档 manifest、local ID、确定性预归并、resolution 校验和 Reduce 纯函数。
- Modify: `cyclops/kg.py` — 证据原文匹配、code-point char offsets、公开 canonical KG ID helper 和 FAQ 当前 parser。
- Modify: `cyclops/kg_ai.py` — 切片 Map 与受约束 entity-resolution 两种明确模型调用；不生成跨切片关系。
- Modify: `cyclops/db/kg.py` — FAQ/document 显式排队、lease claim、Map staging、resolution 阶段、整文档原子 snapshot 和只读 job DTO。
- Create: `cyclops/kg_extraction_worker.py` — 在 ASGI 生命周期内恢复并逐阶段推进持久 KG job。
- Modify: `cyclops/admin_server.py` — FAQ/document 资源级业务入口和 worker 单步执行；删除通用 queue/run/source dispatch。
- Modify: `cyclops/asgi_app.py` — 启停 KG worker，注册资源路由，删除通用 POST 与 KG `BackgroundTasks`。
- Modify: `cyclops/config.py`, `.env.example` — 唯一 KG worker poll/lease 配置及严格范围校验。
- Modify: `sql/001_init.sql` — 当前 job/job-item schema、staging JSON object constraints、唯一 active generation、evidence offsets 和一次性 0→1 KG 数据重置。
- Create: `tests/test_document_kg.py` — manifest、offset、premerge、resolution 与 Reduce 的纯函数契约。
- Create: `tests/test_kg_extraction_worker.py` — 无 HTTP 推进、lease 恢复、阶段幂等和单 job 失败隔离。
- Create: `tests/test_document_kg_postgres.py` — 真实 PostgreSQL manifest fence、旧 snapshot 保留、原子发布和 live `source_count`。
- Modify: `tests/test_kg.py`, `tests/test_db.py`, `tests/test_admin_server.py`, `tests/test_asgi_app.py`, `tests/test_kg_postgres_concurrency.py`, `tests/test_config.py` — 当前 Python/SQL/HTTP/锁序契约并删除旧断言。
- Modify: `web/src/api/schemas.ts`, `web/src/api/hooks.ts`, `web/src/api/kg-jobs.ts`, `web/src/api/kg-jobs.test.ts`, `web/src/api/schemas-contract.test.ts` — phase/job DTO、资源级 create/latest 查询和完成边界 cache invalidation。
- Modify: `web/src/pages/faqs/faq-drawer.tsx`, `web/src/pages/faqs/kg-actions.ts`, `web/src/pages/faqs/kg-actions.test.ts` — FAQ 独立任务入口与恢复中的当前任务状态。
- Modify: `web/src/pages/documents/document-drawer.tsx`, `web/src/pages/documents/chunk-browser.tsx`, `web/src/pages/documents/kg-actions.ts`, `web/src/pages/documents/kg-actions.test.ts` — 文件级入口、Map/Resolve/Reduce 进度与单切片入口删除。
- Modify: `web/src/pages/KnowledgeGraphPage.tsx`, `web/src/pages/kg/helpers.test.ts`, `web/src/pages/kg/entry-points.test.ts`, `web/src/pages/kg/kg-cache-contract.test.ts` — evidence offset 展示、资源归属和任务完成后的统一 KG cache 契约。
- Create: `web/src/pages/documents/document-kg-progress.test.ts` — phase、切片计数、按钮门禁和终态显示契约。
- Modify: `.trellis/spec/backend/cyclops-db-contracts.md` — 实施完成后记录文档 snapshot、manifest fence 和 evidence offset 当前契约。
- Modify: `docs/changes/20260714-092948-knowledge-graph-retrieval-review/update-plan.md`, `docs/changes/20260714-092948-knowledge-graph-retrieval-review/confirmation.md` — 实施完成后记录实际验证结果，不改写已确认决策。

## 唯一 wire contract

```python
KgExtractionJob = {
    "id": str,
    "source_type": "faq" | "document",
    "source_id": str,
    "phase": "queued" | "mapping" | "resolving" | "reducing" | "completed" | "failed",
    "processed_chunks": int,
    "total_chunks": int,
    "entity_count": int,
    "relation_count": int,
    "evidence_count": int,
    "model": str | None,
    "error": str | None,
    "created_at": datetime,
    "updated_at": datetime,
}
```

```text
POST /api/faqs/{faq_id}/kg-extraction-jobs
body: {}
response: queued KgExtractionJob

GET /api/faqs/{faq_id}/kg-extraction-jobs/latest
response: KgExtractionJob | null

POST /api/import/files/{file_id}/kg-extraction-jobs
body: {}
response: queued KgExtractionJob

GET /api/import/files/{file_id}/kg-extraction-jobs/latest
response: KgExtractionJob | null

GET /api/kg/extraction-jobs/{job_id}
response: KgExtractionJob
```

`POST /api/kg/extraction-jobs` 整体删除。空 HTTP body 仍由 `_read_json()` 拒绝；只有显式 JSON `{}` 合法，任何字段都返回 400。公开响应不得包含 `source_fingerprint`、manifest item、`map_result`、`resolution_result`、lease token、attempt count 或 provider prompt。

## 内部数据契约

文档 manifest 由未禁用切片按 `(chunk_index, id)` 排序后生成。任一未禁用切片正文为空时拒绝排队；禁用切片不进入 generation。`chunk_order` 是该稳定排序中的 0-based 序号，fingerprint 使用 `ensure_ascii=False`、`sort_keys=True`、紧凑 separators 的 canonical JSON SHA-256。

```python
DocumentKgManifest = {
    "file_id": str,
    "source_title": str,
    "fingerprint": str,
    "items": [
        {
            "chunk_id": str,
            "chunk_order": int,
            "source_fingerprint": str,
            "section_path": list[str],
            "page_start": int | None,
            "page_end": int | None,
        }
    ],
}
```

Map staging 的每条 evidence 固定为：

```python
DocumentKgEvidence = {
    "source_type": "document",
    "source_id": file_id,
    "source_chunk_id": chunk_id,
    "source_title": original_name,
    "section_path": list[str],
    "page_start": int | None,
    "page_end": int | None,
    "excerpt": str,
    "char_start": int,
    "char_end": int,
}
```

offset 使用 Python/PostgreSQL Unicode code-point 下标，满足 `source_text[char_start:char_end] == excerpt`。模型仍只输出 `excerpt`；后端用首次精确子串命中确定 offset，不做 trim 以外的归一化、不做 fuzzy matching、不接受模型自报 offset。找不到原文时整个 Map item 失败。

隐藏的单片 Map JSON 固定为以下 shape；`chunk_order` 只服务确定性归并，最终写入 `kg_evidence` 时不作为证据字段保存：

```python
DocumentKgMapResult = {
    "chunk_id": str,
    "chunk_order": int,
    "entities": [
        {
            "local_entity_id": str,
            "name": str,
            "entity_type": str,
            "aliases": list[str],
            "description": str,
            "confidence": float | None,
            "evidence": list[DocumentKgEvidence],
        }
    ],
    "relations": [
        {
            "local_relation_id": str,
            "head_local_entity_id": str,
            "relation_type": str,
            "tail_local_entity_id": str,
            "description": str,
            "confidence": float | None,
            "evidence": list[DocumentKgEvidence],
        }
    ],
}
```

resolution 模型唯一输出：

```json
{"groups":[["kg_doc_local_a","kg_doc_local_b"]]}
```

每个 group 至少两个 ID；ID 必须来自 deterministic premerge、在整个响应中至多出现一次且 `entity_type` 完全相同。未列出的 ID 保持 singleton。额外字段、canonical name、关系、evidence、未知 ID、跨类型 group 都使父任务失败。

## Task 1: 锁定原文 offsets 与纯函数 Map/Resolve/Reduce

- [ ] **Step 1: 在 `tests/test_kg.py` 写 evidence offset RED**

```python
def test_parse_kg_extraction_response_matches_exact_excerpt_offsets():
    """证据 offset 必须指向模型实际看见的同一份 Unicode 原文。"""
    source_text = "报告导出失败。先检查权限；再次检查权限。"
    result = parse_kg_extraction_response(
        canonical_payload(excerpt="检查权限"),
        source_text=source_text,
        source=document_source(chunk_id="chunk_1"),
    )
    evidence = result["entities"][0]["evidence"][0]
    assert (evidence["char_start"], evidence["char_end"]) == (8, 12)
    assert source_text[evidence["char_start"]:evidence["char_end"]] == evidence["excerpt"]


def test_parse_kg_extraction_response_rejects_excerpt_absent_from_source():
    """模型改写或臆造的 excerpt 不能进入 staging。"""
    with pytest.raises(KnowledgeGraphExtractionError, match="exact source substring"):
        parse_kg_extraction_response(
            canonical_payload(excerpt="不存在的证据"),
            source_text="只包含真实原文。",
            source=document_source(chunk_id="chunk_1"),
        )
```

- [ ] **Step 2: 运行 offset RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_kg.py -k "excerpt_offsets or absent_from_source" -q
```

Expected: 因 parser 尚未要求 `source_text` 且 evidence 没有 `char_start/char_end` 而失败；测试必须成功 import，不能把签名写错当作目标 RED。

- [ ] **Step 3: 在 `cyclops/kg.py` 实现精确定位并硬切 parser 签名**

```python
def locate_kg_evidence_excerpt(source_text: str, excerpt: str) -> tuple[int, int]:
    """定位证据原文；重复文本取首次精确命中，不做模糊修复。"""
    start = source_text.find(excerpt)
    if start < 0:
        raise KnowledgeGraphExtractionError("evidence excerpt must be an exact source substring")
    return start, start + len(excerpt)


def parse_kg_extraction_response(
    payload: str | dict[str, Any],
    *,
    source_text: str,
    source: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """解析唯一 KG 模型 schema，并把每条证据绑定到精确原文位置。"""
```

把 `source_text` 传到 `_normalize_evidence()`；返回 evidence 增加两个整数。所有调用和测试一次改用新签名，不保留无 `source_text` overload 或默认值。

- [ ] **Step 4: 运行 offset GREEN 与既有 parser 回归**

Run:

```bash
.venv/bin/python -m pytest tests/test_kg.py -q
```

Expected: PASS；既有严格字段、枚举、confidence、duplicate evidence 测试继续通过。

- [ ] **Step 5: 创建 `tests/test_document_kg.py` 的 manifest RED**

```python
def test_document_manifest_is_ordered_and_fingerprints_every_locator():
    """manifest 顺序与 fingerprint 必须覆盖 ID、正文、页码、章节和文件标题。"""
    manifest = build_document_kg_manifest(
        import_file(file_id="imp_1", title="产品手册.pdf"),
        [chunk("b", index=2, text="B"), chunk("a", index=1, text="A")],
    )
    assert [item["chunk_id"] for item in manifest["items"]] == ["a", "b"]
    assert [item["chunk_order"] for item in manifest["items"]] == [0, 1]
    changed = build_document_kg_manifest(
        import_file(file_id="imp_1", title="产品手册.pdf"),
        [chunk("a", index=1, text="A", section_path=["新章节"]), chunk("b", index=2, text="B")],
    )
    assert changed["fingerprint"] != manifest["fingerprint"]


def test_document_manifest_rejects_enabled_blank_chunk():
    """启用但无正文的切片不能被静默跳过。"""
    with pytest.raises(ValueError, match="source_text"):
        build_document_kg_manifest(import_file(), [chunk("chunk_1", text="  ")])
```

- [ ] **Step 6: 写 premerge/resolution/Reduce RED**

测试固定以下矩阵：

```python
def test_premerge_is_exact_name_and_type_before_resolution():
    """精确同名同类型先合并，别名实体保留 local ID 给 resolution。"""
    premerged = premerge_document_kg_map_results(
        [map_result("控制台", "product_platform_module", chunk_order=0),
         map_result("控制台", "product_platform_module", chunk_order=1),
         map_result("管理后台", "product_platform_module", chunk_order=2)]
    )
    assert [item["name"] for item in premerged["entities"]] == ["控制台", "管理后台"]


def test_resolution_rejects_unknown_duplicate_and_cross_type_ids():
    """模型只能对已存在且同类型 local ID 做互斥分组。"""
    for payload, message in invalid_resolution_cases():
        with pytest.raises(KnowledgeGraphExtractionError, match=message):
            parse_document_entity_resolution_response(payload, entities=resolution_entities())


def test_reduce_only_rewrites_relations_found_by_map():
    """resolution 只能改端点等价类，不能提供或制造新关系。"""
    reduced = reduce_document_kg(
        premerged_with_two_mapped_relations(),
        {"groups": [["kg_doc_local_console", "kg_doc_local_admin"]]},
    )
    assert canonical_triples(reduced) == {
        ("控制台", "requires", "管理员权限"),
        ("控制台", "resolves_by", "刷新缓存"),
    }
    assert flattened_evidence(reduced) == sorted(original_map_evidence(), key=evidence_sort_key)
```

`invalid_resolution_cases()` 必须包含：额外顶层字段、非数组 groups、长度 1、未知 ID、同一 ID 出现两次、跨类型 ID、group 中非字符串。另加测试确认 canonical name 取最早 `(chunk_order, char_start, local_id)`，aliases 稳定去重，description 取 `(confidence 降序、长度降序、文本升序)` 第一项，confidence 取支持项最大值，evidence 按 `(chunk_order, char_start, char_end, excerpt)` 稳定并集。

- [ ] **Step 7: 运行文档纯函数 RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_document_kg.py -q
```

Expected: `cyclops.document_kg` 尚不存在而失败。

- [ ] **Step 8: 实现 `cyclops/document_kg.py` 与公开 canonical ID helper**

目标签名固定为：

```python
def build_document_kg_manifest(
    import_file: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成不可变文档 manifest；只纳入未禁用且有正文的当前切片。"""


def localize_document_kg_map_result(
    extraction: dict[str, Any],
    *,
    job_id: str,
    chunk_id: str,
    chunk_order: int,
) -> dict[str, Any]:
    """把单片 provisional canonical ID 改为 job/chunk 域内 local ID。"""


def premerge_document_kg_map_results(
    map_results: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """按规范化 name/type 预归并实体，并只归并已有 Map relations。"""


def parse_document_entity_resolution_response(
    payload: str | dict[str, Any],
    *,
    entities: list[dict[str, Any]],
) -> dict[str, list[list[str]]]:
    """校验模型只返回同类型 local entity ID groups。"""


def reduce_document_kg(
    premerged: dict[str, list[dict[str, Any]]],
    resolution: dict[str, list[list[str]]],
) -> dict[str, list[dict[str, Any]]]:
    """由代码选 canonical entity 并重映射、去重 Map 中已有关系。"""
```

在 `cyclops/kg.py` 把 `_entity_id` / `_relation_id` 政名为 `canonical_kg_entity_id` / `canonical_kg_relation_id` 并更新原调用；不保留旧私有函数转发层。

- [ ] **Step 9: 运行纯函数 GREEN 和 Ruff**

Run:

```bash
.venv/bin/python -m pytest tests/test_document_kg.py tests/test_kg.py -q
.venv/bin/python -m ruff check cyclops/document_kg.py cyclops/kg.py tests/test_document_kg.py tests/test_kg.py
```

Expected: PASS。

## Task 2: 用当前 schema 保存 durable parent job、items 与隐藏 staging

- [ ] **Step 1: 在 `tests/test_db.py` 写 schema RED**

```python
def test_document_kg_schema_has_durable_parent_items_and_exact_evidence_offsets():
    """当前 schema 只保存 faq/document 父任务、不可变 item manifest 和精确证据。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    assert "phase IN ('queued', 'mapping', 'resolving', 'reducing', 'completed', 'failed')" in schema
    assert "processed_chunks INTEGER NOT NULL DEFAULT 0" in schema
    assert "total_chunks INTEGER NOT NULL" in schema
    assert "CREATE TABLE IF NOT EXISTS kg_extraction_job_items" in schema
    assert "map_result JSONB" in schema
    assert "kg_extraction_jobs_one_active_source_idx" in schema
    assert "char_start INTEGER NOT NULL" in schema
    assert "char_end INTEGER NOT NULL" in schema
    assert "source_chunk_id" not in kg_job_table_block(schema)
    assert "document_chunk" not in kg_job_table_block(schema)
```

再断言 `map_result` / `resolution_result` 仅允许 object 或 SQL null、`processed_chunks <= total_chunks`、item 的 `(job_id, chunk_id)` 与 `(job_id, chunk_order)` 唯一、FAQ 不能进入 resolving/reducing、每个 source 只有一个 active generation。

- [ ] **Step 2: 运行 schema RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "document_kg_schema or durable_parent or exact_evidence_offsets" -q
```

Expected: 当前表仍含 `document_chunk/source_chunk_id/status=processing` 且 evidence 没有 offset，测试失败。

- [ ] **Step 3: 在 `sql/001_init.sql` 写一次性 0→1 reset 与 evidence 当前约束**

在 `cyclops_schema_migrations` 已创建后，以 marker `20260715_document_kg_pipeline_v1` 只执行一次：

```sql
DELETE FROM knowledge_chunks WHERE source_type IN ('kg_entity', 'kg_relation');
DELETE FROM kg_evidence;
DELETE FROM kg_relations;
DELETE FROM kg_entities;
DROP TABLE IF EXISTS kg_extraction_job_items;
DROP TABLE IF EXISTS kg_extraction_jobs;
INSERT INTO cyclops_schema_migrations (id) VALUES ('20260715_document_kg_pipeline_v1');
```

这是经确认的 0→1 数据重置，不写旧 job/evidence 读取兼容。fresh `kg_evidence` 定义加入：

```sql
char_start INTEGER NOT NULL,
char_end INTEGER NOT NULL,
CHECK (char_start >= 0 AND char_end > char_start),
CHECK (
    (source_type = 'faq' AND source_chunk_id IS NULL)
    OR (source_type = 'document' AND source_chunk_id IS NOT NULL)
)
```

已有数据库在 reset 后执行同一当前约束，不依赖 `CREATE TABLE IF NOT EXISTS` 补列：

```sql
ALTER TABLE kg_evidence
    ADD COLUMN IF NOT EXISTS char_start INTEGER,
    ADD COLUMN IF NOT EXISTS char_end INTEGER;

ALTER TABLE kg_evidence
    ALTER COLUMN char_start SET NOT NULL,
    ALTER COLUMN char_end SET NOT NULL;

ALTER TABLE kg_evidence
    DROP CONSTRAINT IF EXISTS kg_evidence_char_offsets_check,
    DROP CONSTRAINT IF EXISTS kg_evidence_source_locator_check,
    ADD CONSTRAINT kg_evidence_char_offsets_check
        CHECK (char_start >= 0 AND char_end > char_start),
    ADD CONSTRAINT kg_evidence_source_locator_check CHECK (
        (source_type = 'faq' AND source_chunk_id IS NULL)
        OR (source_type = 'document' AND source_chunk_id IS NOT NULL)
    );
```

- [ ] **Step 4: 创建当前 job 与 item 表**

```sql
CREATE TABLE IF NOT EXISTS kg_extraction_jobs (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('faq', 'document')),
    source_id TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued' CHECK (
        phase IN ('queued', 'mapping', 'resolving', 'reducing', 'completed', 'failed')
    ),
    processed_chunks INTEGER NOT NULL DEFAULT 0 CHECK (processed_chunks >= 0),
    total_chunks INTEGER NOT NULL CHECK (total_chunks >= 1),
    source_fingerprint TEXT NOT NULL,
    resolution_result JSONB,
    entity_count INTEGER NOT NULL DEFAULT 0 CHECK (entity_count >= 0),
    relation_count INTEGER NOT NULL DEFAULT 0 CHECK (relation_count >= 0),
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    model TEXT,
    error TEXT CHECK (error IS NULL OR char_length(error) <= 1000),
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (processed_chunks <= total_chunks),
    CHECK (resolution_result IS NULL OR jsonb_typeof(resolution_result) = 'object'),
    CHECK (source_type = 'document' OR phase NOT IN ('resolving', 'reducing'))
);

CREATE UNIQUE INDEX IF NOT EXISTS kg_extraction_jobs_one_active_source_idx
ON kg_extraction_jobs (source_type, source_id)
WHERE phase IN ('queued', 'mapping', 'resolving', 'reducing');

CREATE TABLE IF NOT EXISTS kg_extraction_job_items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES kg_extraction_jobs(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    chunk_order INTEGER NOT NULL CHECK (chunk_order >= 0),
    source_fingerprint TEXT NOT NULL,
    section_path JSONB NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    phase TEXT NOT NULL DEFAULT 'queued' CHECK (phase IN ('queued', 'mapping', 'mapped', 'failed')),
    map_result JSONB,
    error TEXT CHECK (error IS NULL OR char_length(error) <= 1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, chunk_id),
    UNIQUE (job_id, chunk_order),
    CHECK (jsonb_typeof(section_path) = 'array'),
    CHECK (map_result IS NULL OR jsonb_typeof(map_result) = 'object')
);
```

item 的 `chunk_id` 故意不设 FK：重解析/删除必须能完成并让 manifest fence 把 job 标 failed，不能由 staging 反向阻塞来源 mutation。

- [ ] **Step 5: 运行 schema GREEN 与重复初始化文本门**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "document_kg_schema or durable_parent or exact_evidence_offsets" -q
.venv/bin/python -m ruff check tests/test_db.py
```

Expected: PASS；marker 存在且 reset 语句只位于 marker block。

## Task 3: 实现显式排队、只读 DTO、lease 和 Map staging

- [ ] **Step 1: 写 FAQ/document 排队 RED**

在 `tests/test_db.py` 增加：

```python
def test_create_document_kg_job_locks_file_and_manifest_items_in_one_transaction():
    """排队必须在 file→chunk 锁序内创建父任务和全部 immutable items。"""
    job = db.create_document_kg_extraction_job("imp_1", model="mimo-v2.5-pro")
    assert job == public_job(source_type="document", source_id="imp_1", phase="queued", total_chunks=2)
    assert sql_order(conn.calls) == ["lock_file", "lock_chunks_by_id", "insert_job", "insert_items"]
    assert [call.params["chunk_order"] for call in item_inserts(conn)] == [0, 1]


def test_create_faq_kg_job_has_one_logical_chunk_and_no_item_rows():
    """FAQ 保持独立单来源契约，但复用 durable parent lifecycle。"""
    job = db.create_faq_kg_extraction_job("faq_1", model="mimo-v2.5-pro")
    assert job["source_type"] == "faq"
    assert (job["processed_chunks"], job["total_chunks"]) == (0, 1)
    assert not item_inserts(conn)
```

门禁矩阵：不存在/禁用/非解析终态文档、无切片、启用空正文、非 usable FAQ 都在 INSERT 前失败；同一 source 已有 active job 映射为明确 conflict，不复用旧 job ID。

- [ ] **Step 2: 写 public read 与 staging 隐藏 RED**

```python
def test_kg_job_public_reads_never_select_staging_or_lease_fields():
    """job GET/latest 只返回 wire 字段，内部 manifest 和模型 staging 不外露。"""
    for sql in (Database._get_kg_extraction_job_sql(), Database._latest_kg_extraction_job_sql()):
        assert "SELECT *" not in sql
        assert "source_fingerprint" not in selected_columns(sql)
        assert "resolution_result" not in selected_columns(sql)
        assert "lease_token" not in selected_columns(sql)
    assert Database.get_kg_extraction_job(db, "kg_job_1") == public_job()
```

- [ ] **Step 3: 运行 queue/read RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "create_document_kg_job or create_faq_kg_job or kg_job_public_reads" -q
```

Expected: 显式方法、manifest inserts 和当前 SELECT 尚不存在。

- [ ] **Step 4: 实现 queue/read 当前签名**

```python
def create_faq_kg_extraction_job(self, faq_id: str, *, model: str) -> dict[str, Any]:
    """锁定 usable FAQ 并创建 total_chunks=1 的唯一 queued job。"""


def create_document_kg_extraction_job(
    self,
    file_id: str,
    *,
    model: str,
) -> dict[str, Any]:
    """锁定整篇文档、生成 manifest 并原子写父任务及 item。"""


def get_kg_extraction_job(self, job_id: str) -> dict[str, Any] | None:
    """按 job ID 返回唯一公开 DTO，不读取 staging 字段。"""


def get_latest_kg_extraction_job(
    self,
    *,
    source_type: str,
    source_id: str,
) -> dict[str, Any] | None:
    """按 source 与 created_at/id 倒序读取最近 job 的公开 DTO。"""
```

删除 `create_kg_extraction_job()`；不保留根据 `source_type` 调用两个新方法的 DB wrapper。文档锁 SQL按 `chunk.id ASC FOR UPDATE` 获取全局稳定锁，再在 Python 中按 `(chunk_index, id)` 生成 manifest 顺序。

- [ ] **Step 5: 运行 queue/read GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "create_document_kg_job or create_faq_kg_job or kg_job_public_reads" -q
```

Expected: PASS。

- [ ] **Step 6: 写 lease 与 Map item RED**

```python
def test_claim_kg_job_uses_skip_locked_and_expired_lease():
    """多 worker 只能领取未租用或 lease 已过期的 active job。"""
    sql = normalize_sql(Database._claim_kg_extraction_job_sql())
    assert "for update skip locked" in sql
    assert "lease_expires_at < now()" in sql
    assert "next_attempt_at <= now()" in sql
    assert "attempt_count = attempt_count + 1" in sql


def test_complete_map_item_revalidates_source_and_advances_progress_atomically():
    """Map 结果只有在 job lease 和当前 chunk fingerprint 都匹配时才能入 staging。"""
    result = db.complete_document_kg_map_item(
        "kg_job_1",
        "kg_item_1",
        lease_token="lease_1",
        map_result=localized_map_result(),
    )
    assert result["processed_chunks"] == 1
    assert result["phase"] == "mapping"
    assert source_lock_index(conn) < stage_update_index(conn) < job_progress_index(conn)
```

再覆盖：最后一个 item 同事务进入 `resolving`；旧/错误 lease 不写；manifest item 已 mapped 时不二次增加；模型调用期间 chunk text/page/section/file title 改变时 stage 不写；Map item 失败后父 job 终态 failed。

- [ ] **Step 7: 运行 lease/Map RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "claim_kg_job or complete_map_item or map_item_manifest" -q
```

Expected: claim、item load/complete 和 lease SQL 尚不存在。

- [ ] **Step 8: 实现 lease 与 staging 当前签名**

```python
def claim_kg_extraction_job(self, *, lease_seconds: int) -> dict[str, Any] | None:
    """原子领取一个可推进 job；queued 在领取事务内进入 mapping。"""


def load_document_kg_map_item(
    self,
    job_id: str,
    *,
    lease_token: str,
) -> dict[str, Any] | None:
    """取得首个未 mapped item 和当前来源文本，并在返回前校验 frozen locator。"""


def complete_document_kg_map_item(
    self,
    job_id: str,
    item_id: str,
    *,
    lease_token: str,
    map_result: dict[str, Any],
) -> dict[str, Any]:
    """复核 job/item/source 后写隐藏 staging、推进计数并释放 lease。"""


def load_document_kg_map_results(
    self,
    job_id: str,
    *,
    lease_token: str,
) -> list[dict[str, Any]]:
    """只供 resolving/reducing worker 读取全部 mapped JSON，公开 API 不调用。"""


def save_document_kg_resolution(
    self,
    job_id: str,
    *,
    lease_token: str,
    resolution_result: dict[str, Any],
) -> dict[str, Any]:
    """把 resolving 结果持久化并原子进入 reducing，供崩溃后恢复。"""
```

每个推进方法成功后清空 lease；job 崩溃只能由 claim 重领，不能 catch 后直接无 token 更新。

- [ ] **Step 9: 运行 lease/Map GREEN 与 DB 目标回归**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "kg_job or map_item or kg_extraction_snapshot" -q
.venv/bin/python -m ruff check cyclops/db/kg.py tests/test_db.py
```

Expected: PASS；旧 `start_kg_extraction_job` 断言已删除而不是同时维护两套 lifecycle。

## Task 4: 实现受约束模型阶段与可恢复 KG worker

- [ ] **Step 1: 在 `tests/test_document_kg.py` 写 resolution prompt/parser RED**

```python
def test_resolution_prompt_only_contains_local_entity_candidates():
    """resolution 输入不包含可让模型新增 relation/evidence 的输出 schema。"""
    prompt = KnowledgeGraphAiAssistant.entity_resolution_user_prompt(premerged_entities())
    assert "kg_doc_local_console" in prompt
    assert "entity_type" in prompt
    assert "relations" not in prompt
    assert "evidence" not in prompt


def test_resolution_model_output_is_validated_before_db_stage():
    """模型 extra fields 和跨类型 groups 必须在 save_resolution 前失败。"""
```

- [ ] **Step 2: 运行 AI RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_document_kg.py -k "resolution_prompt or resolution_model" -q
```

Expected: `KnowledgeGraphAiAssistant` 还没有文档 resolution API。

- [ ] **Step 3: 实现 `cyclops/kg_ai.py` 两个明确阶段**

```python
def extract(
    self,
    *,
    source_text: str,
    source: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """执行 FAQ 或单个文档 Map；证据必须由 parser 精确匹配原文。"""


def resolve_document_entities(
    self,
    *,
    entities: list[dict[str, Any]],
) -> dict[str, list[list[str]]]:
    """仅让模型分组已存在 local entity ID，不接受 canonical/fact 输出。"""
```

`extract()` 把 `source_text` 传给新 parser。resolution system prompt 明确：只输出 `groups`，不得新增/改写 ID，不得跨 type，不得输出关系或 evidence。候选少于 2 时由调用方直接使用 `{"groups": []}`，不调用 Chat。

- [ ] **Step 4: 运行 AI GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_document_kg.py tests/test_kg.py -q
```

Expected: PASS。

- [ ] **Step 5: 创建 `tests/test_kg_extraction_worker.py` 的 worker RED**

```python
def test_worker_advances_document_without_any_http_get():
    """浏览器不轮询时，worker 仍依次推进 Map、Resolve、Reduce。"""
    admin = FakeAdmin(claimed=[mapping_job(), resolving_job(), reducing_job(), None])
    worker = KgExtractionWorker(admin, poll_interval_seconds=0.01, lease_seconds=180)
    assert worker.run_available_once() is True
    assert worker.run_available_once() is True
    assert worker.run_available_once() is True
    assert admin.processed == [
        ("kg_job_1", "mapping"),
        ("kg_job_1", "resolving"),
        ("kg_job_1", "reducing"),
    ]


def test_worker_reclaims_expired_lease_after_restart():
    """worker A 崩溃后，worker B 只能在 lease 过期后重领同一阶段。"""
```

再覆盖：一个 job 模型异常会被标 failed 且循环继续处理下一 job；stop 后 `run()` 及时结束；空队列使用 stop-event timeout，不 busy loop；同一个已 mapped item 不重复执行 Chat。

- [ ] **Step 6: 运行 worker RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_kg_extraction_worker.py -q
```

Expected: `cyclops.kg_extraction_worker` 尚不存在。

- [ ] **Step 7: 实现 `cyclops/kg_extraction_worker.py`**

```python
class KgExtractionWorker:
    """在 ASGI 生命周期内逐步推进持久 KG job，关闭时可控停止。"""

    async def run(self) -> None:
        """异步等待任务；同步数据库和 Chat 调用统一放到 asyncio.to_thread。"""
        while not self._stop_event.is_set():
            processed = await asyncio.to_thread(self.run_available_once)
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass

    def run_available_once(self) -> bool:
        """claim 一个 job 并调用 AdminApp 单步推进；不保存第二份状态。"""
```

`run_available_once()` 调用 `database.claim_kg_extraction_job()` 后只调用 `AdminApp.process_kg_extraction_job_step(job)`。异常由该业务方法使用同一 lease 标 failed；worker 记录日志并继续，不吞掉未持久化失败。

- [ ] **Step 8: 在 `tests/test_admin_server.py` 写阶段 orchestration RED**

固定断言：

```text
faq/mapping -> load exact FAQ -> one Chat Map -> atomic FAQ complete
document/mapping -> load one item -> one Chat Map -> localize -> complete item
document/resolving with 0/1 entity -> groups=[] without Chat -> phase reducing
document/resolving with 2+ entities -> premerge -> one resolution Chat -> validated save
document/reducing -> reload persisted maps/resolution -> deterministic reduce -> atomic document complete
any phase error -> fail with same lease and <=1000-char error
faq/resolving or unknown phase -> explicit failure, no fallback dispatch
```

- [ ] **Step 9: 运行 Admin phase RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_admin_server.py -k "kg_job_step or document_kg_map or document_kg_resolution or document_kg_reduce" -q
```

Expected: 现有 `run_kg_extraction_job()` 仍一次完成单来源任务，目标方法不存在。

- [ ] **Step 10: 实现 Admin 当前方法并删除 generic dispatch**

```python
def queue_faq_kg_extraction_job(
    self,
    faq_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """要求严格空对象并创建 FAQ 资源级 queued job。"""


def queue_document_kg_extraction_job(
    self,
    file_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """要求严格空对象并创建整篇文档父任务与 manifest items。"""


def get_latest_faq_kg_extraction_job(self, faq_id: str) -> dict[str, Any] | None:
    """读取 FAQ 最近一次公开 job DTO，不暴露 staging。"""


def get_latest_document_kg_extraction_job(self, file_id: str) -> dict[str, Any] | None:
    """读取整篇文档最近一次公开 job DTO，不接受 chunk ID。"""


def process_kg_extraction_job_step(self, job: dict[str, Any]) -> dict[str, Any]:
    """按已 claim source_type/phase 推进一步；一次调用至多一次 Chat。"""
```

删除 `queue_kg_extraction_job()`、`run_kg_extraction_job()`、`_kg_extraction_source()` 和 `VALID_KG_EXTRACTION_SOURCE_TYPES`。内部 dispatch 只接受 DB 当前 `faq|document` 与合法 phase，未知值抛错并进入 failed；不推断来源、不查询 ID 前缀。

- [ ] **Step 11: 运行 worker/Admin GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_kg_extraction_worker.py tests/test_admin_server.py -k "kg" -q
.venv/bin/python -m ruff check cyclops/kg_extraction_worker.py cyclops/kg_ai.py cyclops/admin_server.py tests/test_kg_extraction_worker.py
```

Expected: PASS。

## Task 5: 在一个事务发布完整文档 snapshot

- [ ] **Step 1: 在 `tests/test_db.py` 写 atomic completion RED**

```python
def test_complete_document_kg_job_fences_manifest_before_owner_writes():
    """job/file/items/chunks 全部复核后才能碰 canonical owner 或 evidence。"""
    result = db.complete_document_kg_extraction_job(
        "kg_job_1",
        lease_token="lease_1",
        extraction=reduced_document_extraction(),
    )
    assert call_order(conn) == [
        "lock_job",
        "lock_file",
        "lock_chunks",
        "validate_evidence",
        "entity_locks_or_upserts",
        "fresh_relation_read",
        "relation_locks_or_upserts",
        "delete_old_document_evidence",
        "insert_new_evidence",
        "live_owner_reconcile",
        "complete_job",
        "clear_staging",
    ]
    assert result["phase"] == "completed"
```

另写：manifest fingerprint 不同、chunk item 集合不同、offset 越界、offset substring 不等、evidence 指向另一 file/chunk、relation endpoint 缺失时，owner/evidence/completed SQL 零调用。

- [ ] **Step 2: 写 FAQ completion 与 live count 回归 RED**

FAQ 仍使用单来源 guard，但 completion 签名独立：

```python
def test_complete_faq_kg_job_uses_same_offset_and_live_reconcile_path():
    """FAQ 不走文档 Reduce，但发布必须复用 Unit 0 owner 实时重算。"""
    result = db.complete_faq_kg_extraction_job(
        "kg_job_faq",
        lease_token="lease_faq",
        extraction=faq_extraction_with_offsets(),
    )
    assert result["processed_chunks"] == result["total_chunks"] == 1
    assert exactly_one_live_reconcile_call(conn)
```

同组 SQL 测试还必须断言 `_kg_evidence_params()`、`_insert_kg_evidence_sql()`、实体/关系列表 JSON 和 valid-evidence projection SELECT 全部读写 `char_start/char_end`，不能只在 staging 中保留 offset。

- [ ] **Step 3: 运行 completion RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py -k "complete_document_kg_job or complete_faq_kg_job or document_manifest_fence" -q
```

Expected: 当前 completion 仍只接受单 source guard，无法整文档 fence/replace。

- [ ] **Step 4: 实现两个 completion 与共享私有 snapshot primitive**

```python
def complete_faq_kg_extraction_job(
    self,
    job_id: str,
    *,
    lease_token: str,
    extraction: dict[str, Any],
) -> dict[str, Any]:
    """复核 FAQ fingerprint 后原子替换 FAQ snapshot 并完成 job。"""


def complete_document_kg_extraction_job(
    self,
    job_id: str,
    *,
    lease_token: str,
    extraction: dict[str, Any],
) -> dict[str, Any]:
    """复核完整 manifest/evidence 后原子替换整篇文档 snapshot 并完成 job。"""


def _replace_kg_source_snapshot_in_conn(
    self,
    conn: Any,
    *,
    source_type: str,
    source_ids: list[str],
    source_chunk_ids: list[str] | None,
    extraction: dict[str, Any],
) -> dict[str, int]:
    """按 Unit 0 entity→fresh relation 两阶段锁序替换精确来源范围。"""
```

FAQ 传 `source_chunk_ids=None`；document 也传 `None`，因此删除该 file 全部旧 evidence，再插入完整 Reduce evidence。最后调用现有 `_reconcile_kg_owners_after_evidence_change_in_conn()` 计算 distinct live locator `source_count` 和 relation live evidence；不读模型 count、不新增文档专用计数 SQL。删除旧 `_replace_kg_extraction_source_snapshot_in_conn()` 与旧 `complete_kg_extraction_job()`，不留 wrapper。

- [ ] **Step 5: 实现失败清理**

```python
def fail_kg_extraction_job(
    self,
    job_id: str,
    *,
    lease_token: str,
    error: str,
) -> dict[str, Any]:
    """仅当前 lease 可把 active job 置 failed，并清除 map/resolution staging。"""
```

同一事务把当前 `mapping` item 标 failed、清空所有 item `map_result`、清空 parent `resolution_result/lease`，保留 parent 计数和 <=1000 字错误供 UI。它不调用 snapshot primitive，因此失败 generation 不写任何 candidate/evidence。

- [ ] **Step 6: 运行 completion GREEN 与既有锁序回归**

Run:

```bash
.venv/bin/python -m pytest tests/test_db.py tests/test_kg_postgres_concurrency.py -k "kg" -q
.venv/bin/python -m ruff check cyclops/db/kg.py tests/test_db.py
```

Expected: PASS；Unit 0 relation-only lock-only endpoint 语义和 source enable/disable 双向重算不变。

- [ ] **Step 7: 创建 `tests/test_document_kg_postgres.py` 的真实 PostgreSQL门**

测试在随机 schema 中使用生产 DB 方法覆盖：

```text
1. 两个文档切片 Map 完成后、resolution 前：审核表仍只见旧 snapshot。
2. 直接改变会进入 manifest 的 file title 或 chunk locator：final completion failed，旧 owner/evidence byte-for-byte 不变，新 local candidate 零行。
3. 通过生产 update/reparse 改正文：Unit 0 正常失效旧 evidence，同时 failed generation 仍不写 staging candidate。
4. 成功 final：同一事务后只见完整新 snapshot，job completed 与 counts 同时可见。
5. 同一实体由两个 chunk 支持：source_count=2；同 chunk 两条 evidence 仍只算一个 distinct source。
6. 两个切片共享 resolved entity 但 Map 没有关系：最终 relation 零行。
7. Map 明确的重复关系经 resolution 端点重映射后只有一条 relation，保留两片精确 evidence。
8. worker A claim 后崩溃：lease 过期由 worker B 从未完成 item/phase 恢复，最终只发布一次。
```

- [ ] **Step 8: 运行真实 PostgreSQL门**

Run:

```bash
set -a
source .env
set +a
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-$DATABASE_URL}"
.venv/bin/python -m pytest tests/test_document_kg_postgres.py tests/test_kg_postgres_concurrency.py -q
```

Expected: PASS；命令不打印 `.env` 或数据库密码。

## Task 6: 硬切资源路由并在 lifespan 启动 worker

- [ ] **Step 1: 在 `tests/test_asgi_app.py` 写 route surface RED**

```python
def test_asgi_exposes_resource_kg_jobs_and_removes_generic_post():
    """FAQ/document 只能从 owner 资源创建任务，通用 POST 不存在。"""
    pairs = route_pairs(create_app(admin_app=FakeAdminApp(), kg_worker_factory=fake_factory))
    assert ("POST", "/api/faqs/{faq_id}/kg-extraction-jobs") in pairs
    assert ("GET", "/api/faqs/{faq_id}/kg-extraction-jobs/latest") in pairs
    assert ("POST", "/api/import/files/{file_id}/kg-extraction-jobs") in pairs
    assert ("GET", "/api/import/files/{file_id}/kg-extraction-jobs/latest") in pairs
    assert ("GET", "/api/kg/extraction-jobs/{job_id}") in pairs
    assert ("POST", "/api/kg/extraction-jobs") not in pairs
```

- [ ] **Step 2: 写严格 body 与 queued response RED**

用 endpoint 直接调用和 `TestClient` 断言：显式 `{}` 传给相应 Admin 方法并立即返回 queued；空 HTTP body、`null`、数组、`{"source_type":"document"}`、`{"chunk_id":"chunk_1"}` 全部 400；POST 不创建 `BackgroundTasks`，也不调用 worker step。

- [ ] **Step 3: 写 lifespan RED**

```python
def test_lifespan_starts_and_stops_kg_worker_without_http_trigger():
    """服务启动即恢复 job，shutdown 先停止 worker 再关闭 DB pool。"""
    worker = FakeWorker()
    with TestClient(create_app(admin_app=fake_admin, kg_worker_factory=lambda _admin: worker)):
        assert worker.started is True
    assert worker.stopped is True
    assert worker.stop_order < fake_admin.db.close_order
```

- [ ] **Step 4: 运行 ASGI RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_asgi_app.py -k "resource_kg_jobs or kg_worker or kg_body" -q
```

Expected: 当前仍注册 generic POST 并依赖 FastAPI `BackgroundTasks`。

- [ ] **Step 5: 实现路由和 lifespan**

路由只做 HTTP 适配：

```python
@app.post("/api/faqs/{faq_id}/kg-extraction-jobs")
async def queue_faq_kg_job(faq_id: str, request: Request) -> Any:
    return _admin(request).queue_faq_kg_extraction_job(faq_id, await _read_json(request))


@app.post("/api/import/files/{file_id}/kg-extraction-jobs")
async def queue_document_kg_job(file_id: str, request: Request) -> Any:
    return _admin(request).queue_document_kg_extraction_job(file_id, await _read_json(request))
```

latest GET 分别固定 `source_type='faq'/'document'` 调 Admin 明确方法。基于 Unit 0b 的 `create_app()` 增加显式 `kg_worker_factory` 测试注入；生产构造 `KgExtractionWorker`，与 import parse worker 各有自己的 asyncio task，finally 依次 stop/await 后关闭 pool。删除 `BackgroundTasks` import（若其他路由不再使用）。

- [ ] **Step 6: 增加配置并运行配置 GREEN**

`Settings` 当前字段：

```python
kg_extraction_worker_poll_seconds: float = 0.5
kg_extraction_worker_lease_seconds: int = 180
```

环境名只允许 `KG_EXTRACTION_WORKER_POLL_SECONDS` / `KG_EXTRACTION_WORKER_LEASE_SECONDS`；poll > 0，lease > `chat_timeout_seconds`。在 `tests/test_config.py` 覆盖默认、合法覆盖、0/负数、lease 不大于 Chat timeout。

Run:

```bash
.venv/bin/python -m pytest tests/test_asgi_app.py tests/test_config.py tests/test_kg_extraction_worker.py -q
.venv/bin/python -m ruff check cyclops/asgi_app.py cyclops/config.py tests/test_asgi_app.py tests/test_config.py
```

Expected: PASS；POST response 不等待任何模型调用。

## Task 7: 前端改用文件级入口并展示同一持久 job

- [ ] **Step 1: 在 `web/src/api/schemas-contract.test.ts` 写 DTO RED**

```typescript
test('KG extraction job uses only the durable phase contract', () => {
  assert.match(schemasSource, /source_type:\s*'faq'\s*\|\s*'document'/)
  assert.match(schemasSource, /phase:\s*'queued'\s*\|\s*'mapping'\s*\|\s*'resolving'\s*\|\s*'reducing'\s*\|\s*'completed'\s*\|\s*'failed'/)
  assert.match(schemasSource, /processed_chunks:\s*number/)
  assert.match(schemasSource, /total_chunks:\s*number/)
  assert.doesNotMatch(kgJobBlock(schemasSource), /source_chunk_id|status:\s*'queued'|document_chunk/)
})

test('KG review evidence exposes exact source offsets', () => {
  assert.match(kgEvidenceBlock(schemasSource), /char_start:\s*number/)
  assert.match(kgEvidenceBlock(schemasSource), /char_end:\s*number/)
})
```

- [ ] **Step 2: 重写 `web/src/api/kg-jobs.test.ts` 的 phase helper RED**

```typescript
test('polls only durable active phases', () => {
  for (const phase of ['queued', 'mapping', 'resolving', 'reducing'] as const) {
    assert.equal(kgJobRefetchInterval(job(phase)), 1500)
  }
  assert.equal(kgJobRefetchInterval(job('completed')), false)
  assert.equal(kgJobRefetchInterval(job('failed')), false)
})

test('derives document progress from phase and exact chunk counts', () => {
  assert.deepEqual(documentKgProgress(job('mapping', { processed_chunks: 3, total_chunks: 8 })), {
    label: '切片抽取',
    countLabel: '3/8',
    terminal: false,
  })
  assert.equal(documentKgProgress(job('resolving')).label, '实体归并')
  assert.equal(documentKgProgress(job('reducing')).label, '关系归约')
})
```

删除两分钟 `DEFAULT_MAX_POLL_ATTEMPTS` 和 `waitForKgExtractionJob()`；持久 job 的生命周期不能由页面超时决定。

- [ ] **Step 3: 运行 API RED**

Run:

```bash
cd web && npm test -- src/api/kg-jobs.test.ts src/api/schemas-contract.test.ts
```

Expected: 当前 DTO 仍是 `document_chunk/status=processing`，helper 仍做两分钟 blocking wait。

- [ ] **Step 4: 实现 DTO、phase helper 和四个资源 hook**

```typescript
export function useCreateFaqKgExtractionJob() // POST owner route, body {}
export function useLatestFaqKgExtractionJob(faqId: string | null)
export function useCreateDocumentKgExtractionJob() // POST owner route, body {}
export function useLatestDocumentKgExtractionJob(fileId: string | null)
```

create mutation 只返回 queued 并 invalidate 对应 latest query，不等待 completed。latest query 对 active phase 每 1500ms refetch，terminal 停止；读取到 `completed` 时 `await invalidateKgReviewQueries(queryClient)`，读取 `failed` 时保留后端 error 给页面。删除 `useCreateKgExtractionJob()`，不保留 payload union 或 route fallback。

同一步把 `KgEvidence.char_start/char_end` 设为必填 `number`；`KnowledgeGraphPage.tsx` 的历史 evidence 行显示紧凑 `字符 {char_start}-{char_end}` 定位，不从 excerpt 长度重新推导。

- [ ] **Step 5: 运行 API GREEN**

Run:

```bash
cd web && npm test -- src/api/kg-jobs.test.ts src/api/schemas-contract.test.ts src/pages/kg/kg-cache-contract.test.ts
```

Expected: PASS；KG review cache 只在 completed 响应边界刷新，不在 queued mutation 成功时伪装候选已变化。

- [ ] **Step 6: 在 `web/src/pages/kg/entry-points.test.ts` 写入口移除 RED**

```typescript
test('uses FAQ and whole-document owner routes with no chunk KG action', () => {
  assert.match(faqDrawerSource, /useCreateFaqKgExtractionJob/)
  assert.match(documentDrawerSource, /useCreateDocumentKgExtractionJob/)
  assert.doesNotMatch(chunkBrowserSource, /KG 抽取|useCreate.*KgExtraction|document_chunk/)
  assert.doesNotMatch(hooksSource, /\/api\/kg\/extraction-jobs['"]/)
})
```

- [ ] **Step 7: 创建 `document-kg-progress.test.ts` 的 UI model RED**

测试固定：parsed 且 file/chunks 未禁用且无 active job 时按钮可用；active 时按钮禁用并显示 `processed_chunks/total_chunks`；resolving/reducing 显示中文 phase；failed 显示后端 error 且允许创建新 generation；completed 显示 entity/relation counts；切片工具栏仍保留编辑、禁用和 embedding，但没有 KG。

- [ ] **Step 8: 运行页面 RED**

Run:

```bash
cd web && npm test -- src/pages/kg/entry-points.test.ts src/pages/documents/document-kg-progress.test.ts
```

Expected: 当前按钮仍位于 `ChunkToolbar`，document drawer 没有 KG job。

- [ ] **Step 9: 把 KG 操作移动到 `document-drawer.tsx`**

文件级按钮放在现有解析/Embedding/假设问题操作组：

```tsx
<Button
  onClick={() => createDocumentKgJob.mutate(fileId)}
  disabled={!canExtractDocumentKg || kgProgress.active}
>
  {kgProgress.active ? <Loader2 className="size-3.5 animate-spin" /> : <Bot className="size-3.5" />}
  KG 抽取
</Button>
```

`TaskPanel` 新增 `DocumentKgProgressRow`，直接展示 latest job 的 phase、`processed_chunks/total_chunks`、失败原因和 completed counts。它不读取 `ChunkBrowser` 当前选中切片，也不把 mutation pending 当持久进度。`chunk-browser.tsx` 删除 KG import、mutation、tooltip、按钮和 `source_type:'document_chunk'` payload。

- [ ] **Step 10: 把 FAQ 改到独立 hook**

`faq-drawer.tsx` 使用 `useCreateFaqKgExtractionJob()` 和 `useLatestFaqKgExtractionJob(faqId)`；按钮保持“已保存、无 dirty、status=usable”的门禁，再叠加 active job 禁用。POST 成功提示“已开始 KG 抽取”；完成/失败文案只读 latest job，不从旧 blocking mutation 返回值推断。

- [ ] **Step 11: 运行页面 GREEN、类型和 lint**

Run:

```bash
cd web && npm test -- src/pages/kg/entry-points.test.ts src/pages/kg/helpers.test.ts src/pages/documents/document-kg-progress.test.ts src/pages/faqs/kg-actions.test.ts src/pages/documents/kg-actions.test.ts
cd web && npm run typecheck && npm run lint
```

Expected: PASS；单切片界面没有 KG 操作，整篇文件显示同一数据库 job 的阶段与切片计数。

## Task 8: 静态零命中、跨层回归与交付门

- [ ] **Step 1: 增加旧契约静态零命中测试**

在 Python/Node contract tests 中读取生产源码并要求以下模式零命中：

```text
POST /api/kg/extraction-jobs
source_type == "document_chunk"（KG job 上下文）
source_type: 'document_chunk'（前端 KG payload）
useCreateKgExtractionJob
queue_kg_extraction_job
run_kg_extraction_job
start_kg_extraction_job
complete_kg_extraction_job
_kg_extraction_source
BackgroundTasks（KG route）
DEFAULT_MAX_POLL_ATTEMPTS
```

迁移 marker 和“不得存在”测试字符串可命中；断言必须把 production file set 与 test/docs 分开，不能用忽略整个目录掩盖正式引用。

- [ ] **Step 2: 运行目标 Python 全门**

Run:

```bash
.venv/bin/python -m pytest tests/test_kg.py tests/test_document_kg.py tests/test_db.py tests/test_admin_server.py tests/test_asgi_app.py tests/test_kg_extraction_worker.py tests/test_document_kg_postgres.py tests/test_kg_postgres_concurrency.py tests/test_config.py -q
.venv/bin/python -m ruff check cyclops tests
```

Expected: PASS。

- [ ] **Step 3: 运行前端全门并重建实际静态产物**

Run:

```bash
cd web && npm test && npm run typecheck && npm run lint && npm run build
```

Expected: PASS，`cyclops/static/dist` 使用新的 FAQ/document 路由与 phase DTO。

- [ ] **Step 4: 运行静态扫描**

Run:

```bash
rg -n "queue_kg_extraction_job|run_kg_extraction_job|start_kg_extraction_job|complete_kg_extraction_job|_kg_extraction_source|useCreateKgExtractionJob|DEFAULT_MAX_POLL_ATTEMPTS" cyclops web/src
rg -n "source_type\s*[:=]+\s*['\"]document_chunk['\"]" cyclops/kg.py cyclops/kg_ai.py cyclops/document_kg.py cyclops/db/kg.py cyclops/admin_server.py cyclops/asgi_app.py web/src/api web/src/pages/faqs web/src/pages/documents
```

Expected: 两条命令在 production source 零命中。

- [ ] **Step 5: 浏览器手工验收**

使用一个至少三切片的本地非敏感文档：从文件 Drawer 创建 job，确认 Map 计数逐步增加；关闭 Drawer、切到 FAQ/KG 页面后任务继续；重开 Drawer 看到同一 job ID/phase；完成前 KG 审核列表无 partial candidate，完成后一次出现全部候选；切片工具栏没有 KG 按钮。再修改一个测试文档切片后创建新 job，确认旧 generation failed 且没有新 partial candidate。

- [ ] **Step 6: 更新 executable spec 与变更记录**

`.trellis/spec/backend/cyclops-db-contracts.md` 新增一个完整 scenario，记录 resource routes、manifest fence、local-ID-only resolution、Map-only relations、offset substring、one-transaction snapshot、Unit 0 live count 和失败终态。`update-plan.md` / `confirmation.md` 只记录实际实现和验证时间，不新增兼容例外。

- [ ] **Step 7: 最终 whitespace/diff 门**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` 无输出；工作区仍可包含用户原有修改，本实施单元不自动 commit。

## Rollback

回滚以整个 Unit 0c 的 schema/code 变更为边界并停止服务，不在运行时恢复通用 POST、单切片入口、`BackgroundTasks` 或旧 job 状态。尚未发布的 active/staging job 可直接删除；已 completed 的 canonical KG 仍由 PostgreSQL owner/evidence 事务保持一致。由于一次性 migration 按用户确认清除了旧 0→1 KG 数据，回滚代码不会尝试把旧 evidence shape 伪造为带 offset 的当前数据。
