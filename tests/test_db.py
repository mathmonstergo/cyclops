import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyclops.db import (
    Database,
    KgFactHit,
    build_document_knowledge_chunk_row,
    build_embedding_text,
    build_faq_knowledge_chunk_row,
    build_import_candidate_faq_row,
    compute_content_hash,
    format_vector,
    score_to_distance,
)
from cyclops.db.builders import child_knowledge_chunk_index
from cyclops.document_kg import build_document_kg_manifest
from cyclops.kg import build_faq_kg_source_text, build_kg_source_guard


_PARSE_JOB_NOW = datetime(2026, 7, 15, tzinfo=UTC)
_PARSE_JOB_LIVE_LEASE_EXPIRES_AT = datetime(2026, 7, 16, tzinfo=UTC)


def _kg_source_guard(
    source_text,
    *,
    source_type="faq",
    source_id="faq_1",
    source_chunk_id=None,
    source_title=None,
    section_path=None,
    page_start=None,
    page_end=None,
):
    """构造测试用 canonical KG guard，所有 locator 字段均显式进入唯一当前形状。"""
    resolved_title = source_title
    if resolved_title is None and source_type == "faq":
        first_line = str(source_text).splitlines()[0] if str(source_text) else ""
        resolved_title = first_line.removeprefix("问题：") or None
    return build_kg_source_guard(
        source_text=source_text,
        source={
            "source_type": source_type,
            "source_id": source_id,
            "source_chunk_id": source_chunk_id,
            "source_title": resolved_title,
            "section_path": list(section_path or []),
            "page_start": page_start,
            "page_end": page_end,
        },
    )


class _FakeQueryResult:
    """模拟单次数据库查询结果，避免测试连接依赖全局游标调用顺序。"""

    def __init__(self, value=None):
        """保存一条或多条预设结果，供 fetchone/fetchall 按真实接口读取。"""
        self.value = value

    def fetchone(self):
        """返回单条结果；列表响应取第一条，空列表返回 None。"""
        if isinstance(self.value, list):
            return self.value[0] if self.value else None
        return self.value

    def fetchall(self):
        """返回结果列表；单条响应自动包装，None 返回空列表。"""
        if isinstance(self.value, list):
            return self.value
        return [] if self.value is None else [self.value]


class _RecordingConnection:
    """按 SQL 片段路由预设响应，同时记录同一事务内的全部数据库操作。"""

    def __init__(self, responses=None):
        """接收有序的 ``(SQL 片段, 响应)``，优先使用第一个匹配项。"""
        self.responses = list(responses or [])
        self.calls = []

    def execute(self, sql, params=None):
        """记录查询并返回独立结果对象，避免后续 execute 覆盖前一次游标。"""
        normalized_params = params or {}
        self.calls.append((sql, normalized_params))
        for marker, response in self.responses:
            if marker in sql:
                value = response(sql, normalized_params) if callable(response) else response
                return _FakeQueryResult(value)
        return _FakeQueryResult()

    def __enter__(self):
        """作为 Database.connect 的事务上下文返回自身。"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """不吞掉业务异常，让测试能断言校验失败。"""
        return False


class _StatefulParseClaimConnection:
    """按 due/lease 事实模拟 claim，避免测试无条件回显已领取行。"""

    def __init__(self, jobs, *, now):
        """复制任务事实并固定数据库时钟，供三类 lease 行为重复断言。"""
        self.jobs = [dict(job) for job in jobs]
        self.now = now
        self.calls = []

    def execute(self, sql, params=None):
        """只模拟生产 claim SQL 的候选过滤、稳定排序和领取状态写入。"""
        normalized_params = params or {}
        self.calls.append((sql, normalized_params))
        if "WITH candidate AS" not in sql:
            return _FakeQueryResult()
        candidates = [
            job
            for job in self.jobs
            if job["status"] in {"queued", "submitting", "polling", "finalizing"}
            and job["next_poll_at"] <= self.now
            and (
                (
                    job["lease_token"] is None
                    and job["lease_expires_at"] is None
                )
                or (
                    job["lease_expires_at"] is not None
                    and job["lease_expires_at"] <= self.now
                )
            )
        ]
        if not candidates:
            return _FakeQueryResult()
        claimed = min(
            candidates,
            key=lambda job: (job["next_poll_at"], job["created_at"], job["id"]),
        )
        if claimed["status"] == "queued":
            claimed["status"] = "submitting"
        claimed["lease_token"] = normalized_params["lease_token"]
        claimed["lease_expires_at"] = self.now + timedelta(
            seconds=normalized_params["lease_seconds"]
        )
        return _FakeQueryResult(dict(claimed))

    def __enter__(self):
        """作为 Database.connect 的事务上下文返回自身。"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """不吞掉 claim 异常，保持与真实事务上下文一致。"""
        return False


def _import_parse_job_row(**overrides):
    """构造解析任务当前行，测试不得依赖缺字段默认修复。"""
    return {
        "id": "parse_job_1",
        "file_id": "imp_1",
        "status": "queued",
        "chunker_type": "naive",
        "input_fingerprint": "sha256:document-v1",
        "provider_batch_id": None,
        "provider_file_name": None,
        "progress": {},
        "error": None,
        "lease_token": None,
        "lease_expires_at": None,
        "next_poll_at": _PARSE_JOB_NOW,
        "created_at": _PARSE_JOB_NOW,
        "updated_at": _PARSE_JOB_NOW,
        **overrides,
    }


def _kg_source_invalidation_calls(conn):
    """筛选按来源启动 KG 回退的语义目标查询；候选写入与行锁随后统一排序。"""
    return [
        (sql, params)
        for sql, params in conn.calls
        if "SELECT DISTINCT evidence.entity_id AS id" in sql
        and "source_ids" in params
    ]


def _kg_source_evidence_deletion_calls(conn):
    """筛选来源正文变化后删除旧 KG 证据的事务 SQL。"""
    return [
        (sql, params)
        for sql, params in conn.calls
        if "DELETE FROM kg_evidence" in sql and "source_ids" in params
    ]


def _faq_projection_stale_calls(conn):
    """筛选 FAQ 正文变化后让统一知识投影退出 ready 的事务 SQL。"""
    return [
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE knowledge_chunks" in sql
        and "source_type = 'faq'" in sql
        and "embedding_status = 'stale'" in sql
    ]


def _faq_projection_status_calls(conn):
    """筛选 FAQ 审核状态同步到统一知识投影的事务 SQL。"""
    return [
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE knowledge_chunks" in sql
        and "source_type = 'faq'" in sql
        and "status = %(status)s" in sql
    ]


def _kg_relation_lock_responses(relation):
    """构造关系确认的 locator、端点升序锁和关系锁响应。"""
    entity_ids = sorted(
        {
            relation["head_entity_id"],
            relation["tail_entity_id"],
        }
    )
    return [
        ("SELECT rel.head_entity_id", relation),
        ("ent.id = ANY", [{"id": entity_id} for entity_id in entity_ids]),
        ("FOR UPDATE OF rel", relation),
    ]


def test_format_vector_outputs_pgvector_literal():
    assert format_vector([0.1, -0.2, 3]) == "[0.1,-0.2,3.0]"


def test_database_exposes_only_canonical_unified_retrieval_models():
    """统一检索落地后必须删除 FAQ-only search、RetrievedDocument 和属性别名。"""
    import cyclops.db as db_module

    assert not hasattr(Database, "search")
    assert not hasattr(Database, "upsert_knowledge_chunk")
    assert not hasattr(Database, "_kg_projection_exists_sql")
    assert not hasattr(db_module, "RetrievedDocument")
    for attribute in ("question", "answer", "category", "source_date"):
        assert not hasattr(db_module.RetrievedKnowledgeChunk, attribute)


def test_update_import_file_summary_has_no_legacy_parse_runtime_writer():
    """文件摘要 writer 必须硬删除旧 provider/progress 字段，不能继续写或转换。"""
    source = Path("cyclops/db/imports.py").read_text(encoding="utf-8")
    method_source = source.split("def update_import_file_summary", 1)[1].split(
        "def get_import_file",
        1,
    )[0]
    for field in ("parse_batch_id", "parse_file_name", "parse_progress"):
        assert field not in method_source

    current_file = {"id": "imp_1", "status": "processing"}
    conn = _RecordingConnection(
        responses=[
            ("UPDATE import_files", current_file),
            ("SELECT * FROM import_files", current_file),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    result = database.update_import_file_summary(
        "imp_1",
        parse_batch_id="batch_old",
        parse_file_name="file_old.pdf",
        parse_progress={"state": "old"},
    )

    assert result == current_file
    assert len(conn.calls) == 1
    assert "SELECT * FROM import_files" in conn.calls[0][0]


def test_import_parse_job_schema_has_current_lifecycle_and_one_active_job():
    """解析任务独立持久化，并由 partial unique index 限制每文件一个活跃任务。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    normalized = " ".join(schema.split())
    table = schema.split("CREATE TABLE IF NOT EXISTS import_parse_jobs (", 1)[1].split(
        "CREATE UNIQUE INDEX IF NOT EXISTS import_parse_jobs_one_active_per_file_idx",
        1,
    )[0]
    normalized_table = " ".join(table.split())

    assert "CREATE TABLE IF NOT EXISTS import_parse_jobs" in schema
    assert "id TEXT PRIMARY KEY" in table
    assert (
        "file_id TEXT NOT NULL REFERENCES import_files(id) ON DELETE CASCADE"
        in normalized_table
    )
    assert "status IN ('queued', 'submitting', 'polling', 'finalizing', 'completed', 'failed')" in normalized
    assert "chunker_type TEXT NOT NULL" in table
    assert "chunker_type IN ('naive', 'manual', 'qa', 'table')" in normalized_table
    assert "input_fingerprint TEXT NOT NULL" in table
    assert "provider_batch_id TEXT" in table
    assert "provider_file_name TEXT" in table
    assert "progress JSONB NOT NULL DEFAULT '{}'::jsonb" in table
    assert "error TEXT" in table
    assert "lease_token TEXT" in table
    assert "lease_expires_at TIMESTAMPTZ" in table
    assert "next_poll_at TIMESTAMPTZ NOT NULL DEFAULT now()" in table
    assert "created_at TIMESTAMPTZ NOT NULL DEFAULT now()" in table
    assert "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()" in table
    assert "import_parse_jobs_progress_object_check" in table
    assert "CHECK (jsonb_typeof(progress) = 'object')" in table
    assert "import_parse_jobs_lease_pair_check" in table
    assert (
        "CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL))"
        in normalized_table
    )
    assert "CREATE UNIQUE INDEX IF NOT EXISTS import_parse_jobs_one_active_per_file_idx" in schema
    assert re.search(
        r"ON import_parse_jobs\s*\(file_id\)\s*WHERE status IN "
        r"\('queued', 'submitting', 'polling', 'finalizing'\)",
        normalized,
    )
    assert not re.search(
        r"UPDATE import_parse_jobs\s+SET progress\s*=\s*'\{\}'::jsonb",
        normalized,
        flags=re.IGNORECASE,
    )


def test_create_import_parse_job_locks_only_file_before_active_job_check():
    """创建任务只锁文件串行化同源入队，与终态共同遵循来源优先锁序。"""
    queued_job = _import_parse_job_row()
    conn = _RecordingConnection(
        responses=[
            ("FOR UPDATE OF imp", {"id": "imp_1", "status": "parsed"}),
            ("FROM import_parse_jobs", None),
            ("INSERT INTO import_parse_jobs", queued_job),
            ("UPDATE import_files", {"id": "imp_1", "status": "processing"}),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    result = database.create_import_parse_job(
        "imp_1",
        chunker_type="naive",
        input_fingerprint="sha256:document-v1",
    )

    assert result == queued_job
    normalized_calls = [" ".join(sql.split()) for sql, _ in conn.calls]
    assert "FROM import_files imp" in normalized_calls[0]
    assert "FOR UPDATE OF imp" in normalized_calls[0]
    assert "FROM import_parse_jobs" in normalized_calls[1]
    assert "status IN ('queued', 'submitting', 'polling', 'finalizing')" in normalized_calls[1]
    assert "FOR UPDATE OF job" not in normalized_calls[1]
    assert "INSERT INTO import_parse_jobs" in normalized_calls[2]
    assert "UPDATE import_files" in normalized_calls[3]
    assert conn.calls[2][1]["status"] == "queued"
    assert conn.calls[2][1]["chunker_type"] == "naive"
    assert conn.calls[2][1]["input_fingerprint"] == "sha256:document-v1"
    assert json.loads(conn.calls[2][1]["progress"]) == {}
    assert conn.calls[3][1] == {
        "file_id": "imp_1",
        "status": "processing",
        "chunker_type": "naive",
    }


def test_create_import_parse_job_fails_explicitly_when_active_job_exists():
    """文件已有活跃任务时必须显式冲突，不能依赖覆盖或复用旧任务。"""
    conn = _RecordingConnection(
        responses=[
            ("FOR UPDATE OF imp", {"id": "imp_1"}),
            ("FROM import_parse_jobs", {"id": "parse_job_active"}),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="active import parse job"):
        database.create_import_parse_job(
            "imp_1",
            chunker_type="naive",
            input_fingerprint="sha256:document-v1",
        )

    assert len(conn.calls) == 2
    assert not any("INSERT INTO import_parse_jobs" in sql for sql, _ in conn.calls)
    assert not any("UPDATE import_files" in sql for sql, _ in conn.calls)


@pytest.mark.parametrize(
    ("file_id", "chunker_type", "input_fingerprint", "message"),
    [
        ("", "naive", "sha256:document-v1", "file_id"),
        ("imp_1", "legacy", "sha256:document-v1", "chunker_type"),
        ("imp_1", "naive", "", "input_fingerprint"),
    ],
)
def test_create_import_parse_job_rejects_invalid_current_fields_before_sql(
    file_id,
    chunker_type,
    input_fingerprint,
    message,
):
    """创建任务只接受完整 current 字段，非法输入不得触发数据库写入。"""
    conn = _RecordingConnection()
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match=message):
        database.create_import_parse_job(
            file_id,
            chunker_type=chunker_type,
            input_fingerprint=input_fingerprint,
        )

    assert conn.calls == []


def test_import_parse_job_readers_use_canonical_ids_and_latest_order():
    """任务读取只按当前表查询，文件详情按创建时间与 ID 稳定选择最新任务。"""
    job = _import_parse_job_row()
    conn = _RecordingConnection(
        responses=[
            ("WHERE id = %(job_id)s", job),
            ("WHERE file_id = %(file_id)s", job),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    assert database.get_import_parse_job("parse_job_1") == job
    assert database.get_latest_import_parse_job_for_file("imp_1") == job

    first_sql, first_params = conn.calls[0]
    latest_sql, latest_params = conn.calls[1]
    assert "FROM import_parse_jobs" in first_sql
    assert first_params == {"job_id": "parse_job_1"}
    assert "FROM import_parse_jobs" in latest_sql
    assert "ORDER BY created_at DESC, id DESC" in " ".join(latest_sql.split())
    assert "LIMIT 1" in latest_sql
    assert latest_params == {"file_id": "imp_1"}


@pytest.mark.parametrize("progress", [None, "{}", []])
def test_get_import_parse_job_rejects_non_object_database_progress(progress):
    """数据库任务 progress 若被破坏必须显式失败，不解析或替换成空对象。"""
    conn = _RecordingConnection(
        responses=[("WHERE id = %(job_id)s", _import_parse_job_row(progress=progress))]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(TypeError, match="progress must be a JSON object"):
        database.get_import_parse_job("parse_job_1")


def test_get_import_parse_job_rejects_missing_database_progress():
    """读取 current row 缺少必填 progress 时保留 KeyError，不做字段默认补齐。"""
    job = _import_parse_job_row()
    del job["progress"]
    conn = _RecordingConnection(responses=[("WHERE id = %(job_id)s", job)])
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(KeyError, match="progress"):
        database.get_import_parse_job("parse_job_1")


def test_claim_import_parse_job_uses_due_skip_locked_and_unpredictable_lease():
    """多 worker 原子领取到期任务，并为每次领取生成新的不可预测 lease token。"""
    claimed_tokens = []

    def claimed_job(_sql, params):
        """回显本次 token，便于断言 writer 参数来自当前领取。"""
        claimed_tokens.append(params["lease_token"])
        return _import_parse_job_row(
            status="submitting",
            lease_token=params["lease_token"],
            lease_expires_at=datetime(2026, 7, 15, 0, 0, 30, tzinfo=UTC),
        )

    conn = _RecordingConnection(responses=[("WITH candidate AS", claimed_job)])
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    first = database.claim_import_parse_job(lease_seconds=30)
    second = database.claim_import_parse_job(lease_seconds=30)

    sql = " ".join(Database._claim_import_parse_job_sql().split())
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "next_poll_at <= now()" in sql
    assert "lease_expires_at <= now()" in sql
    assert "status IN ('queued', 'submitting', 'polling', 'finalizing')" in sql
    assert "ORDER BY next_poll_at ASC, created_at ASC, id ASC" in sql
    assert "lease_token = %(lease_token)s" in sql
    assert "lease_expires_at = now() + make_interval(secs => %(lease_seconds)s)" in sql
    assert "WHEN job.status = 'queued' THEN 'submitting'" in sql
    assert first is not None and second is not None
    assert len(claimed_tokens) == 2
    assert claimed_tokens[0] != claimed_tokens[1]
    assert all(isinstance(token, str) and len(token) >= 32 for token in claimed_tokens)
    assert all(params["lease_seconds"] == 30 for _, params in conn.calls)


def test_claim_import_parse_job_claims_unleased_due_job():
    """没有 lease 且已到轮询时间的 queued 任务必须可领取并原子进入 submitting。"""
    conn = _StatefulParseClaimConnection(
        [
            _import_parse_job_row(
                next_poll_at=_PARSE_JOB_NOW - timedelta(seconds=1),
            )
        ],
        now=_PARSE_JOB_NOW,
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    claimed = database.claim_import_parse_job(lease_seconds=30)

    assert claimed is not None
    assert claimed["status"] == "submitting"
    assert claimed["lease_token"]
    assert claimed["lease_expires_at"] == _PARSE_JOB_NOW + timedelta(seconds=30)


def test_claim_import_parse_job_reclaims_expired_polling_lease():
    """服务重启后，已过期 polling lease 必须换新 token 并保持 polling 阶段。"""
    conn = _StatefulParseClaimConnection(
        [
            _import_parse_job_row(
                status="polling",
                provider_batch_id="batch_1",
                provider_file_name="document.pdf",
                lease_token="lease-expired",
                lease_expires_at=_PARSE_JOB_NOW - timedelta(seconds=1),
                next_poll_at=_PARSE_JOB_NOW - timedelta(seconds=2),
            )
        ],
        now=_PARSE_JOB_NOW,
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    claimed = database.claim_import_parse_job(lease_seconds=45)

    assert claimed is not None
    assert claimed["status"] == "polling"
    assert claimed["lease_token"] != "lease-expired"
    assert claimed["lease_expires_at"] == _PARSE_JOB_NOW + timedelta(seconds=45)


def test_claim_import_parse_job_does_not_return_live_lease():
    """尚未过期的 live lease 只属于当前 worker，其他 worker 不得重复领取。"""
    conn = _StatefulParseClaimConnection(
        [
            _import_parse_job_row(
                status="polling",
                provider_batch_id="batch_1",
                provider_file_name="document.pdf",
                lease_token="lease-live",
                lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                next_poll_at=_PARSE_JOB_NOW - timedelta(seconds=1),
            )
        ],
        now=_PARSE_JOB_NOW,
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    claimed = database.claim_import_parse_job(lease_seconds=30)

    assert claimed is None
    assert conn.jobs[0]["lease_token"] == "lease-live"
    assert conn.jobs[0]["lease_expires_at"] == _PARSE_JOB_LIVE_LEASE_EXPIRES_AT


def test_claim_import_parse_job_reclaims_expired_finalizing_after_restart():
    """finalizing 是可恢复活跃阶段；终结时崩溃后必须重领以继续原子完成。"""
    conn = _StatefulParseClaimConnection(
        [
            _import_parse_job_row(
                status="finalizing",
                provider_batch_id="batch_1",
                provider_file_name="document.pdf",
                lease_token="lease-finalizing-expired",
                lease_expires_at=_PARSE_JOB_NOW - timedelta(seconds=1),
                next_poll_at=_PARSE_JOB_NOW - timedelta(seconds=1),
            )
        ],
        now=_PARSE_JOB_NOW,
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    claimed = database.claim_import_parse_job(lease_seconds=30)

    assert claimed is not None
    assert claimed["status"] == "finalizing"
    assert claimed["lease_token"] != "lease-finalizing-expired"


@pytest.mark.parametrize("lease_seconds", [0, -1, True, 1.5])
def test_claim_import_parse_job_rejects_invalid_lease_seconds_before_sql(
    lease_seconds,
):
    """lease 秒数必须是正整数，布尔值和小数不能被隐式接受。"""
    conn = _RecordingConnection()
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="lease_seconds"):
        database.claim_import_parse_job(lease_seconds=lease_seconds)

    assert conn.calls == []


def test_renew_import_parse_job_lease_fences_token_and_active_status():
    """长 provider 调用只能用当前未过期 token 延长活跃任务 lease。"""
    conn = _RecordingConnection(
        responses=[("UPDATE import_parse_jobs AS job", {"id": "parse_job_1"})]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    renewed = database.renew_import_parse_job_lease(
        "parse_job_1",
        lease_token="lease-current",
        lease_seconds=30,
    )

    assert renewed is True
    renew_sql, renew_params = conn.calls[0]
    normalized = " ".join(renew_sql.split())
    assert "job.lease_token = %(lease_token)s" in normalized
    assert "job.lease_expires_at > now()" in normalized
    assert "job.status IN ('submitting', 'polling', 'finalizing')" in normalized
    assert "make_interval(secs => %(lease_seconds)s)" in normalized
    assert renew_params == {
        "job_id": "parse_job_1",
        "lease_token": "lease-current",
        "lease_seconds": 30,
    }


@pytest.mark.parametrize("progress", [None, "running", []])
def test_update_import_parse_job_progress_rejects_non_object_before_sql(progress):
    """任务进度只接受 JSON object，不把任何 scalar 或数组修复成空对象。"""
    conn = _RecordingConnection()
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(TypeError, match="progress must be a JSON object"):
        database.update_import_parse_job_progress(
            "parse_job_1",
            lease_token="lease-current",
            status="polling",
            progress=progress,
            provider_batch_id="batch_1",
            provider_file_name="document.pdf",
            next_poll_at=datetime(2026, 7, 15, 0, 0, 5, tzinfo=UTC),
        )

    assert conn.calls == []


def test_update_import_parse_job_progress_requires_all_current_fields():
    """进度 writer 的 provider locator 与 next poll 都是必填参数，不做缺字段推断。"""
    database = Database("postgresql://unused")

    with pytest.raises(TypeError, match="provider_file_name"):
        database.update_import_parse_job_progress(
            "parse_job_1",
            lease_token="lease-current",
            status="polling",
            progress={},
            provider_batch_id="batch_1",
            next_poll_at=datetime(2026, 7, 15, 0, 0, 5, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("provider_batch_id", "provider_file_name"),
    [(None, "document.pdf"), ("batch_1", None), ("", "document.pdf"), ("batch_1", "")],
)
def test_update_import_parse_job_progress_requires_complete_provider_locators(
    provider_batch_id,
    provider_file_name,
):
    """进入 polling 必须持有完整 provider locator，不能保存半套或空字符串。"""
    conn = _RecordingConnection()
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="provider"):
        database.update_import_parse_job_progress(
            "parse_job_1",
            lease_token="lease-current",
            status="polling",
            progress={"phase": "polling"},
            provider_batch_id=provider_batch_id,
            provider_file_name=provider_file_name,
            next_poll_at=datetime(2026, 7, 15, 0, 0, 5, tzinfo=UTC),
        )

    assert conn.calls == []


def test_update_import_parse_job_progress_fences_lease_and_releases_claim():
    """仅当前 lease 可推进合法阶段，写入 provider/progress/next poll 后必须清除 lease。"""
    current = _import_parse_job_row(
        status="submitting",
        lease_token="lease-current",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )
    updated = _import_parse_job_row(
        status="polling",
        provider_batch_id="batch_1",
        provider_file_name="document.pdf",
        progress={"phase": "polling", "percent": 25},
    )
    conn = _RecordingConnection(
        responses=[
            ("FOR UPDATE OF job", current),
            ("UPDATE import_parse_jobs AS job", updated),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn
    next_poll_at = datetime(2026, 7, 15, 0, 0, 5, tzinfo=UTC)

    result = database.update_import_parse_job_progress(
        "parse_job_1",
        lease_token="lease-current",
        status="polling",
        progress={"phase": "polling", "percent": 25},
        provider_batch_id="batch_1",
        provider_file_name="document.pdf",
        next_poll_at=next_poll_at,
    )

    assert result == updated
    update_sql, params = conn.calls[1]
    normalized = " ".join(update_sql.split())
    assert "job.lease_token = %(lease_token)s" in normalized
    assert "job.lease_expires_at > now()" in normalized
    assert "job.status = %(current_status)s" in normalized
    assert "lease_token = NULL" in normalized
    assert "lease_expires_at = NULL" in normalized
    assert "next_poll_at = %(next_poll_at)s" in normalized
    assert params == {
        "job_id": "parse_job_1",
        "lease_token": "lease-current",
        "current_status": "submitting",
        "status": "polling",
        "progress": json.dumps(
            {"phase": "polling", "percent": 25}, ensure_ascii=False
        ),
        "provider_batch_id": "batch_1",
        "provider_file_name": "document.pdf",
        "next_poll_at": next_poll_at,
    }


@pytest.mark.parametrize(
    "current_status",
    [
        "queued",
        "finalizing",
        "completed",
    ],
)
def test_update_import_parse_job_progress_rejects_invalid_transition(
    current_status,
):
    """阶段只能沿当前 lifecycle 前进，终态和逆向 transition 都明确失败。"""
    conn = _RecordingConnection(
        responses=[
            (
                "FOR UPDATE OF job",
                _import_parse_job_row(
                    status=current_status,
                    lease_token="lease-current",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="transition"):
        database.update_import_parse_job_progress(
            "parse_job_1",
            lease_token="lease-current",
            status="polling",
            progress={"phase": "polling"},
            provider_batch_id="batch_1",
            provider_file_name="document.pdf",
            next_poll_at=datetime(2026, 7, 15, 0, 0, 5, tzinfo=UTC),
        )

    assert len(conn.calls) == 1


def test_update_import_parse_job_progress_rejects_non_polling_target_before_sql():
    """progress writer 不是阶段万能入口，非 polling 目标必须在访问数据库前失败。"""
    conn = _RecordingConnection()
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="transition"):
        database.update_import_parse_job_progress(
            "parse_job_1",
            lease_token="lease-current",
            status="submitting",
            progress={"phase": "submitting"},
            provider_batch_id="batch_1",
            provider_file_name="document.pdf",
            next_poll_at=datetime(2026, 7, 15, 0, 0, 5, tzinfo=UTC),
        )

    assert conn.calls == []


def test_update_import_parse_job_progress_rejects_stale_lease():
    """旧 worker 的 lease token 不能覆盖新 worker 已领取的任务状态。"""
    conn = _RecordingConnection(
        responses=[
            (
                "FOR UPDATE OF job",
                _import_parse_job_row(
                    status="polling",
                    lease_token="lease-new",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="lease"):
        database.update_import_parse_job_progress(
            "parse_job_1",
            lease_token="lease-old",
            status="polling",
            progress={"phase": "polling"},
            provider_batch_id="batch_1",
            provider_file_name="document.pdf",
            next_poll_at=datetime(2026, 7, 15, 0, 0, 5, tzinfo=UTC),
        )

    assert len(conn.calls) == 1


def test_fail_import_parse_job_fences_lease_and_fails_file_atomically():
    """解析失败只允许当前 lease 落库，并在同一事务同步文件失败状态。"""
    current = _import_parse_job_row(
        status="polling",
        lease_token="lease-current",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )
    failed = _import_parse_job_row(
        status="failed",
        error="provider failed",
        lease_token=None,
    )
    conn = _RecordingConnection(
        responses=[
            ("SELECT job.*", current),
            ("FOR UPDATE OF job", current),
            ("FOR UPDATE OF imp", {"id": "imp_1", "status": "processing"}),
            ("UPDATE import_parse_jobs AS job", failed),
            ("UPDATE import_files AS imp", {"id": "imp_1", "status": "failed"}),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    result = database.fail_import_parse_job(
        "parse_job_1",
        lease_token="lease-current",
        error="provider failed",
    )

    assert result == failed
    assert "FOR UPDATE OF imp" in conn.calls[1][0]
    assert "FOR UPDATE OF job" in conn.calls[2][0]
    job_sql, job_params = conn.calls[3]
    file_sql, file_params = conn.calls[4]
    assert "job.lease_token = %(lease_token)s" in " ".join(job_sql.split())
    assert "job.lease_expires_at > now()" in " ".join(job_sql.split())
    assert "job.status = %(current_status)s" in " ".join(job_sql.split())
    assert "status = 'failed'" in job_sql
    assert "lease_token = NULL" in job_sql
    assert "lease_expires_at = NULL" in job_sql
    assert job_params == {
        "job_id": "parse_job_1",
        "lease_token": "lease-current",
        "current_status": "polling",
        "error": "provider failed",
    }
    assert "status = 'failed'" in file_sql
    assert file_params == {"file_id": "imp_1", "error": "provider failed"}


def test_fail_import_parse_job_rejects_stale_lease_without_partial_write():
    """失败 writer 遇到 stale lease 时整笔不变，不能只改 job 或只改文件。"""
    conn = _RecordingConnection(
        responses=[
            (
                "SELECT job.*",
                _import_parse_job_row(
                    status="polling",
                    lease_token="lease-new",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="lease"):
        database.fail_import_parse_job(
            "parse_job_1",
            lease_token="lease-old",
            error="stale worker failed",
        )

    assert len(conn.calls) == 1


def test_begin_import_parse_job_finalization_keeps_current_lease():
    """MinerU 完成后必须显式进入 finalizing，并保留 lease 直到原子提交。"""
    current = _import_parse_job_row(
        status="polling",
        lease_token="lease-current",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )
    finalizing = _import_parse_job_row(
        status="finalizing",
        progress={"state": "completed", "percent": 100},
        lease_token="lease-current",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )
    conn = _RecordingConnection(
        responses=[
            ("FOR UPDATE OF job", current),
            ("UPDATE import_parse_jobs AS job", finalizing),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    result = database.begin_import_parse_job_finalization(
        "parse_job_1",
        lease_token="lease-current",
        progress={"state": "completed", "percent": 100},
    )

    assert result == finalizing
    update_sql, update_params = conn.calls[-1]
    assert "status = 'finalizing'" in update_sql
    assert "lease_token = NULL" not in update_sql
    assert "lease_expires_at = NULL" not in update_sql
    assert update_params["current_status"] == "polling"
    assert json.loads(update_params["progress"]) == {
        "state": "completed",
        "percent": 100,
    }


@pytest.mark.parametrize("current_status", ["queued", "submitting", "completed", "failed"])
def test_begin_import_parse_job_finalization_rejects_invalid_status(current_status):
    """finalizing 入口只接受 polling 或恢复领取后的 finalizing。"""
    conn = _RecordingConnection(
        responses=[
            (
                "FOR UPDATE OF job",
                _import_parse_job_row(
                    status=current_status,
                    lease_token="lease-current",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="finaliz"):
        database.begin_import_parse_job_finalization(
            "parse_job_1",
            lease_token="lease-current",
            progress={"state": "completed"},
        )

    assert len(conn.calls) == 1


def _replacement_import_chunk(**overrides):
    """构造原子解析终结使用的完整 current chunk。"""
    return {
        "id": "chunk_new_1",
        "file_id": "imp_1",
        "chunk_index": 0,
        "section_path": ["第一章"],
        "page_start": 1,
        "page_end": 1,
        "block_type": "text",
        "source_offsets": {"start": 0, "end": 4},
        "source_blocks": [],
        "children_delimiter": "",
        "start_at": None,
        "end_at": None,
        "message_count": 0,
        "keywords": [],
        "source_text": "新的正文",
        "status": "pending",
        "candidate_count": 0,
        **overrides,
    }


def test_complete_import_parse_job_replaces_source_and_completes_atomically():
    """终结任务必须在同一事务按锁序替换来源并最后清除 lease。"""
    current = _import_parse_job_row(
        status="finalizing",
        lease_token="lease-current",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )
    completed = _import_parse_job_row(
        status="completed",
        progress={"state": "completed", "percent": 100},
        lease_token=None,
        lease_expires_at=None,
    )
    chunk = _replacement_import_chunk()
    conn = _RecordingConnection(
        responses=[
            ("SELECT job.*", current),
            ("FOR UPDATE OF job", current),
            ("FROM import_files imp", {"id": "imp_1", "original_name": "手册.pdf"}),
            ("INSERT INTO import_chunks", chunk),
            ("UPDATE import_files AS imp", {"id": "imp_1", "status": "needs_review"}),
            ("UPDATE import_parse_jobs AS job", completed),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    result = database.complete_import_parse_job(
        "parse_job_1",
        lease_token="lease-current",
        input_fingerprint="sha256:document-v1",
        chunks=[chunk],
        progress={"state": "completed", "percent": 100},
    )

    assert result == completed
    job_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "FOR UPDATE OF job" in sql
    )
    file_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "FROM import_files imp" in sql and "FOR UPDATE OF imp" in sql
    )
    chunk_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "chunk.file_id = %(source_id)s" in sql and "FOR UPDATE OF chunk" in sql
    )
    evidence_delete_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "DELETE FROM kg_evidence" in sql
    )
    knowledge_delete_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "DELETE FROM knowledge_chunks" in sql
        and "source_id = %(file_id)s" in sql
    )
    chunk_delete_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "DELETE FROM import_chunks" in sql
    )
    chunk_insert_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "INSERT INTO import_chunks" in sql
    )
    file_update_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "UPDATE import_files AS imp" in sql
    )
    job_update_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "UPDATE import_parse_jobs AS job" in sql
    )
    assert (
        file_lock_index
        < chunk_lock_index
        < job_lock_index
        < evidence_delete_index
        < knowledge_delete_index
        < chunk_delete_index
        < chunk_insert_index
        < file_update_index
        < job_update_index
    )
    file_update_params = conn.calls[file_update_index][1]
    assert file_update_params == {
        "file_id": "imp_1",
        "status": "needs_review",
        "message_count": 0,
        "chunk_count": 1,
        "candidate_count": 0,
        "error": None,
    }
    job_update_sql, job_update_params = conn.calls[job_update_index]
    assert "status = 'completed'" in job_update_sql
    assert "lease_token = NULL" in job_update_sql
    assert "lease_expires_at = NULL" in job_update_sql
    assert job_update_params["lease_token"] == "lease-current"
    assert job_update_params["input_fingerprint"] == "sha256:document-v1"
    assert json.loads(job_update_params["progress"]) == {
        "state": "completed",
        "percent": 100,
    }


def test_complete_import_parse_job_accepts_submitting_markdown_completion():
    """本地 Markdown 可从 submitting 直接原子完成，不伪造 provider polling。"""
    current = _import_parse_job_row(
        status="submitting",
        lease_token="lease-current",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )
    completed = _import_parse_job_row(status="completed", progress={"percent": 100})
    conn = _RecordingConnection(
        responses=[
            ("SELECT job.*", current),
            ("FOR UPDATE OF job", current),
            ("FROM import_files imp", {"id": "imp_1"}),
            ("UPDATE import_files AS imp", {"id": "imp_1", "status": "needs_review"}),
            ("UPDATE import_parse_jobs AS job", completed),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    assert database.complete_import_parse_job(
        "parse_job_1",
        lease_token="lease-current",
        input_fingerprint="sha256:document-v1",
        chunks=[],
        progress={"percent": 100},
    ) == completed


@pytest.mark.parametrize("current_status", ["queued", "polling", "completed", "failed"])
def test_complete_import_parse_job_rejects_invalid_status_without_source_changes(
    current_status,
):
    """只有已领取的 submitting/finalizing 能提交，其他阶段不得碰来源数据。"""
    conn = _RecordingConnection(
        responses=[
            (
                "SELECT job.*",
                _import_parse_job_row(
                    status=current_status,
                    lease_token="lease-current",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="complete"):
        database.complete_import_parse_job(
            "parse_job_1",
            lease_token="lease-current",
            input_fingerprint="sha256:document-v1",
            chunks=[],
            progress={"percent": 100},
        )

    assert len(conn.calls) == 1


def test_complete_import_parse_job_rejects_stale_lease_before_source_changes():
    """旧 worker 的 lease 不得删除当前来源、KG 证据或向量。"""
    conn = _RecordingConnection(
        responses=[
            (
                "SELECT job.*",
                _import_parse_job_row(
                    status="finalizing",
                    lease_token="lease-new",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="lease"):
        database.complete_import_parse_job(
            "parse_job_1",
            lease_token="lease-old",
            input_fingerprint="sha256:document-v1",
            chunks=[],
            progress={"percent": 100},
        )

    assert len(conn.calls) == 1


def test_complete_import_parse_job_rechecks_lease_after_source_locks():
    """快检后 token 若被新 worker 替换，锁后复核必须阻止任何来源写入。"""
    preview = _import_parse_job_row(
        status="finalizing",
        lease_token="lease-old",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )
    reclaimed = _import_parse_job_row(
        status="finalizing",
        lease_token="lease-new",
        lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
    )

    def job_state(sql, _params):
        """无锁快检返回旧 token，FOR UPDATE 时返回已重领的新 token。"""
        return reclaimed if "FOR UPDATE OF job" in sql else preview

    conn = _RecordingConnection(
        responses=[
            ("SELECT job.*", job_state),
            ("FROM import_files imp", {"id": "imp_1"}),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="lease"):
        database.complete_import_parse_job(
            "parse_job_1",
            lease_token="lease-old",
            input_fingerprint="sha256:document-v1",
            chunks=[],
            progress={"percent": 100},
        )

    assert any("FOR UPDATE OF imp" in sql for sql, _params in conn.calls)
    assert any("FOR UPDATE OF job" in sql for sql, _params in conn.calls)
    assert not any("DELETE FROM kg_evidence" in sql for sql, _params in conn.calls)
    assert not any("DELETE FROM import_chunks" in sql for sql, _params in conn.calls)


def test_complete_import_parse_job_rejects_changed_fingerprint_before_source_changes():
    """输入文件变化后迟到结果必须失败，旧切片和审核快照保持不动。"""
    conn = _RecordingConnection(
        responses=[
            (
                "SELECT job.*",
                _import_parse_job_row(
                    status="finalizing",
                    lease_token="lease-current",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="fingerprint"):
        database.complete_import_parse_job(
            "parse_job_1",
            lease_token="lease-current",
            input_fingerprint="sha256:changed",
            chunks=[],
            progress={"percent": 100},
        )

    assert len(conn.calls) == 1


def test_complete_import_parse_job_rejects_cross_file_chunk_before_cleanup():
    """新切片必须属于任务文件，禁止一次解析覆盖其他来源。"""
    conn = _RecordingConnection(
        responses=[
            (
                "SELECT job.*",
                _import_parse_job_row(
                    status="finalizing",
                    lease_token="lease-current",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            )
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(ValueError, match="file_id"):
        database.complete_import_parse_job(
            "parse_job_1",
            lease_token="lease-current",
            input_fingerprint="sha256:document-v1",
            chunks=[_replacement_import_chunk(file_id="imp_other")],
            progress={"percent": 100},
        )

    assert len(conn.calls) == 1


def test_complete_import_parse_job_stops_before_file_and_job_completion_on_insert_error():
    """任一新切片写入失败时不得继续更新文件摘要或提交任务终态。"""

    def fail_insert(_sql, _params):
        """模拟同一事务中的新切片约束失败。"""
        raise RuntimeError("insert failed")

    conn = _RecordingConnection(
        responses=[
            (
                "SELECT job.*",
                _import_parse_job_row(
                    status="finalizing",
                    lease_token="lease-current",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            ),
            (
                "FOR UPDATE OF job",
                _import_parse_job_row(
                    status="finalizing",
                    lease_token="lease-current",
                    lease_expires_at=_PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
                ),
            ),
            ("FROM import_files imp", {"id": "imp_1"}),
            ("INSERT INTO import_chunks", fail_insert),
        ]
    )
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises(RuntimeError, match="insert failed"):
        database.complete_import_parse_job(
            "parse_job_1",
            lease_token="lease-current",
            input_fingerprint="sha256:document-v1",
            chunks=[_replacement_import_chunk()],
            progress={"percent": 100},
        )

    assert not any("UPDATE import_files AS imp" in sql for sql, _params in conn.calls)
    assert not any("UPDATE import_parse_jobs AS job" in sql for sql, _params in conn.calls)


@pytest.mark.parametrize(
    ("job_id", "lease_token", "input_fingerprint", "chunks", "progress", "message"),
    [
        ("", "lease", "sha256:document-v1", [], {}, "job_id"),
        ("parse_job_1", "", "sha256:document-v1", [], {}, "lease_token"),
        ("parse_job_1", "lease", "", [], {}, "input_fingerprint"),
        ("parse_job_1", "lease", "sha256:document-v1", None, {}, "chunks"),
        ("parse_job_1", "lease", "sha256:document-v1", [], [], "progress"),
    ],
)
def test_complete_import_parse_job_rejects_invalid_current_fields_before_sql(
    job_id,
    lease_token,
    input_fingerprint,
    chunks,
    progress,
    message,
):
    """终结入口只接受完整 current shape，非法输入不得启动事务写入。"""
    conn = _RecordingConnection()
    database = Database("postgresql://unused")
    database.connect = lambda: conn

    with pytest.raises((TypeError, ValueError), match=message):
        database.complete_import_parse_job(
            job_id,
            lease_token=lease_token,
            input_fingerprint=input_fingerprint,
            chunks=chunks,
            progress=progress,
        )

    assert conn.calls == []


def test_import_parse_jobs_have_no_generic_status_updater():
    """0→1 解析任务只保留显式 lifecycle 方法，不提供通用 status 写入口。"""
    assert not hasattr(Database, "update_import_parse_job_status")


def test_score_to_distance_converts_similarity_threshold():
    assert score_to_distance(0.35) == 0.65


def test_database_connect_uses_injected_pool_connection():
    """Database.connect 支持连接池上下文，避免 ASGI 问答并发下反复新建连接。"""
    calls = []

    class FakeConnectionContext:
        def __enter__(self):
            calls.append("enter")
            return "pooled-connection"

        def __exit__(self, exc_type, exc, tb):
            calls.append("exit")
            return False

    class FakePool:
        def connection(self):
            calls.append("connection")
            return FakeConnectionContext()

    db = Database("postgresql://unused", pool=FakePool())

    with db.connect() as conn:
        assert conn == "pooled-connection"

    assert calls == ["connection", "enter", "exit"]


def test_database_close_closes_pool_once():
    """Database.close 应释放连接池，避免 ASGI lifespan 结束后留下池资源。"""
    calls = []

    class FakePool:
        def close(self):
            calls.append("close")

    db = Database("postgresql://unused", pool=FakePool())

    db.close()
    db.close()

    assert calls == ["close"]
    assert db.pool is None


def test_build_import_candidate_faq_row_defaults_to_needs_review():
    candidate = {
        "id": "cand_1",
        "question": "报告没生成怎么办？",
        "answer": "建议隔 10 分钟刷新查看进度。",
        "similar_questions": ["团体报告下载不了怎么办？"],
        "category": "报告服务",
        "tags": ["报告", "生成中"],
        "confidence": "medium",
        "source_excerpt": "客服 09:16: 隔10分钟刷新一次页面查看进度",
        "file_name": "chat.md",
        "chunk_id": "chunk_1",
    }

    row = build_import_candidate_faq_row(candidate)

    assert row["id"].startswith("faq_cand_1")
    assert row["status"] == "needs_review"
    assert row["question_variants"] == ["团体报告下载不了怎么办？"]
    assert row["evidence"] == [
        {
            "source_file": "chat.md",
            "chunk_id": "chunk_1",
            "excerpt": "客服 09:16: 隔10分钟刷新一次页面查看进度",
        }
    ]


def test_insert_import_candidate_sql_includes_duplicate_fields():
    """候选 FAQ 入库时需要保存查重结果字段。"""
    sql = Database._insert_import_candidate_sql()

    assert "duplicate_level" in sql
    assert "duplicate_score" in sql
    assert "duplicate_target_id" in sql
    assert "duplicate_reason" in sql


def test_build_faq_knowledge_chunk_row_uses_unified_retrieval_shape():
    """FAQ 应能映射为统一知识单元，后续和文档切片共用检索表。"""
    faq = {
        "id": "faq_1",
        "question": "报告没生成怎么办？",
        "question_variants": ["团体报告下载不了怎么办？"],
        "answer": "建议隔 10 分钟刷新查看进度。",
        "category": "报告服务",
        "tags": ["报告", "生成中"],
        "confidence": "high",
        "status": "usable",
        "source_file": "chat.md",
        "source_group": "import_review",
        "evidence": [{"chunk_id": "chunk_1", "excerpt": "隔10分钟刷新"}],
    }

    chunk = build_faq_knowledge_chunk_row(faq)

    assert chunk["source_type"] == "faq"
    assert chunk["source_id"] == "faq_1"
    assert chunk["source_chunk_id"] is None
    assert chunk["chunk_index"] == 0
    assert chunk["source_title"] == "报告没生成怎么办？"
    assert "问题：报告没生成怎么办？" in chunk["content"]
    assert "答案：建议隔 10 分钟刷新查看进度。" in chunk["content"]
    assert chunk["embedding_text"] == build_embedding_text(faq)
    assert "报告服务" in chunk["search_text"]
    assert "生成中" in chunk["search_text"]
    assert chunk["metadata"]["evidence"] == faq["evidence"]
    assert chunk["status"] == "usable"
    assert chunk["confidence"] == "high"


def test_build_document_knowledge_chunk_row_adds_contextual_embedding_text():
    """文档切片映射为知识单元时，embedding_text 应补充来源上下文。"""
    import_file = {
        "id": "file_1",
        "original_name": "平台使用手册.pdf",
        "file_type": "pdf",
        "parser": "mineru",
    }
    import_chunk = {
        "id": "chunk_3",
        "file_id": "file_1",
        "chunk_index": 3,
        "source_text": "用户无法登录时，先检查账号状态，再重置密码。",
        "keywords": ["登录", "密码"],
        "status": "generated",
        "message_count": 0,
        "start_at": None,
        "end_at": None,
        "section_path": ["账号管理", "登录问题"],
        "page_start": 2,
        "page_end": 3,
        "block_type": "paragraph",
        "parent_chunk_id": "parent_1",
        "chunk_level": "child",
        "source_offsets": {"start": 10, "end": 48},
    }

    chunk = build_document_knowledge_chunk_row(
        import_chunk,
        import_file,
        knowledge_chunk_id="chunk_3_child_2",
    )

    assert chunk["id"] == "kc_document_chunk_3_child_2"
    assert chunk["source_type"] == "document"
    assert chunk["source_id"] == "file_1"
    assert chunk["source_chunk_id"] == "chunk_3"
    assert chunk["chunk_index"] == 3
    assert chunk["source_title"] == "平台使用手册.pdf"
    assert chunk["content"] == "用户无法登录时，先检查账号状态，再重置密码。"
    assert "文件：平台使用手册.pdf" in chunk["embedding_text"]
    assert "章节：账号管理 > 登录问题" in chunk["embedding_text"]
    assert "页码：2-3" in chunk["embedding_text"]
    assert "块类型：paragraph" in chunk["embedding_text"]
    assert "关键词：登录，密码" in chunk["embedding_text"]
    assert "正文：用户无法登录时，先检查账号状态，再重置密码。" in chunk["embedding_text"]
    assert "平台使用手册.pdf" in chunk["search_text"]
    assert "账号管理 > 登录问题" in chunk["search_text"]
    assert "登录" in chunk["search_text"]
    assert chunk["tags"] == ["登录", "密码"]
    assert chunk["metadata"]["file_type"] == "pdf"
    assert chunk["metadata"]["parser"] == "mineru"
    assert chunk["metadata"]["section_path"] == ["账号管理", "登录问题"]
    assert chunk["metadata"]["page_start"] == 2
    assert chunk["metadata"]["page_end"] == 3
    assert chunk["metadata"]["chunk_id"] == "chunk_3"
    assert chunk["parent_chunk_id"] == "parent_1"
    assert chunk["chunk_level"] == "child"
    assert chunk["section_path"] == ["账号管理", "登录问题"]
    assert chunk["page_start"] == 2
    assert chunk["page_end"] == 3
    assert chunk["block_type"] == "paragraph"
    assert chunk["source_offsets"] == {"start": 10, "end": 48}
    assert chunk["status"] == "needs_review"


def test_build_document_knowledge_chunk_row_requires_import_file():
    """文档知识行必须显式绑定 import_file，不允许从 chunk 静默补来源。"""
    with pytest.raises(TypeError):
        build_document_knowledge_chunk_row(
            {"id": "chunk_1", "file_id": "file_1", "source_text": "正文"},
            knowledge_chunk_id="chunk_1",
        )


def test_build_document_knowledge_chunk_row_rejects_mismatched_file_identity():
    """chunk.file_id 与 import_file.id 不一致时必须失败，不能任选一个来源 ID。"""
    with pytest.raises(ValueError, match="file_id"):
        build_document_knowledge_chunk_row(
            {"id": "chunk_1", "file_id": "file_1", "source_text": "正文"},
            {"id": "file_other", "original_name": "other.pdf"},
            knowledge_chunk_id="chunk_1",
        )


def test_build_document_knowledge_chunk_row_requires_parent_or_child_level():
    """文档知识行只允许 parent/child 两种形状，不保留默认 chunk 第三形态。"""
    with pytest.raises(ValueError, match="chunk_level"):
        build_document_knowledge_chunk_row(
            {"id": "chunk_1", "file_id": "file_1", "source_text": "正文"},
            {"id": "file_1", "original_name": "manual.pdf"},
            knowledge_chunk_id="chunk_1",
        )


def _document_embedding_commit_fixture():
    """构造一个文件、来源切片及其 parent/child，供原子提交测试复用。"""
    import_file = {
        "id": "imp_1",
        "original_name": "manual.pdf",
        "file_type": "pdf",
        "parser": "mineru",
        "chunker_type": "naive",
        "status": "needs_review",
        "is_disabled": False,
    }
    source_chunk = {
        "id": "chunk_1",
        "file_id": "imp_1",
        "chunk_index": 1,
        "source_text": "报告导出失败时先检查权限。",
        "keywords": ["报告", "权限"],
        "status": "generated",
        "message_count": 1,
        "start_at": None,
        "end_at": None,
        "section_path": ["报告"],
        "page_start": 2,
        "page_end": 2,
        "block_type": "paragraph",
        "source_offsets": {},
        "source_blocks": [],
        "children_delimiter": "",
        "questions": ["为什么不能导出报告？"],
        "is_disabled": False,
    }
    parent = build_document_knowledge_chunk_row(
        {**source_chunk, "chunk_level": "parent", "retrieval_status": "usable"},
        import_file,
        knowledge_chunk_id="chunk_1",
    )
    child = build_document_knowledge_chunk_row(
        {
            **source_chunk,
            "source_text": source_chunk["source_text"],
            "chunk_index": child_knowledge_chunk_index(source_chunk["chunk_index"], 1),
            "parent_chunk_id": parent["id"],
            "chunk_level": "child",
            "retrieval_status": "usable",
        },
        import_file,
        knowledge_chunk_id="chunk_1_child_1",
    )
    return import_file, source_chunk, [parent, child]


def test_replace_document_chunk_embeddings_locks_source_and_commits_batch_atomically():
    """文档向量必须在一个 file→chunk 锁事务内替换该来源的全部 parent/child。"""
    from cyclops.db import builders as builders_module

    assert hasattr(builders_module, "document_embedding_source_fingerprint")
    assert hasattr(Database, "replace_document_chunk_embeddings")
    import_file, source_chunk, rows = _document_embedding_commit_fixture()
    fingerprint = builders_module.document_embedding_source_fingerprint(
        import_file,
        source_chunk,
    )
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", source_chunk),
            ("INSERT INTO knowledge_chunks", lambda _sql, params: dict(params)),
        ]
    )
    connect_count = 0

    def connect():
        """记录事务次数，整个来源切片批次只能打开一个连接上下文。"""
        nonlocal connect_count
        connect_count += 1
        return conn

    db = Database("postgresql://unused")
    db.connect = connect

    result = db.replace_document_chunk_embeddings(
        file_id="imp_1",
        chunk_id="chunk_1",
        source_fingerprint=fingerprint,
        items=[(rows[0], [0.1, 0.2]), (rows[1], [0.3, 0.4])],
        embedding_model="embedding-current",
        embedding_dimensions=2,
    )

    file_lock_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "FOR UPDATE OF imp" in sql
    )
    chunk_lock_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "FOR UPDATE OF chunk" in sql
    )
    delete_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "DELETE FROM knowledge_chunks" in sql
    )
    insert_calls = [
        (index, sql, params)
        for index, (sql, params) in enumerate(conn.calls)
        if "INSERT INTO knowledge_chunks" in sql
    ]
    assert connect_count == 1
    assert file_lock_index < chunk_lock_index < delete_index < insert_calls[0][0]
    assert len(insert_calls) == 2
    assert len(result) == 2
    assert [params["source_chunk_id"] for _index, _sql, params in insert_calls] == [
        "chunk_1",
        "chunk_1",
    ]
    delete_sql, delete_params = conn.calls[delete_index]
    assert "source_type = 'document'" in delete_sql
    assert "source_id = %(file_id)s" in delete_sql
    assert "source_chunk_id = %(chunk_id)s" in delete_sql
    assert delete_params == {"file_id": "imp_1", "chunk_id": "chunk_1"}


def test_replace_document_chunk_embeddings_rejects_incomplete_live_child_batch():
    """锁内实时来源需要两个 child 时，parent 加单 child 的残缺批次不得覆盖完整投影。"""
    from cyclops.db import builders as builders_module

    import_file, source_chunk, rows = _document_embedding_commit_fixture()
    source_chunk = {
        **source_chunk,
        "source_blocks": [
            {"text": "结构块一", "block_type": "text"},
            {"text": "结构块二", "block_type": "text"},
        ],
    }
    fingerprint = builders_module.document_embedding_source_fingerprint(
        import_file,
        source_chunk,
    )
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", source_chunk),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="deterministic source rows"):
        db.replace_document_chunk_embeddings(
            file_id="imp_1",
            chunk_id="chunk_1",
            source_fingerprint=fingerprint,
            items=[(rows[0], [0.1, 0.2]), (rows[1], [0.3, 0.4])],
            embedding_model="embedding-current",
            embedding_dimensions=2,
        )

    assert not any("DELETE FROM knowledge_chunks" in sql for sql, _params in conn.calls)


def test_replace_document_chunk_embeddings_rejects_nondeterministic_row_ids():
    """文档批次必须使用来源切片派生的固定 parent/child ID，不接受自洽伪造 ID。"""
    from cyclops.db import builders as builders_module

    import_file, source_chunk, rows = _document_embedding_commit_fixture()
    fingerprint = builders_module.document_embedding_source_fingerprint(
        import_file,
        source_chunk,
    )
    wrong_parent_id = "kc_document_wrong_parent"
    wrong_rows = [
        {**rows[0], "id": wrong_parent_id},
        {
            **rows[1],
            "id": "kc_document_wrong_child",
            "parent_chunk_id": wrong_parent_id,
        },
    ]
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", source_chunk),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="deterministic source rows"):
        db.replace_document_chunk_embeddings(
            file_id="imp_1",
            chunk_id="chunk_1",
            source_fingerprint=fingerprint,
            items=[(wrong_rows[0], [0.1, 0.2]), (wrong_rows[1], [0.3, 0.4])],
            embedding_model="embedding-current",
            embedding_dimensions=2,
        )

    assert not any("DELETE FROM knowledge_chunks" in sql for sql, _params in conn.calls)


@pytest.mark.parametrize(
    ("file_override", "chunk_override", "missing_row"),
    [
        ({}, {}, "file"),
        ({}, {}, "chunk"),
        ({"is_disabled": True}, {}, None),
        ({}, {"is_disabled": True}, None),
        ({}, {"source_text": "并发修改后的正文"}, None),
    ],
    ids=["deleted-file", "reparsed-chunk", "disabled-file", "disabled-chunk", "changed"],
)
def test_replace_document_chunk_embeddings_rejects_late_or_unavailable_source(
    file_override,
    chunk_override,
    missing_row,
):
    """删除、重解析、禁用或内容变化后，迟到向量不得删除或重建任何知识行。"""
    from cyclops.db import builders as builders_module

    assert hasattr(builders_module, "document_embedding_source_fingerprint")
    assert hasattr(Database, "replace_document_chunk_embeddings")
    import_file, source_chunk, rows = _document_embedding_commit_fixture()
    fingerprint = builders_module.document_embedding_source_fingerprint(
        import_file,
        source_chunk,
    )
    locked_file = None if missing_row == "file" else {**import_file, **file_override}
    locked_chunk = None if missing_row == "chunk" else {**source_chunk, **chunk_override}
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF imp", locked_file),
            ("FOR UPDATE OF chunk", locked_chunk),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="changed|unavailable"):
        db.replace_document_chunk_embeddings(
            file_id="imp_1",
            chunk_id="chunk_1",
            source_fingerprint=fingerprint,
            items=[(rows[0], [0.1, 0.2]), (rows[1], [0.3, 0.4])],
            embedding_model="embedding-current",
            embedding_dimensions=2,
        )

    assert not any("DELETE FROM knowledge_chunks" in sql for sql, _params in conn.calls)
    assert not any("INSERT INTO knowledge_chunks" in sql for sql, _params in conn.calls)


def test_replace_document_chunk_embeddings_propagates_partial_insert_failure():
    """批次任一 child 写入失败必须抛出，由同一事务回滚 delete 和先前 insert。"""
    from cyclops.db import builders as builders_module

    assert hasattr(builders_module, "document_embedding_source_fingerprint")
    assert hasattr(Database, "replace_document_chunk_embeddings")
    import_file, source_chunk, rows = _document_embedding_commit_fixture()
    fingerprint = builders_module.document_embedding_source_fingerprint(
        import_file,
        source_chunk,
    )
    insert_count = 0

    def insert_or_fail(_sql, params):
        """首行返回、第二行抛错，模拟 child 约束失败。"""
        nonlocal insert_count
        insert_count += 1
        if insert_count == 2:
            raise RuntimeError("child insert failed")
        return dict(params)

    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", source_chunk),
            ("INSERT INTO knowledge_chunks", insert_or_fail),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(RuntimeError, match="child insert failed"):
        db.replace_document_chunk_embeddings(
            file_id="imp_1",
            chunk_id="chunk_1",
            source_fingerprint=fingerprint,
            items=[(rows[0], [0.1, 0.2]), (rows[1], [0.3, 0.4])],
            embedding_model="embedding-current",
            embedding_dimensions=2,
        )

    assert insert_count == 2


def test_knowledge_chunks_schema_supports_vector_and_keyword_retrieval():
    """统一知识单元表需要同时预留向量检索和全文检索能力。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS knowledge_chunks" in schema
    assert "source_type TEXT NOT NULL" in schema
    assert "source_id TEXT NOT NULL" in schema
    assert "source_chunk_id TEXT" in schema
    assert "parent_chunk_id TEXT" in schema
    assert "chunk_level TEXT NOT NULL DEFAULT 'chunk'" in schema
    assert "section_path JSONB NOT NULL DEFAULT '[]'::jsonb" in schema
    assert "page_start INTEGER" in schema
    assert "page_end INTEGER" in schema
    assert "block_type TEXT" in schema
    assert "source_offsets JSONB NOT NULL DEFAULT '{}'::jsonb" in schema
    assert "content TEXT NOT NULL" in schema
    assert "embedding vector(1024)" in schema
    assert "metadata JSONB NOT NULL DEFAULT '{}'::jsonb" in schema
    assert "UNIQUE (source_type, source_id, chunk_index)" in schema
    assert "knowledge_chunks_parent_idx" in schema
    assert "knowledge_chunks_section_path_idx" in schema
    assert "knowledge_chunks_embedding_idx" in schema
    assert "knowledge_chunks_search_idx" in schema
    assert "to_tsvector('simple', search_text)" in schema


def test_kg_schema_supports_reviewable_entities_relations_and_evidence():
    """KG schema 需要独立保存实体、关系和证据，确认后再投影到 knowledge_chunks。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS kg_entities" in schema
    assert "entity_type TEXT NOT NULL" in schema
    assert "aliases JSONB NOT NULL DEFAULT '[]'::jsonb" in schema
    assert "status TEXT NOT NULL DEFAULT 'needs_review'" in schema
    assert "CREATE TABLE IF NOT EXISTS kg_relations" in schema
    assert "head_entity_id TEXT NOT NULL REFERENCES kg_entities(id)" in schema
    assert "tail_entity_id TEXT NOT NULL REFERENCES kg_entities(id)" in schema
    assert "relation_type TEXT NOT NULL" in schema
    assert "CREATE TABLE IF NOT EXISTS kg_evidence" in schema
    assert "source_type TEXT NOT NULL" in schema
    assert "source_chunk_id TEXT" in schema
    assert "excerpt TEXT NOT NULL" in schema
    assert "kg_entities_status_type_idx" in schema
    assert "kg_relations_status_type_idx" in schema
    assert "kg_evidence_source_idx" in schema


def test_document_kg_schema_has_durable_parent_items_and_exact_evidence_offsets():
    """当前 schema 只保存 FAQ/文档父任务、不可变 item manifest 和精确证据。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    normalized = " ".join(schema.split())
    job_table = schema.split("CREATE TABLE IF NOT EXISTS kg_extraction_jobs (", 1)[1].split(
        ");",
        1,
    )[0]
    assert "CREATE TABLE IF NOT EXISTS kg_extraction_job_items (" in schema
    item_table = schema.split(
        "CREATE TABLE IF NOT EXISTS kg_extraction_job_items (",
        1,
    )[1].split(");", 1)[0]
    normalized_job = " ".join(job_table.split())
    normalized_item = " ".join(item_table.split())

    assert schema.count("CREATE TABLE IF NOT EXISTS kg_extraction_jobs (") == 1
    assert "source_type TEXT NOT NULL CHECK (source_type IN ('faq', 'document'))" in normalized_job
    assert "source_id TEXT NOT NULL" in job_table
    assert "source_chunk_id" not in job_table
    assert "document_chunk" not in job_table
    assert (
        "phase IN ('queued', 'mapping', 'resolving', 'reducing', 'completed', 'failed')"
        in normalized_job
    )
    assert "processed_chunks INTEGER NOT NULL DEFAULT 0" in job_table
    assert "total_chunks INTEGER NOT NULL" in job_table
    assert "source_fingerprint TEXT NOT NULL" in job_table
    assert "resolution_result JSONB" in job_table
    assert "jsonb_typeof(resolution_result) = 'object'" in normalized_job
    assert "processed_chunks <= total_chunks" in normalized_job
    assert "source_type = 'document' OR phase NOT IN ('resolving', 'reducing')" in normalized_job
    assert "kg_extraction_jobs_lease_pair_check" in job_table
    assert "(lease_token IS NULL) = (lease_expires_at IS NULL)" in normalized_job
    assert "error TEXT CHECK (error IS NULL OR char_length(error) <= 1000)" in normalized_job

    assert "job_id TEXT NOT NULL REFERENCES kg_extraction_jobs(id) ON DELETE CASCADE" in normalized_item
    assert "chunk_id TEXT NOT NULL" in item_table
    assert "chunk_id TEXT NOT NULL REFERENCES" not in normalized_item
    assert "chunk_order INTEGER NOT NULL CHECK (chunk_order >= 0)" in normalized_item
    assert "section_path JSONB NOT NULL" in item_table
    assert "map_result JSONB" in item_table
    assert "jsonb_typeof(map_result) = 'object'" in normalized_item
    assert "UNIQUE (job_id, chunk_id)" in normalized_item
    assert "UNIQUE (job_id, chunk_order)" in normalized_item

    assert "kg_extraction_jobs_one_active_source_idx" in schema
    assert "WHERE phase IN ('queued', 'mapping', 'resolving', 'reducing')" in normalized
    assert "kg_extraction_jobs_claim_due_idx" in schema
    assert "kg_extraction_jobs_source_history_idx" in schema

    evidence_table = schema.split("CREATE TABLE IF NOT EXISTS kg_evidence (", 1)[1].split(
        ");",
        1,
    )[0]
    normalized_evidence = " ".join(evidence_table.split())
    assert "char_start INTEGER NOT NULL" in evidence_table
    assert "char_end INTEGER NOT NULL" in evidence_table
    assert "kg_evidence_char_offsets_check" in evidence_table
    assert "char_start >= 0 AND char_end > char_start" in normalized_evidence
    assert "kg_evidence_source_locator_check" in evidence_table
    assert "source_type = 'faq' AND source_chunk_id IS NULL" in normalized_evidence
    assert "source_type = 'document' AND source_chunk_id IS NOT NULL" in normalized_evidence

    marker = "20260715_document_kg_pipeline_v1"
    assert schema.count(marker) == 2
    reset_index = schema.index("DO $document_kg_pipeline$")
    assert reset_index > schema.rindex("UPDATE retrieval_eval_cases AS eval_case")
    assert reset_index < schema.index(
        "CREATE TABLE IF NOT EXISTS kg_extraction_jobs ("
    )
    reset_block = schema.split("DO $document_kg_pipeline$", 1)[1].split(
        "$document_kg_pipeline$;",
        1,
    )[0]
    assert "DELETE FROM knowledge_chunks" in reset_block
    assert "DELETE FROM kg_evidence" in reset_block
    assert "DELETE FROM kg_relations" in reset_block
    assert "DELETE FROM kg_entities" in reset_block
    assert "DROP TABLE IF EXISTS kg_extraction_job_items" in reset_block
    assert "DROP TABLE IF EXISTS kg_extraction_jobs" in reset_block


def _kg_public_job_row(**overrides):
    """构造唯一公开 KG job DTO，测试不为内部字段提供兼容默认值。"""
    return {
        "id": "kg_job_1",
        "source_type": "document",
        "source_id": "imp_1",
        "phase": "queued",
        "processed_chunks": 0,
        "total_chunks": 2,
        "entity_count": 0,
        "relation_count": 0,
        "evidence_count": 0,
        "model": "mimo-v2.5-pro",
        "error": None,
        "created_at": _PARSE_JOB_NOW,
        "updated_at": _PARSE_JOB_NOW,
        **overrides,
    }


def _kg_internal_job_row(**overrides):
    """构造 worker 内部父任务行，lease 与 staging 字段不得进入公开 DTO。"""
    return {
        **_kg_public_job_row(phase="mapping"),
        "source_fingerprint": "sha256:document-manifest",
        "resolution_result": None,
        "lease_token": "lease_1",
        "lease_expires_at": _PARSE_JOB_LIVE_LEASE_EXPIRES_AT,
        "next_attempt_at": _PARSE_JOB_NOW,
        "attempt_count": 1,
        "lease_is_current": True,
        **overrides,
    }


def _document_kg_file(**overrides):
    """构造可排队文档；needs_review 是解析完成后的当前审核态。"""
    return {
        "id": "imp_1",
        "original_name": "平台手册.pdf",
        "status": "needs_review",
        "is_disabled": False,
        **overrides,
    }


def _document_kg_chunk(**overrides):
    """构造包含完整 manifest locator 的启用文档切片。"""
    return {
        "id": "chunk_1",
        "file_id": "imp_1",
        "chunk_index": 0,
        "source_text": "报告导出前需要账号权限。",
        "section_path": ["报表", "导出"],
        "page_start": 2,
        "page_end": 2,
        "is_disabled": False,
        **overrides,
    }


def _document_kg_item_row(import_file=None, chunk=None, **overrides):
    """按当前文件与切片生成 frozen item，fingerprint 覆盖全部来源 locator。"""
    resolved_file = import_file or _document_kg_file()
    resolved_chunk = chunk or _document_kg_chunk()
    guard = _kg_source_guard(
        resolved_chunk["source_text"],
        source_type="document",
        source_id=resolved_file["id"],
        source_chunk_id=resolved_chunk["id"],
        source_title=resolved_file["original_name"],
        section_path=resolved_chunk["section_path"],
        page_start=resolved_chunk["page_start"],
        page_end=resolved_chunk["page_end"],
    )
    return {
        "id": "kg_item_1",
        "job_id": "kg_job_1",
        "chunk_id": resolved_chunk["id"],
        "chunk_order": 0,
        "source_fingerprint": guard["fingerprint"],
        "section_path": list(resolved_chunk["section_path"]),
        "page_start": resolved_chunk["page_start"],
        "page_end": resolved_chunk["page_end"],
        "phase": "mapping",
        "map_result": None,
        "error": None,
        **overrides,
    }


def _localized_document_map_result(item=None, **overrides):
    """构造已绑定 manifest item 的最小 Map staging JSON。"""
    resolved_item = item or _document_kg_item_row()
    return {
        "chunk_id": resolved_item["chunk_id"],
        "chunk_order": resolved_item["chunk_order"],
        "entities": [],
        "relations": [],
        **overrides,
    }


def _document_kg_completion_state():
    """构造 final completion 使用的当前文件、完整 manifest、父任务和 items。"""
    import_file = _document_kg_file()
    chunks = [
        _document_kg_chunk(),
        _document_kg_chunk(
            id="chunk_2",
            chunk_index=1,
            source_text="失败后联系技术支持。",
            section_path=["报表", "故障处理"],
            page_start=3,
            page_end=3,
        ),
    ]
    manifest = build_document_kg_manifest(import_file, chunks)
    items = [
        {
            "id": f"kg_item_{index + 1}",
            "job_id": "kg_job_1",
            "chunk_id": manifest_item["chunk_id"],
            "chunk_order": manifest_item["chunk_order"],
            "source_fingerprint": manifest_item["source_fingerprint"],
            "section_path": list(manifest_item["section_path"]),
            "page_start": manifest_item["page_start"],
            "page_end": manifest_item["page_end"],
            "phase": "mapped",
            "map_result": {
                "chunk_id": manifest_item["chunk_id"],
                "chunk_order": manifest_item["chunk_order"],
                "entities": [],
                "relations": [],
            },
            "error": None,
        }
        for index, manifest_item in enumerate(manifest["items"])
    ]
    job = _kg_internal_job_row(
        phase="reducing",
        processed_chunks=len(items),
        total_chunks=len(items),
        source_fingerprint=manifest["fingerprint"],
        resolution_result={"groups": []},
    )
    return {
        "file": import_file,
        "chunks": chunks,
        "manifest": manifest,
        "items": items,
        "job": job,
    }


def _document_kg_source(import_file, chunk):
    """从当前文件和切片构造 evidence 必须精确匹配的文档 locator。"""
    return {
        "source_type": "document",
        "source_id": import_file["id"],
        "source_chunk_id": chunk["id"],
        "source_title": import_file["original_name"],
        "section_path": list(chunk["section_path"]),
        "page_start": chunk["page_start"],
        "page_end": chunk["page_end"],
    }


def _kg_evidence_from_text(source_text, source, excerpt):
    """按 Python Unicode code-point 下标构造一条精确原文证据。"""
    char_start = source_text.index(excerpt)
    return {
        **source,
        "excerpt": excerpt,
        "char_start": char_start,
        "char_end": char_start + len(excerpt),
    }


def _document_kg_reduced_extraction(state):
    """构造跨两个 manifest items 的完整 Reduce 结果，包含实体、关系和 offsets。"""
    import_file = state["file"]
    first_chunk, second_chunk = state["chunks"]
    first_source = _document_kg_source(import_file, first_chunk)
    second_source = _document_kg_source(import_file, second_chunk)
    export_evidence = _kg_evidence_from_text(
        first_chunk["source_text"],
        first_source,
        "报告导出",
    )
    permission_evidence = _kg_evidence_from_text(
        first_chunk["source_text"],
        first_source,
        "账号权限",
    )
    support_evidence = _kg_evidence_from_text(
        second_chunk["source_text"],
        second_source,
        "技术支持",
    )
    relation_evidence = _kg_evidence_from_text(
        first_chunk["source_text"],
        first_source,
        "需要账号权限",
    )
    return {
        "entities": [
            {
                "id": "kg_ent_export",
                "name": "报告导出",
                "entity_type": "feature_ui_action",
                "aliases": [],
                "description": "报告导出功能",
                "status": "needs_review",
                "confidence": 0.91,
                "evidence": [export_evidence],
            },
            {
                "id": "kg_ent_permission",
                "name": "账号权限",
                "entity_type": "role_permission_channel",
                "aliases": [],
                "description": "导出所需权限",
                "status": "needs_review",
                "confidence": 0.88,
                "evidence": [permission_evidence],
            },
            {
                "id": "kg_ent_support",
                "name": "技术支持",
                "entity_type": "role_permission_channel",
                "aliases": [],
                "description": "故障升级渠道",
                "status": "needs_review",
                "confidence": 0.83,
                "evidence": [support_evidence],
            },
        ],
        "relations": [
            {
                "id": "kg_rel_export_requires_permission",
                "head_entity_id": "kg_ent_export",
                "head_entity_name": "报告导出",
                "head_entity_type": "feature_ui_action",
                "relation_type": "requires",
                "tail_entity_id": "kg_ent_permission",
                "tail_entity_name": "账号权限",
                "tail_entity_type": "role_permission_channel",
                "description": "报告导出需要账号权限",
                "status": "needs_review",
                "confidence": 0.86,
                "evidence": [relation_evidence],
            }
        ],
    }


def _document_kg_completion_connection(
    state,
    *,
    preview_job=None,
    locked_job=None,
    items=None,
    completed=None,
    extra_responses=None,
):
    """为 final completion 路由无锁快检、来源锁、父任务锁和 item 锁响应。"""
    responses = list(extra_responses or [])
    if completed is not None:
        responses.append(("SET phase = 'completed'", completed))
    responses.extend(
        [
            ("FOR UPDATE OF item", items if items is not None else state["items"]),
            ("FOR UPDATE OF job", locked_job if locked_job is not None else state["job"]),
            ("FOR UPDATE OF imp", state["file"]),
            ("FOR UPDATE OF chunk", state["chunks"]),
            (
                "WHERE job.id = %(job_id)s",
                preview_job if preview_job is not None else state["job"],
            ),
        ]
    )
    return _RecordingConnection(responses)


def _faq_kg_completion_state():
    """构造 FAQ 单来源 job、原文、精确 evidence 和最终公开结果。"""
    faq = {
        "id": "faq_1",
        "question": "报告怎么导出？",
        "answer": "先检查账号权限。",
        "category": "报表",
        "tags": ["导出"],
        "status": "usable",
    }
    source_text = build_faq_kg_source_text(faq)
    source = {
        "source_type": "faq",
        "source_id": "faq_1",
        "source_chunk_id": None,
        "source_title": faq["question"],
        "section_path": [],
        "page_start": None,
        "page_end": None,
    }
    evidence = _kg_evidence_from_text(source_text, source, "账号权限")
    guard = build_kg_source_guard(source_text=source_text, source=source)
    job = _kg_internal_job_row(
        source_type="faq",
        source_id="faq_1",
        phase="mapping",
        processed_chunks=0,
        total_chunks=1,
        source_fingerprint=guard["fingerprint"],
        resolution_result=None,
    )
    extraction = {
        "entities": [
            {
                "id": "kg_ent_permission",
                "name": "账号权限",
                "entity_type": "role_permission_channel",
                "aliases": [],
                "description": "导出前需要检查的权限",
                "status": "needs_review",
                "confidence": 0.9,
                "evidence": [evidence],
            }
        ],
        "relations": [],
    }
    completed = _kg_public_job_row(
        source_type="faq",
        source_id="faq_1",
        phase="completed",
        processed_chunks=1,
        total_chunks=1,
        entity_count=1,
        relation_count=0,
        evidence_count=1,
    )
    return {
        "faq": faq,
        "source_text": source_text,
        "source": source,
        "job": job,
        "extraction": extraction,
        "completed": completed,
    }


def _assert_no_kg_snapshot_writes(conn):
    """断言 fence 失败前未触碰 canonical owner、evidence 或完成状态。"""
    forbidden_markers = (
        "INSERT INTO kg_entities",
        "INSERT INTO kg_relations",
        "DELETE FROM kg_evidence",
        "INSERT INTO kg_evidence",
        "UPDATE kg_entities entity",
        "UPDATE kg_relations relation",
        "SET phase = 'completed'",
    )
    assert not any(
        marker in sql
        for sql, _params in conn.calls
        for marker in forbidden_markers
    )


def _kg_call_index(conn, marker):
    """返回指定 SQL 片段首次调用位置，缺失时让顺序断言直接失败。"""
    for index, (sql, _params) in enumerate(conn.calls):
        if marker in sql:
            return index
    raise AssertionError(f"SQL call not found: {marker}")


def _return_recorded_params(_sql, params):
    """让 INSERT fake 回显参数，供测试记录同事务内的 manifest item 写入。"""
    return dict(params)


def test_kg_extraction_job_has_only_explicit_current_lifecycle_entrypoints():
    """0→1 durable job 必须删除 generic create/start/complete 生命周期入口。"""
    assert not hasattr(Database, "create_kg_extraction_job")
    assert not hasattr(Database, "start_kg_extraction_job")
    assert not hasattr(Database, "complete_kg_extraction_job")
    assert not hasattr(Database, "update_kg_extraction_job")


def test_create_document_kg_job_locks_file_and_manifest_items_in_one_transaction():
    """排队必须按 file→chunk ID 锁序创建父任务，并按 manifest 顺序写全部 item。"""
    import_file = _document_kg_file()
    chunks = [
        _document_kg_chunk(id="chunk_a", chunk_index=4, section_path=["附录"]),
        _document_kg_chunk(
            id="chunk_b",
            chunk_index=1,
            source_text="先检查账号权限。",
            section_path=["导出"],
            page_start=1,
            page_end=1,
        ),
        _document_kg_chunk(id="chunk_disabled", chunk_index=0, is_disabled=True),
    ]
    public_job = _kg_public_job_row(total_chunks=2)
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", chunks),
            ("job.phase IN ('queued', 'mapping', 'resolving', 'reducing')", None),
            ("INSERT INTO kg_extraction_jobs", public_job),
            ("INSERT INTO kg_extraction_job_items", _return_recorded_params),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.create_document_kg_extraction_job(
        "imp_1",
        model="mimo-v2.5-pro",
    )

    assert result == public_job
    file_lock = _kg_call_index(conn, "FOR UPDATE OF imp")
    chunk_lock = _kg_call_index(conn, "FOR UPDATE OF chunk")
    parent_insert = _kg_call_index(conn, "INSERT INTO kg_extraction_jobs")
    item_insert = _kg_call_index(conn, "INSERT INTO kg_extraction_job_items")
    assert file_lock < chunk_lock < parent_insert < item_insert
    assert "ORDER BY chunk.id ASC" in conn.calls[chunk_lock][0]
    item_calls = [
        params
        for sql, params in conn.calls
        if "INSERT INTO kg_extraction_job_items" in sql
    ]
    assert [params["chunk_id"] for params in item_calls] == ["chunk_b", "chunk_a"]
    assert [params["chunk_order"] for params in item_calls] == [0, 1]
    assert [json.loads(params["section_path"]) for params in item_calls] == [
        ["导出"],
        ["附录"],
    ]
    parent_params = conn.calls[parent_insert][1]
    assert parent_params["source_type"] == "document"
    assert parent_params["source_id"] == "imp_1"
    assert parent_params["total_chunks"] == 2
    assert parent_params["source_fingerprint"]
    assert all(params["job_id"] == parent_params["id"] for params in item_calls)


def test_create_faq_kg_job_has_one_logical_chunk_and_no_item_rows():
    """FAQ 保持独立单来源契约，但复用 total_chunks=1 的 durable parent lifecycle。"""
    faq = {
        "id": "faq_1",
        "question": "报告怎么导出？",
        "answer": "先检查账号权限。",
        "category": "报表",
        "tags": ["导出"],
        "status": "usable",
    }
    public_job = _kg_public_job_row(
        source_type="faq",
        source_id="faq_1",
        total_chunks=1,
    )
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF faq", faq),
            ("job.phase IN ('queued', 'mapping', 'resolving', 'reducing')", None),
            ("INSERT INTO kg_extraction_jobs", public_job),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.create_faq_kg_extraction_job("faq_1", model="mimo-v2.5-pro")

    assert result == public_job
    parent_params = next(
        params for sql, params in conn.calls if "INSERT INTO kg_extraction_jobs" in sql
    )
    assert parent_params["source_type"] == "faq"
    assert parent_params["source_id"] == "faq_1"
    assert parent_params["total_chunks"] == 1
    assert parent_params["source_fingerprint"]
    assert not any("kg_extraction_job_items" in sql for sql, _params in conn.calls)


@pytest.mark.parametrize(
    ("import_file", "chunks", "error_type", "message"),
    [
        (None, [], KeyError, "Import file not found"),
        (_document_kg_file(is_disabled=True), [], ValueError, "disabled import file"),
        (_document_kg_file(status="processing"), [], ValueError, "parsed review state"),
        (_document_kg_file(), [], ValueError, "at least one enabled chunk"),
        (
            _document_kg_file(),
            [_document_kg_chunk(source_text="   ")],
            ValueError,
            "source_text is required",
        ),
    ],
)
def test_create_document_kg_job_rejects_invalid_source_before_insert(
    import_file,
    chunks,
    error_type,
    message,
):
    """缺失、禁用、未完成解析或无有效正文的文档都不能创建父任务。"""
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", chunks),
            ("job.phase IN ('queued', 'mapping', 'resolving', 'reducing')", None),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(error_type, match=message):
        db.create_document_kg_extraction_job("imp_1", model="mimo-v2.5-pro")

    assert not any("INSERT INTO kg_extraction_jobs" in sql for sql, _params in conn.calls)
    assert not any(
        "INSERT INTO kg_extraction_job_items" in sql for sql, _params in conn.calls
    )


@pytest.mark.parametrize(
    ("faq", "error_type", "message"),
    [
        (None, KeyError, "FAQ not found"),
        (
            {
                "id": "faq_1",
                "question": "问题",
                "answer": "答案",
                "status": "needs_review",
            },
            ValueError,
            "usable",
        ),
        (
            {
                "id": "faq_1",
                "question": "问题",
                "answer": "答案",
                "status": "disabled",
            },
            ValueError,
            "usable",
        ),
    ],
)
def test_create_faq_kg_job_rejects_missing_or_nonusable_source_before_insert(
    faq,
    error_type,
    message,
):
    """FAQ 不存在或未进入 usable 时必须在 queued INSERT 前明确失败。"""
    conn = _RecordingConnection([("FOR UPDATE OF faq", faq)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(error_type, match=message):
        db.create_faq_kg_extraction_job("faq_1", model="mimo-v2.5-pro")

    assert not any("INSERT INTO kg_extraction_jobs" in sql for sql, _params in conn.calls)


@pytest.mark.parametrize("source_type", ["faq", "document"])
def test_create_kg_jobs_reject_active_source_conflict_without_reusing_job(source_type):
    """同一来源已有 active generation 时必须报冲突，不能回传或复用旧 job ID。"""
    active = {"id": "kg_job_active"}
    responses = [
        ("job.phase IN ('queued', 'mapping', 'resolving', 'reducing')", active),
    ]
    if source_type == "faq":
        responses.insert(
            0,
            (
                "FOR UPDATE OF faq",
                {
                    "id": "faq_1",
                    "question": "问题",
                    "answer": "答案",
                    "status": "usable",
                },
            ),
        )
    else:
        responses[0:0] = [
            ("FOR UPDATE OF imp", _document_kg_file()),
            ("FOR UPDATE OF chunk", [_document_kg_chunk()]),
        ]
    conn = _RecordingConnection(responses)
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="kg_job_active"):
        if source_type == "faq":
            db.create_faq_kg_extraction_job("faq_1", model="mimo-v2.5-pro")
        else:
            db.create_document_kg_extraction_job("imp_1", model="mimo-v2.5-pro")

    assert not any("INSERT INTO kg_extraction_jobs" in sql for sql, _params in conn.calls)


def test_kg_job_public_reads_never_select_staging_or_lease_fields():
    """job GET/latest 只查询 wire 字段，manifest、staging、lease 和 attempt 均不外露。"""
    forbidden = {
        "source_fingerprint",
        "resolution_result",
        "lease_token",
        "lease_expires_at",
        "next_attempt_at",
        "attempt_count",
        "map_result",
    }
    required = set(_kg_public_job_row())
    for sql in (
        Database._get_kg_extraction_job_sql(),
        Database._latest_kg_extraction_job_sql(),
    ):
        select_clause = sql.split("FROM kg_extraction_jobs", 1)[0]
        assert "SELECT *" not in select_clause
        assert not forbidden.intersection(select_clause)
        assert all(f"job.{field}" in select_clause for field in required)
    latest_sql = Database._latest_kg_extraction_job_sql()
    assert "ORDER BY job.created_at DESC, job.id DESC" in latest_sql


def test_get_and_latest_kg_job_return_exact_public_dto():
    """按 ID 和 owner 读取返回同一公开 DTO，latest 只接受 faq/document owner。"""
    stored = _kg_public_job_row(phase="mapping", processed_chunks=1)
    conn = _RecordingConnection(
        [
            ("WHERE job.id = %(job_id)s", stored),
            ("job.source_type = %(source_type)s", stored),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    assert db.get_kg_extraction_job("kg_job_1") == stored
    assert db.get_latest_kg_extraction_job(
        source_type="document",
        source_id="imp_1",
    ) == stored
    assert conn.calls[0][1] == {"job_id": "kg_job_1"}
    assert conn.calls[1][1] == {
        "source_type": "document",
        "source_id": "imp_1",
    }
    with pytest.raises(ValueError, match="faq or document"):
        db.get_latest_kg_extraction_job(
            source_type="document_chunk",
            source_id="chunk_1",
        )


def test_claim_kg_job_uses_skip_locked_and_expired_lease():
    """多 worker 只能领取未租用或 lease 已过期的 due active job。"""
    sql = " ".join(Database._claim_kg_extraction_job_sql().split())
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "job.next_attempt_at <= now()" in sql
    assert "job.lease_token IS NULL OR job.lease_expires_at < now()" in sql
    assert "job.phase IN ('queued', 'mapping', 'resolving', 'reducing')" in sql
    assert "WHEN job.phase = 'queued' THEN 'mapping'" in sql
    assert "attempt_count = job.attempt_count + 1" in sql
    claimed = _kg_internal_job_row()
    conn = _RecordingConnection([("WITH due_job AS", claimed)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    assert db.claim_kg_extraction_job(lease_seconds=180) == claimed
    params = conn.calls[0][1]
    assert params["lease_seconds"] == 180
    assert isinstance(params["lease_token"], str) and params["lease_token"]


def test_load_document_kg_map_item_revalidates_manifest_before_mapping():
    """Map 前必须按 file→chunk→job→item 复核 frozen locator，再标记 item。"""
    import_file = _document_kg_file()
    chunk = _document_kg_chunk()
    item = _document_kg_item_row(import_file, chunk, phase="queued")
    job = _kg_internal_job_row()
    conn = _RecordingConnection(
        [
            ("SELECT job.*", job),
            ("SELECT item.*", item),
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", chunk),
            ("SET phase = 'mapping'", {**item, "phase": "mapping"}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.load_document_kg_map_item("kg_job_1", lease_token="lease_1")

    assert result["source_text"] == chunk["source_text"]
    assert result["source"] == {
        "source_type": "document",
        "source_id": "imp_1",
        "source_chunk_id": "chunk_1",
        "source_title": "平台手册.pdf",
        "section_path": ["报表", "导出"],
        "page_start": 2,
        "page_end": 2,
    }
    file_lock = _kg_call_index(conn, "FOR UPDATE OF imp")
    chunk_lock = _kg_call_index(conn, "FOR UPDATE OF chunk")
    job_lock = _kg_call_index(conn, "FOR UPDATE OF job")
    item_lock = _kg_call_index(conn, "FOR UPDATE OF item")
    mark_mapping = _kg_call_index(conn, "SET phase = 'mapping'")
    assert file_lock < chunk_lock < job_lock < item_lock < mark_mapping


@pytest.mark.parametrize(
    ("job_overrides", "lease_token"),
    [
        ({"lease_token": "lease_other"}, "lease_1"),
        ({"lease_is_current": False}, "lease_1"),
    ],
)
def test_load_document_kg_map_item_rejects_wrong_or_expired_lease_before_locks(
    job_overrides,
    lease_token,
):
    """错误或过期 lease 在无锁快检即失败，不能取得来源锁或改 item phase。"""
    conn = _RecordingConnection(
        [("SELECT job.*", _kg_internal_job_row(**job_overrides))]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="lease"):
        db.load_document_kg_map_item("kg_job_1", lease_token=lease_token)

    assert not any("FOR UPDATE OF imp" in sql for sql, _params in conn.calls)
    assert not any("SET phase = 'mapping'" in sql for sql, _params in conn.calls)


@pytest.mark.parametrize(
    ("processed_chunks", "expected_processed", "expected_phase"),
    [(0, 1, "mapping"), (1, 2, "resolving")],
)
def test_complete_map_item_revalidates_source_and_advances_progress_atomically(
    processed_chunks,
    expected_processed,
    expected_phase,
):
    """Map staging 只能在 lease/manifest 匹配后写入，并只推进一次父任务计数。"""
    import_file = _document_kg_file()
    chunk = _document_kg_chunk()
    item = _document_kg_item_row(import_file, chunk)
    job = _kg_internal_job_row(processed_chunks=processed_chunks)
    public_result = _kg_public_job_row(
        phase=expected_phase,
        processed_chunks=expected_processed,
    )
    map_result = _localized_document_map_result(item)
    conn = _RecordingConnection(
        [
            ("SELECT job.*", job),
            ("SELECT item.*", item),
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", chunk),
            ("SET phase = 'mapped'", {"id": "kg_item_1"}),
            ("SET processed_chunks", public_result),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.complete_document_kg_map_item(
        "kg_job_1",
        "kg_item_1",
        lease_token="lease_1",
        map_result=map_result,
    )

    assert result == public_result
    source_lock = _kg_call_index(conn, "FOR UPDATE OF chunk")
    item_lock = _kg_call_index(conn, "FOR UPDATE OF item")
    stage_update = _kg_call_index(conn, "SET phase = 'mapped'")
    progress_update = _kg_call_index(conn, "SET processed_chunks")
    assert source_lock < item_lock < stage_update < progress_update
    assert json.loads(conn.calls[stage_update][1]["map_result"]) == map_result
    progress_sql, progress_params = conn.calls[progress_update]
    assert "job.processed_chunks < job.total_chunks" in progress_sql
    assert "job.lease_token = %(lease_token)s" in progress_sql
    assert "job.lease_expires_at > now()" in progress_sql
    assert "lease_token = NULL" in progress_sql
    assert progress_params["lease_token"] == "lease_1"


def test_complete_map_item_rejects_already_mapped_item_without_increment():
    """已 mapped item 不得二次写 staging 或重复增加父任务进度。"""
    import_file = _document_kg_file()
    chunk = _document_kg_chunk()
    item = _document_kg_item_row(
        import_file,
        chunk,
        phase="mapped",
        map_result=_localized_document_map_result(),
    )
    job = _kg_internal_job_row(processed_chunks=1)
    conn = _RecordingConnection(
        [
            ("SELECT job.*", job),
            ("SELECT item.*", item),
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", chunk),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="must be mapping"):
        db.complete_document_kg_map_item(
            "kg_job_1",
            "kg_item_1",
            lease_token="lease_1",
            map_result=_localized_document_map_result(item),
        )

    assert not any("SET phase = 'mapped'" in sql for sql, _params in conn.calls)
    assert not any("SET processed_chunks" in sql for sql, _params in conn.calls)


@pytest.mark.parametrize(
    "changed_locator",
    ["file_title", "source_text", "section_path", "page_start", "page_end", "disabled"],
)
def test_complete_map_item_rejects_manifest_change_before_staging(changed_locator):
    """模型调用期间任一 frozen locator 变化都必须挡在 staging 与进度写入之前。"""
    original_file = _document_kg_file()
    original_chunk = _document_kg_chunk()
    item = _document_kg_item_row(original_file, original_chunk)
    current_file = dict(original_file)
    current_chunk = dict(original_chunk)
    if changed_locator == "file_title":
        current_file["original_name"] = "新标题.pdf"
    elif changed_locator == "source_text":
        current_chunk["source_text"] = "正文已被重新解析。"
    elif changed_locator == "section_path":
        current_chunk["section_path"] = ["新章节"]
    elif changed_locator == "page_start":
        current_chunk["page_start"] = 3
    elif changed_locator == "page_end":
        current_chunk["page_end"] = 3
    else:
        current_chunk["is_disabled"] = True
    job = _kg_internal_job_row()
    conn = _RecordingConnection(
        [
            ("SELECT job.*", job),
            ("SELECT item.*", item),
            ("FOR UPDATE OF imp", current_file),
            ("FOR UPDATE OF chunk", current_chunk),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="manifest|unavailable"):
        db.complete_document_kg_map_item(
            "kg_job_1",
            "kg_item_1",
            lease_token="lease_1",
            map_result=_localized_document_map_result(item),
        )

    assert not any("SET phase = 'mapped'" in sql for sql, _params in conn.calls)
    assert not any("SET processed_chunks" in sql for sql, _params in conn.calls)


@pytest.mark.parametrize(
    "map_result",
    [
        _localized_document_map_result(chunk_id="chunk_other"),
        _localized_document_map_result(chunk_order=9),
    ],
)
def test_complete_map_item_requires_result_to_match_manifest_item(map_result):
    """Map JSON 的 chunk_id/order 必须属于当前 item，不能写入另一片的结果。"""
    import_file = _document_kg_file()
    chunk = _document_kg_chunk()
    item = _document_kg_item_row(import_file, chunk)
    job = _kg_internal_job_row()
    conn = _RecordingConnection(
        [
            ("SELECT job.*", job),
            ("SELECT item.*", item),
            ("FOR UPDATE OF imp", import_file),
            ("FOR UPDATE OF chunk", chunk),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="Map result.*manifest item"):
        db.complete_document_kg_map_item(
            "kg_job_1",
            "kg_item_1",
            lease_token="lease_1",
            map_result=map_result,
        )

    assert not any("SET phase = 'mapped'" in sql for sql, _params in conn.calls)
    assert not any("SET processed_chunks" in sql for sql, _params in conn.calls)


def test_load_document_kg_map_results_reads_complete_manifest_order():
    """Resolve/Reduce 只能按 immutable chunk_order 读取所有 mapped JSON。"""
    first = _localized_document_map_result()
    second = _localized_document_map_result(chunk_id="chunk_2", chunk_order=1)
    job = _kg_internal_job_row(
        phase="resolving",
        processed_chunks=2,
        total_chunks=2,
    )
    conn = _RecordingConnection(
        [
            ("SELECT job.*", job),
            (
                "SELECT item.phase, item.map_result",
                [
                    {"phase": "mapped", "map_result": first},
                    {"phase": "mapped", "map_result": second},
                ],
            ),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    assert db.load_document_kg_map_results(
        "kg_job_1",
        lease_token="lease_1",
    ) == [first, second]
    results_sql = next(
        sql for sql, _params in conn.calls if "SELECT item.phase, item.map_result" in sql
    )
    assert "ORDER BY item.chunk_order ASC, item.id ASC" in results_sql


@pytest.mark.parametrize(
    "rows",
    [
        [{"phase": "mapped", "map_result": _localized_document_map_result()}],
        [
            {"phase": "mapped", "map_result": _localized_document_map_result()},
            {"phase": "mapping", "map_result": None},
        ],
    ],
)
def test_load_document_kg_map_results_rejects_incomplete_staging(rows):
    """item 数量不足或存在非 mapped 行时不能把不完整文档交给 resolution。"""
    job = _kg_internal_job_row(
        phase="resolving",
        processed_chunks=2,
        total_chunks=2,
    )
    conn = _RecordingConnection(
        [
            ("SELECT job.*", job),
            ("SELECT item.phase, item.map_result", rows),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="every mapped item|fully mapped"):
        db.load_document_kg_map_results("kg_job_1", lease_token="lease_1")


def test_save_document_kg_resolution_is_lease_fenced_and_enters_reducing():
    """resolution object 只能由当前 resolving lease 保存，并原子清租约进入 reducing。"""
    reduced = _kg_public_job_row(
        phase="reducing",
        processed_chunks=2,
        total_chunks=2,
    )
    resolution = {"groups": [["local_a", "local_b"]]}
    conn = _RecordingConnection([("SET resolution_result", reduced)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.save_document_kg_resolution(
        "kg_job_1",
        lease_token="lease_1",
        resolution_result=resolution,
    )

    assert result == reduced
    sql, params = conn.calls[0]
    assert "job.source_type = 'document'" in sql
    assert "job.phase = 'resolving'" in sql
    assert "job.processed_chunks = job.total_chunks" in sql
    assert "job.lease_token = %(lease_token)s" in sql
    assert "job.lease_expires_at > now()" in sql
    assert "phase = 'reducing'" in sql
    assert "lease_token = NULL" in sql and "lease_expires_at = NULL" in sql
    assert json.loads(params["resolution_result"]) == resolution


def test_save_document_kg_resolution_rejects_replay_and_nonobject_staging():
    """已进入 reducing 的 resolution 不能重放覆盖，非 object 不能执行 SQL。"""
    save_count = 0

    def first_save_only(_sql, _params):
        """仅第一次模拟 resolving CAS 成功，第二次代表 phase 已离开。"""
        nonlocal save_count
        save_count += 1
        if save_count == 1:
            return _kg_public_job_row(
                phase="reducing",
                processed_chunks=2,
                total_chunks=2,
            )
        return None

    conn = _RecordingConnection([("SET resolution_result", first_save_only)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn
    resolution = {"groups": []}

    db.save_document_kg_resolution(
        "kg_job_1",
        lease_token="lease_1",
        resolution_result=resolution,
    )
    with pytest.raises(ValueError, match="lease expired"):
        db.save_document_kg_resolution(
            "kg_job_1",
            lease_token="lease_1",
            resolution_result=resolution,
        )
    sql_calls_before_invalid_type = len(conn.calls)
    with pytest.raises(TypeError, match="JSON object"):
        db.save_document_kg_resolution(
            "kg_job_1",
            lease_token="lease_1",
            resolution_result=[],
        )
    assert len(conn.calls) == sql_calls_before_invalid_type


def test_search_knowledge_sql_excludes_kg_projection_by_default():
    """默认客服检索不能读取 KG 投影，KG 只通过显式调试/评测开关进入候选。"""
    sql = Database._search_knowledge_sql()

    assert "kc.source_type IN ('faq', 'document')" in sql


def test_search_knowledge_text_sql_excludes_kg_projection_by_default():
    """默认关键词召回同样不能直接读取 KG 投影。"""
    sql = Database._search_knowledge_text_sql()

    assert "kc.source_type IN ('faq', 'document')" in sql


def test_search_kg_knowledge_text_sql_reads_only_confirmed_kg_projection():
    """KG 关键词召回必须固定只读 usable fact，不暴露可切换状态参数。"""
    sql = Database._search_kg_knowledge_text_sql()

    assert "FROM knowledge_chunks kc" in sql
    assert "kc.source_type IN ('kg_entity', 'kg_relation')" in sql
    assert "kc.status = 'usable'" in sql
    assert "%(status)s" not in sql
    assert "kg_entity" in sql
    assert "kg_relation" in sql


def test_search_kg_knowledge_text_returns_ranked_fact_hits():
    """KG 搜索结果只描述合成 fact，不把合成投影伪装成最终知识候选。"""
    row = {
        "id": "kc_kg_relation_1",
        "source_type": "kg_relation",
        "source_id": "kg_rel_1",
        "source_chunk_id": None,
        "parent_chunk_id": None,
        "chunk_level": "chunk",
        "source_title": "报告 requires 权限",
        "section_path": [],
        "page_start": None,
        "page_end": None,
        "block_type": "kg_relation",
        "source_offsets": {},
        "content": "报告导出需要权限",
        "metadata": {},
        "tags": [],
        "confidence": "high",
        "status": "usable",
        "score": 0.77,
    }
    conn = _RecordingConnection([("FROM knowledge_chunks kc", [row])])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    hits = db.search_kg_knowledge_text("报告导出", top_k=5, query_terms=["报告"])

    assert len(hits) == 1
    assert hits[0].fact_chunk_id == "kc_kg_relation_1"
    assert hits[0].fact_id == "kg_rel_1"
    assert hits[0].fact_type == "kg_relation"
    assert hits[0].fact_rank == 1
    assert hits[0].fact_score == 0.77
    assert "status" not in conn.calls[0][1]


def test_expand_kg_fact_hits_groups_original_candidates_and_deduplicates_matches():
    """同一原始知识行可由多个 fact 扩召回，但每个 fact/candidate 组合只保留一次。"""
    rows = [
        {
            "id": "kc_document_chunk_1_child_1",
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
            "parent_chunk_id": "kc_document_chunk_1",
            "chunk_level": "child",
            "source_title": "manual.pdf",
            "section_path": ["报告"],
            "page_start": 1,
            "page_end": 1,
            "block_type": "text",
            "source_offsets": {},
            "content": "导出报告前检查权限。",
            "metadata": {"chunk_id": "chunk_1"},
            "tags": [],
            "confidence": None,
            "status": "usable",
            "score": 0.77,
            "fact_chunk_id": "kc_kg_relation_1",
            "fact_id": "kg_rel_1",
            "fact_type": "kg_relation",
            "fact_rank": 1,
            "fact_score": 0.77,
        },
        {
            "id": "kc_document_chunk_1_child_1",
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
            "parent_chunk_id": "kc_document_chunk_1",
            "chunk_level": "child",
            "source_title": "manual.pdf",
            "section_path": ["报告"],
            "page_start": 1,
            "page_end": 1,
            "block_type": "text",
            "source_offsets": {},
            "content": "导出报告前检查权限。",
            "metadata": {"chunk_id": "chunk_1"},
            "tags": [],
            "confidence": None,
            "status": "usable",
            "score": 0.64,
            "fact_chunk_id": "kc_kg_entity_2",
            "fact_id": "kg_ent_2",
            "fact_type": "kg_entity",
            "fact_rank": 2,
            "fact_score": 0.64,
        },
        {
            "id": "kc_document_chunk_1_child_1",
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
            "parent_chunk_id": "kc_document_chunk_1",
            "chunk_level": "child",
            "source_title": "manual.pdf",
            "section_path": ["报告"],
            "page_start": 1,
            "page_end": 1,
            "block_type": "text",
            "source_offsets": {},
            "content": "导出报告前检查权限。",
            "metadata": {"chunk_id": "chunk_1"},
            "tags": [],
            "confidence": None,
            "status": "usable",
            "score": 0.77,
            "fact_chunk_id": "kc_kg_relation_1",
            "fact_id": "kg_rel_1",
            "fact_type": "kg_relation",
            "fact_rank": 1,
            "fact_score": 0.77,
        },
    ]
    conn = _RecordingConnection([("WITH fact_hits AS", rows)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn
    fact_hits = [
        KgFactHit(
            fact_chunk_id="kc_kg_relation_1",
            fact_id="kg_rel_1",
            fact_type="kg_relation",
            fact_rank=1,
            fact_score=0.77,
        ),
        KgFactHit(
            fact_chunk_id="kc_kg_entity_2",
            fact_id="kg_ent_2",
            fact_type="kg_entity",
            fact_rank=2,
            fact_score=0.64,
        ),
    ]

    candidates = db.expand_kg_fact_hits(fact_hits)

    assert len(candidates) == 1
    assert candidates[0].document.id == "kc_document_chunk_1_child_1"
    assert candidates[0].document.source_chunk_id == "chunk_1"
    assert [match.fact_chunk_id for match in candidates[0].kg_matches] == [
        "kc_kg_relation_1",
        "kc_kg_entity_2",
    ]
    assert conn.calls[0][1]["fact_chunk_ids"] == [
        "kc_kg_relation_1",
        "kc_kg_entity_2",
    ]


@pytest.mark.parametrize(
    "invalid_hit",
    [
        object(),
        {},
        SimpleNamespace(
            fact_chunk_id="kc_kg_entity_1",
            fact_id="kg_ent_1",
            fact_type="kg_entity",
            fact_rank=1,
            fact_score=0.8,
        ),
    ],
)
def test_expand_kg_fact_hits_rejects_noncanonical_models(invalid_hit):
    """KG 展开只接受 KgFactHit，不允许 dict 或字段齐全的 duck object 兼容形状。"""
    db = Database("postgresql://unused")
    db.connect = lambda: pytest.fail("invalid KG fact must be rejected before database access")

    with pytest.raises(TypeError, match="KgFactHit"):
        db.expand_kg_fact_hits([invalid_hit])


def test_expand_kg_fact_hits_sql_requires_exact_live_original_evidence():
    """KG 展开只能 inner join 实时 FAQ/direct child，不允许 parent、metadata 或 ID 前缀兜底。"""
    sql = Database._expand_kg_fact_hits_sql()
    normalized_sql = " ".join(sql.split())

    assert "JOIN kg_evidence" in sql
    assert "JOIN faq_documents faq" in sql
    assert "faq.status = 'usable'" in sql
    assert "faq.embedding_status = 'ready'" in sql
    assert "original.source_chunk_id IS NULL" in sql
    assert "original.chunk_level = 'chunk'" in sql
    assert "JOIN import_files import_file" in sql
    assert "JOIN import_chunks import_chunk" in sql
    assert "import_file.is_disabled = false" in sql
    assert "import_chunk.is_disabled = false" in sql
    assert "original.source_chunk_id = evidence.source_chunk_id" in sql
    assert "original.chunk_level = 'child'" in sql
    assert "original.embedding_status = 'ready'" in sql
    assert "original.status = 'usable'" in sql
    assert "original.chunk_level = 'parent'" not in normalized_sql
    assert "JOIN knowledge_chunks parent" not in normalized_sql
    assert "metadata->" not in sql
    assert "kc_document_" not in sql


def test_list_kg_entities_sql_includes_evidence_and_filters():
    """KG 实体审核列表需要带证据摘要，并支持状态/类型筛选。"""
    sql = Database._list_kg_entities_sql(where="WHERE ent.status = %(status)s AND ent.entity_type = %(entity_type)s")

    assert "FROM kg_entities ent" in sql
    assert "LEFT JOIN kg_evidence ev ON ev.entity_id = ent.id" in sql
    assert "jsonb_agg" in sql
    assert "ent.status = %(status)s" in sql
    assert "ent.entity_type = %(entity_type)s" in sql
    assert "LIMIT %(limit)s OFFSET %(offset)s" in sql


@pytest.mark.parametrize(
    ("sql", "history_join"),
    [
        (
            Database._list_kg_entities_sql(),
            "LEFT JOIN kg_evidence ev ON ev.entity_id = ent.id",
        ),
        (
            Database._list_kg_relations_sql(),
            "LEFT JOIN kg_evidence ev ON ev.relation_id = rel.id",
        ),
    ],
)
def test_kg_review_lists_preserve_history_and_mark_each_evidence_validity(
    sql,
    history_join,
):
    """审核列表保留全部历史证据，并为每条证据返回必填实时有效标记。"""
    normalized_sql = " ".join(sql.split())

    assert history_join in normalized_sql
    assert "FILTER (WHERE ev.id IS NOT NULL)" in normalized_sql
    assert re.search(r"'is_valid'\s*,\s*EXISTS\s*\(", sql)
    assert re.search(r"WHERE\s+valid_ev\.id\s*=\s*ev\.id\b", sql)


def test_list_kg_entities_returns_authoritative_database_source_count():
    """实体列表必须透传 ent.source_count，不能按历史 evidence 数组重新派生。"""
    conn = _RecordingConnection(
        [
            (
                "GROUP BY ent.id",
                [
                    {
                        "id": "kg_ent_1",
                        "source_count": 1,
                        "evidence": [
                            {"id": "ev_old_1", "is_valid": True},
                            {"id": "ev_old_2", "is_valid": False},
                        ],
                    }
                ],
            ),
            ("SELECT count(*) AS total", {"total": 1}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.list_kg_entities()

    assert result["items"][0]["source_count"] == 1
    rows_sql = " ".join(conn.calls[0][0].split())
    select_sql = rows_sql.split("FROM kg_entities ent", 1)[0]
    assert "ent.*" in select_sql
    assert not re.search(r"\bAS\s+source_count\b", select_sql, flags=re.IGNORECASE)
    assert "jsonb_array_length" not in select_sql


@pytest.mark.parametrize(
    ("sql", "owner_condition"),
    [
        (Database._list_kg_entities_sql(), "valid_ev.entity_id = ent.id"),
        (Database._list_kg_relations_sql(), "valid_ev.relation_id = rel.id"),
    ],
)
def test_kg_review_lists_expose_mandatory_live_evidence_gate(sql, owner_condition):
    """实体和关系列表必须返回实时证据门禁，不能把保留的历史 evidence 当作仍有效。"""
    assert "AS has_valid_evidence" in sql
    assert "FROM kg_evidence valid_ev" in sql
    assert owner_condition in sql
    assert "valid_ev.source_type = 'faq'" in sql
    assert "faq.id = valid_ev.source_id" in sql
    assert "faq.status = 'usable'" in sql
    assert "valid_ev.source_type = 'document'" in sql
    assert "imp.id = valid_ev.source_id" in sql
    assert "chunk.id = valid_ev.source_chunk_id" in sql
    assert "chunk.file_id = imp.id" in sql
    assert "imp.id IS NOT NULL" in sql
    assert "chunk.id IS NOT NULL" in sql
    assert "imp.is_disabled = false" in sql
    assert "chunk.is_disabled = false" in sql


def test_list_kg_relations_returns_live_evidence_count():
    """关系列表 evidence_count 只统计通过唯一实时来源门禁的证据。"""
    sql = Database._list_kg_relations_sql()
    normalized_sql = " ".join(sql.split())

    assert "count(DISTINCT valid_ev.id)::integer" in normalized_sql
    assert "valid_ev.relation_id = rel.id" in normalized_sql
    assert re.search(r"\)\s+AS\s+evidence_count\b", sql, flags=re.IGNORECASE)
    assert "jsonb_array_length" not in normalized_sql


@pytest.mark.parametrize(
    ("sql_builder", "expected_gate_count"),
    [
        (Database._list_kg_entities_sql, 2),
        (Database._list_kg_relations_sql, 3),
        (Database._kg_subgraph_sql, 1),
    ],
)
def test_kg_review_reads_reuse_the_single_live_evidence_predicate(
    monkeypatch,
    sql_builder,
    expected_gate_count,
):
    """列表标记、实时计数和子图计数必须调用同一个证据门禁生成器。"""
    import cyclops.db.kg as kg_db

    sentinel = "kg_live_evidence_gate_sentinel"

    def sentinel_predicate():
        """返回不可与真实 SQL 混淆的门禁标记，验证所有读路径调用同一 helper。"""
        return sentinel

    monkeypatch.setattr(kg_db, "_kg_live_evidence_predicate_sql", sentinel_predicate)

    assert sql_builder().count(sentinel) == expected_gate_count


def test_list_kg_relations_sql_includes_head_tail_and_evidence():
    """KG 关系审核列表需要带头尾实体名称和证据摘要。"""
    sql = Database._list_kg_relations_sql(where="WHERE rel.status = %(status)s")

    assert "FROM kg_relations rel" in sql
    assert "JOIN kg_entities head ON head.id = rel.head_entity_id" in sql
    assert "JOIN kg_entities tail ON tail.id = rel.tail_entity_id" in sql
    assert "LEFT JOIN kg_evidence ev ON ev.relation_id = rel.id" in sql
    assert "head.name AS head_entity_name" in sql
    assert "head.status AS head_entity_status" in sql
    assert "tail.name AS tail_entity_name" in sql
    assert "tail.status AS tail_entity_status" in sql
    assert "jsonb_agg" in sql


def test_set_kg_entity_status_updates_projection_chunk():
    """禁用或待复核 KG 实体时，应同步更新 kg_entity 投影状态。"""
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF ent", {"id": "kg_ent_abc"}),
            ("ORDER BY rel.id ASC", []),
            ("UPDATE kg_entities", {"id": "kg_ent_abc", "status": "disabled"}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    row = db.set_kg_entity_status("kg_ent_abc", "disabled")

    assert row["status"] == "disabled"
    assert any("UPDATE kg_entities" in sql for sql, _params in conn.calls)
    projection_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE knowledge_chunks" in sql and "%(source_type)s" in sql
    ]
    assert projection_calls
    assert projection_calls[0][1] == {"id": "kg_ent_abc", "status": "disabled", "source_type": "kg_entity"}


def test_set_kg_relation_status_updates_projection_chunk():
    """禁用或待复核 KG 关系时，应同步更新 kg_relation 投影状态。"""
    sql = Database._set_kg_relation_status_sql()
    projection_sql = Database._set_kg_projection_status_sql()

    assert "UPDATE kg_relations" in sql
    assert "status = %(status)s" in sql
    assert "JOIN kg_entities head" in sql
    assert "JOIN kg_entities tail" in sql
    assert "head_entity_name" in sql
    assert "tail_entity_name" in sql
    assert "UPDATE knowledge_chunks" in projection_sql
    assert "source_type = %(source_type)s" in projection_sql


def test_insert_knowledge_chunk_sql_uses_single_upsert_shape():
    """统一知识单元写入 SQL 应覆盖来源、内容、检索文本和 embedding 状态。"""
    sql = Database._insert_knowledge_chunk_sql()

    assert "INSERT INTO knowledge_chunks" in sql
    assert "source_type" in sql
    assert "source_id" in sql
    assert "source_chunk_id" in sql
    assert "parent_chunk_id" in sql
    assert "chunk_level" in sql
    assert "section_path" in sql
    assert "page_start" in sql
    assert "page_end" in sql
    assert "block_type" in sql
    assert "source_offsets" in sql
    assert "embedding_text" in sql
    assert "search_text" in sql
    assert "ON CONFLICT (source_type, source_id, chunk_index)" in sql


def test_confirm_kg_entity_projects_usable_chunk_in_same_connection():
    """确认 KG 实体时应更新状态并写入 kg_entity 知识单元投影。"""
    entity = {
        "id": "kg_ent_abc",
        "name": "报告导出",
        "entity_type": "feature_ui_action",
        "aliases": ["导出报告"],
        "description": "后台导出团体报告的功能入口。",
        "confidence": 0.86,
        "status": "usable",
        "review_revision": 1,
    }
    evidence = {
        "source_type": "document",
        "source_id": "imp_1",
        "source_chunk_id": "chunk_1",
        "excerpt": "检查账号权限",
    }
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF ent", entity),
            ("AS has_valid_evidence", {**entity, "has_valid_evidence": True}),
            ("UPDATE kg_entities", entity),
            ("FROM kg_evidence", [evidence]),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    row = db.confirm_kg_entity("kg_ent_abc", expected_revision=1)

    assert row["status"] == "usable"
    assert any("UPDATE kg_entities" in sql for sql, _params in conn.calls)
    insert_calls = [(sql, params) for sql, params in conn.calls if "INSERT INTO knowledge_chunks" in sql]
    assert insert_calls
    assert insert_calls[0][1]["source_type"] == "kg_entity"
    assert insert_calls[0][1]["source_id"] == "kg_ent_abc"
    assert insert_calls[0][1]["embedding_status"] == "pending"


def test_confirm_kg_reviews_require_expected_revision():
    """实体和关系确认必须显式提交列表中的 revision，不允许只凭稳定 ID 审核。"""
    import inspect

    entity_parameter = inspect.signature(Database.confirm_kg_entity).parameters[
        "expected_revision"
    ]
    relation_parameter = inspect.signature(Database.confirm_kg_relation).parameters[
        "expected_revision"
    ]
    assert entity_parameter.default is inspect.Parameter.empty
    assert relation_parameter.default is inspect.Parameter.empty


def test_confirm_kg_entity_rejects_revision_changed_before_lock():
    """实体锁到的新 revision 与页面不一致时必须拒绝，且不得读取证据或写投影。"""
    conn = _RecordingConnection(
        [("FOR UPDATE OF ent", {"id": "kg_ent_abc", "review_revision": 2})]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="review revision changed"):
        db.confirm_kg_entity("kg_ent_abc", expected_revision=1)

    assert not any("AS has_valid_evidence" in sql for sql, _params in conn.calls)
    assert not any("UPDATE kg_entities" in sql for sql, _params in conn.calls)


def test_confirm_kg_relation_rejects_revision_changed_after_ordered_locks():
    """关系按端点和关系加锁后必须比对 revision，旧页面不能确认新快照。"""
    locator = {
        "head_entity_id": "kg_ent_a",
        "tail_entity_id": "kg_ent_b",
    }
    conn = _RecordingConnection(
        [
            ("SELECT rel.head_entity_id", locator),
            ("ent.id = ANY", [{"id": "kg_ent_a"}, {"id": "kg_ent_b"}]),
            (
                "FOR UPDATE OF rel",
                {"id": "kg_rel_abc", "review_revision": 2},
            ),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="review revision changed"):
        db.confirm_kg_relation("kg_rel_abc", expected_revision=1)

    assert not any("AS has_valid_evidence" in sql for sql, _params in conn.calls)
    assert not any("UPDATE kg_relations" in sql for sql, _params in conn.calls)


def test_confirm_kg_entity_projects_only_currently_valid_evidence():
    """确认投影不得把已失效来源的旧 excerpt 重新写回 fact 检索文本。"""
    entity = {
        "id": "kg_ent_abc",
        "name": "报告导出",
        "entity_type": "feature_ui_action",
        "aliases": [],
        "description": "导出报告。",
        "confidence": 0.86,
        "status": "usable",
        "review_revision": 1,
    }
    stale_evidence = {
        "source_type": "faq",
        "source_id": "faq_disabled",
        "source_chunk_id": None,
        "excerpt": "已失效的特征词 stale-only-token",
    }
    live_evidence = {
        "source_type": "faq",
        "source_id": "faq_live",
        "source_chunk_id": None,
        "excerpt": "仍有效的导出权限说明",
    }
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF ent", entity),
            ("AS has_valid_evidence", {**entity, "has_valid_evidence": True}),
            ("UPDATE kg_entities", entity),
            ("FROM kg_evidence valid_ev", [live_evidence]),
            ("FROM kg_evidence", [stale_evidence, live_evidence]),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.confirm_kg_entity("kg_ent_abc", expected_revision=1)

    evidence_sql = next(
        sql
        for sql, _params in conn.calls
        if "FROM kg_evidence" in sql and "SELECT" in sql
    )
    insert_payload = next(
        params
        for sql, params in conn.calls
        if "INSERT INTO knowledge_chunks" in sql
    )
    assert "FROM kg_evidence valid_ev" in evidence_sql
    assert "faq.status = 'usable'" in evidence_sql
    assert "imp.is_disabled" in evidence_sql
    assert "chunk.is_disabled" in evidence_sql
    assert "仍有效的导出权限说明" in insert_payload["search_text"]
    assert "stale-only-token" not in insert_payload["search_text"]


@pytest.mark.parametrize(
    "sql_getter",
    [
        lambda: Database._list_valid_kg_entity_evidence_sql(),
        lambda: Database._list_valid_kg_relation_evidence_sql(),
    ],
)
def test_kg_projection_evidence_queries_share_live_source_predicate(sql_getter):
    """实体和关系投影必须共用同一套 FAQ/文档实时来源门禁。"""
    sql = sql_getter()

    assert "FROM kg_evidence valid_ev" in sql
    assert "faq.id = valid_ev.source_id" in sql
    assert "faq.status = 'usable'" in sql
    assert "imp.id = valid_ev.source_id" in sql
    assert "chunk.id = valid_ev.source_chunk_id" in sql
    assert "chunk.file_id = imp.id" in sql
    assert "imp.is_disabled = false" in sql
    assert "chunk.is_disabled = false" in sql


def test_confirm_kg_relation_projects_usable_chunk_with_head_tail_entities():
    """确认 KG 关系时应读取头尾实体并写入 kg_relation 知识单元投影。"""
    relation = {
        "id": "kg_rel_abc",
        "head_entity_id": "kg_ent_head",
        "head_entity_name": "报告导出",
        "head_entity_type": "feature_ui_action",
        "head_entity_status": "usable",
        "relation_type": "requires",
        "tail_entity_id": "kg_ent_tail",
        "tail_entity_name": "账号权限",
        "tail_entity_type": "role_permission_channel",
        "tail_entity_status": "usable",
        "description": "导出报告需要账号具备报告权限。",
        "confidence": 0.8,
        "status": "usable",
        "review_revision": 1,
    }
    evidence = {
        "source_type": "document",
        "source_id": "imp_1",
        "source_chunk_id": "chunk_1",
        "excerpt": "先检查账号权限",
    }
    conn = _RecordingConnection(
        [
            *_kg_relation_lock_responses(relation),
            ("AS has_valid_evidence", {**relation, "has_valid_evidence": True}),
            ("UPDATE kg_relations", relation),
            ("FROM kg_evidence", [evidence]),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    row = db.confirm_kg_relation("kg_rel_abc", expected_revision=1)

    assert row["status"] == "usable"
    assert any("UPDATE kg_relations" in sql for sql, _params in conn.calls)
    insert_calls = [(sql, params) for sql, params in conn.calls if "INSERT INTO knowledge_chunks" in sql]
    assert insert_calls
    assert insert_calls[0][1]["source_type"] == "kg_relation"
    assert insert_calls[0][1]["source_id"] == "kg_rel_abc"
    assert "报告导出 requires 账号权限" in insert_calls[0][1]["source_title"]


def test_confirm_kg_relation_locks_sorted_entities_before_relation():
    """关系确认必须按端点 ID 升序锁实体，再锁关系，避免与实体降级形成反向等待。"""
    relation = {
        "id": "kg_rel_lock_order",
        "head_entity_id": "kg_ent_z",
        "head_entity_name": "报告导出",
        "head_entity_type": "feature_ui_action",
        "head_entity_status": "usable",
        "relation_type": "requires",
        "tail_entity_id": "kg_ent_a",
        "tail_entity_name": "账号权限",
        "tail_entity_type": "role_permission_channel",
        "tail_entity_status": "usable",
        "description": "导出报告需要权限。",
        "confidence": 0.8,
        "status": "usable",
        "has_valid_evidence": True,
        "review_revision": 1,
    }
    conn = _RecordingConnection(
        [
            ("SELECT rel.head_entity_id", relation),
            ("ent.id = ANY", [{"id": "kg_ent_a"}, {"id": "kg_ent_z"}]),
            ("FOR UPDATE OF rel", relation),
            ("AS has_valid_evidence", relation),
            ("UPDATE kg_relations", relation),
            (
                "FROM kg_evidence",
                [
                    {
                        "source_type": "faq",
                        "source_id": "faq_1",
                        "source_chunk_id": None,
                        "excerpt": "检查账号权限",
                    }
                ],
            ),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.confirm_kg_relation("kg_rel_lock_order", expected_revision=1)

    locator_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "SELECT rel.head_entity_id" in sql
    )
    entity_lock_index, entity_lock_params = next(
        (index, params)
        for index, (sql, params) in enumerate(conn.calls)
        if "ent.id = ANY" in sql
    )
    relation_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "FOR UPDATE OF rel" in sql and "head" not in sql
    )
    assert locator_index < entity_lock_index < relation_lock_index
    assert entity_lock_params["entity_ids"] == ["kg_ent_a", "kg_ent_z"]


def test_confirm_kg_relation_rejects_endpoint_change_after_locking():
    """端点锁定后必须复核关系 locator，不能确认并发改向了端点的关系。"""
    locator = {
        "id": "kg_rel_lock_order",
        "head_entity_id": "kg_ent_a",
        "tail_entity_id": "kg_ent_b",
        "review_revision": 1,
    }
    changed_context = {
        **locator,
        "tail_entity_id": "kg_ent_c",
        "head_entity_status": "usable",
        "tail_entity_status": "usable",
        "has_valid_evidence": True,
    }
    conn = _RecordingConnection(
        [
            ("SELECT rel.head_entity_id", locator),
            ("ent.id = ANY", [{"id": "kg_ent_a"}, {"id": "kg_ent_b"}]),
            ("FOR UPDATE OF rel", locator),
            ("AS has_valid_evidence", changed_context),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="endpoints changed"):
        db.confirm_kg_relation("kg_rel_lock_order", expected_revision=1)

    assert not any("UPDATE kg_relations" in sql for sql, _params in conn.calls)


def test_replace_kg_source_snapshot_writes_entities_relations_and_evidence():
    """唯一来源快照入口应原子保存完整端点、关系和证据。"""
    source_text = "报告导出失败时，先检查账号权限。"
    full_excerpt = "报告导出失败时，先检查账号权限。"
    permission_excerpt = "先检查账号权限。"
    permission_start = source_text.index(permission_excerpt)
    conn = _RecordingConnection()
    db = Database("postgresql://unused")
    db.connect = lambda: conn
    extraction = {
        "entities": [
            {
                "id": "kg_ent_head",
                "name": "报告导出",
                "entity_type": "feature_ui_action",
                "aliases": ["导出报告"],
                "description": "导出报告入口",
                "status": "needs_review",
                "confidence": 0.86,
                "source_count": 1,
                "evidence": [
                    {
                        "source_type": "document",
                        "source_id": "imp_1",
                        "source_chunk_id": "chunk_1",
                        "source_title": "平台使用手册.pdf",
                        "section_path": ["报告"],
                        "page_start": 3,
                        "page_end": 4,
                        "excerpt": full_excerpt,
                        "char_start": 0,
                        "char_end": len(full_excerpt),
                    }
                ],
            },
            {
                "id": "kg_ent_tail",
                "name": "账号权限",
                "entity_type": "role_permission_channel",
                "status": "needs_review",
                "evidence": [
                    {
                        "source_type": "document",
                        "source_id": "imp_1",
                        "source_chunk_id": "chunk_1",
                        "source_title": "平台使用手册.pdf",
                        "section_path": ["报告"],
                        "page_start": 3,
                        "page_end": 4,
                        "excerpt": permission_excerpt,
                        "char_start": permission_start,
                        "char_end": permission_start + len(permission_excerpt),
                    }
                ],
            },
        ],
        "relations": [
            {
                "id": "kg_rel_requires",
                "head_entity_id": "kg_ent_head",
                "relation_type": "requires",
                "tail_entity_id": "kg_ent_tail",
                "description": "导出报告需要账号权限",
                "status": "needs_review",
                "confidence": 0.8,
                "evidence": [
                    {
                        "source_type": "document",
                        "source_id": "imp_1",
                        "source_chunk_id": "chunk_1",
                        "source_title": "平台使用手册.pdf",
                        "section_path": ["报告"],
                        "page_start": 3,
                        "page_end": 4,
                        "excerpt": permission_excerpt,
                        "char_start": permission_start,
                        "char_end": permission_start + len(permission_excerpt),
                    }
                ],
            }
        ],
    }

    result = db._replace_kg_source_snapshot_in_conn(
        conn,
        source_type="document",
        source_ids=["imp_1"],
        source_chunk_ids=None,
        extraction=extraction,
    )

    assert result == {"entity_count": 2, "relation_count": 1, "evidence_count": 3}
    assert any("INSERT INTO kg_entities" in sql for sql, _params in conn.calls)
    assert any("INSERT INTO kg_relations" in sql for sql, _params in conn.calls)
    evidence_calls = [(sql, params) for sql, params in conn.calls if "INSERT INTO kg_evidence" in sql]
    assert len(evidence_calls) == 3
    assert {params["entity_id"] for _sql, params in evidence_calls} == {
        "kg_ent_head",
        "kg_ent_tail",
        None,
    }
    assert any(params["relation_id"] == "kg_rel_requires" for _sql, params in evidence_calls)
    assert all(params["source_id"] == "imp_1" for _sql, params in evidence_calls)


def test_replace_kg_source_snapshot_uses_stable_global_lock_order():
    """快照替换必须先锁 owner，再替换证据，最后按实体到关系顺序实时重算。"""
    db = Database("postgresql://unused")
    conn = _RecordingConnection()
    source_text = "问题：顺序测试\n答案：实体证据一；实体证据二；关系证据一；关系证据二。"
    evidence_base = {
        "source_type": "faq",
        "source_id": "faq_1",
        "source_chunk_id": None,
        "source_title": "顺序测试",
        "section_path": [],
        "page_start": None,
        "page_end": None,
    }
    entity_evidence = [
        _kg_evidence_from_text(source_text, evidence_base, "实体证据一"),
        _kg_evidence_from_text(source_text, evidence_base, "实体证据二"),
    ]
    relation_evidence = [
        _kg_evidence_from_text(source_text, evidence_base, "关系证据一"),
        _kg_evidence_from_text(source_text, evidence_base, "关系证据二"),
    ]
    entity_evidence.sort(
        key=lambda item: db._kg_evidence_id(
            item,
            entity_id="kg_ent_a",
            relation_id=None,
        ),
        reverse=True,
    )
    relation_evidence.sort(
        key=lambda item: db._kg_evidence_id(
            item,
            entity_id=None,
            relation_id="kg_rel_a",
        ),
        reverse=True,
    )
    extraction = {
        "entities": [
                {
                    "id": "kg_ent_z",
                    "name": "Z",
                    "entity_type": "condition_policy",
                    "evidence": [],
                },
                {
                    "id": "kg_ent_a",
                    "name": "A",
                    "entity_type": "feature_ui_action",
                "evidence": entity_evidence,
            },
        ],
        "relations": [
            {
                    "id": "kg_rel_z",
                    "head_entity_id": "kg_ent_z",
                    "relation_type": "requires",
                "tail_entity_id": "kg_ent_a",
                "evidence": [],
            },
            {
                    "id": "kg_rel_a",
                    "head_entity_id": "kg_ent_a",
                    "relation_type": "requires",
                "tail_entity_id": "kg_ent_z",
                "evidence": relation_evidence,
            },
        ],
    }

    db._replace_kg_source_snapshot_in_conn(
        conn,
        source_type="faq",
        source_ids=["faq_1"],
        source_chunk_ids=None,
        extraction=extraction,
    )

    entity_ids = [
        params["id"]
        for sql, params in conn.calls
        if "INSERT INTO kg_entities" in sql
    ]
    relation_ids = [
        params["id"]
        for sql, params in conn.calls
        if "INSERT INTO kg_relations" in sql
    ]
    entity_evidence_ids = [
        params["id"]
        for sql, params in conn.calls
        if "INSERT INTO kg_evidence" in sql and params["entity_id"] == "kg_ent_a"
    ]
    relation_evidence_ids = [
        params["id"]
        for sql, params in conn.calls
        if "INSERT INTO kg_evidence" in sql and params["relation_id"] == "kg_rel_a"
    ]
    entity_upsert_indices = [
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "INSERT INTO kg_entities" in sql
    ]
    relation_upsert_indices = [
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "INSERT INTO kg_relations" in sql
    ]
    delete_evidence_indices = [
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "DELETE FROM kg_evidence" in sql
    ]
    evidence_indices_and_ids = [
        (index, params["id"])
        for index, (sql, params) in enumerate(conn.calls)
        if "INSERT INTO kg_evidence" in sql
    ]
    entity_reconcile_indices = [
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "UPDATE kg_entities" in sql
    ]
    relation_reconcile_indices = [
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "UPDATE kg_relations" in sql
    ]
    incident_query_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "rel.head_entity_id = ANY" in sql
    )
    assert entity_ids == ["kg_ent_a", "kg_ent_z"]
    assert relation_ids == ["kg_rel_a", "kg_rel_z"]
    assert entity_evidence_ids == sorted(entity_evidence_ids)
    assert relation_evidence_ids == sorted(relation_evidence_ids)
    assert max(entity_upsert_indices) < incident_query_index < min(relation_upsert_indices)
    assert len(delete_evidence_indices) == 1
    assert entity_reconcile_indices
    assert relation_reconcile_indices
    assert (
        max(relation_upsert_indices)
        < delete_evidence_indices[0]
        < min(index for index, _evidence_id in evidence_indices_and_ids)
    )
    assert (
        max(index for index, _evidence_id in evidence_indices_and_ids)
        < min(entity_reconcile_indices)
        < min(relation_reconcile_indices)
    )
    assert [evidence_id for _index, evidence_id in evidence_indices_and_ids] == sorted(
        evidence_id for _index, evidence_id in evidence_indices_and_ids
    )


def test_reextracting_kg_candidates_resets_candidates_and_projections_to_review():
    """重复抽取必须撤销旧审核结论，实体、关系及既有投影都回到待复核。"""
    source_text = "问题：报告怎么导出？\n答案：先检查账号权限。"
    source = {
        "source_type": "faq",
        "source_id": "faq_1",
        "source_chunk_id": None,
        "source_title": "报告怎么导出？",
        "section_path": [],
        "page_start": None,
        "page_end": None,
    }
    source_evidence = _kg_evidence_from_text(source_text, source, "先检查账号权限。")
    conn = _RecordingConnection(
        [
            (
                "UPDATE kg_entities entity",
                [
                    {"id": "kg_ent_head", "status": "needs_review"},
                    {"id": "kg_ent_tail", "status": "needs_review"},
                ],
            ),
            (
                "UPDATE kg_relations relation",
                [{"id": "kg_rel_requires", "status": "needs_review"}],
            ),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db._replace_kg_source_snapshot_in_conn(
        conn,
        source_type="faq",
        source_ids=["faq_1"],
        source_chunk_ids=None,
        extraction={
            "entities": [
                {
                    "id": "kg_ent_head",
                    "name": "报告导出",
                    "entity_type": "feature_ui_action",
                    "description": "AI 更新后的描述",
                    "status": "usable",
                    "evidence": [source_evidence],
                },
                {
                    "id": "kg_ent_tail",
                    "name": "账号权限",
                    "entity_type": "role_permission_channel",
                    "status": "usable",
                    "evidence": [source_evidence],
                },
            ],
            "relations": [
                {
                    "id": "kg_rel_requires",
                    "head_entity_id": "kg_ent_head",
                    "relation_type": "requires",
                    "tail_entity_id": "kg_ent_tail",
                    "description": "AI 更新后的关系",
                    "status": "usable",
                    "evidence": [source_evidence],
                }
            ],
        },
    )

    entity_sql, entity_params = next(
        (sql, params) for sql, params in conn.calls if "INSERT INTO kg_entities" in sql
    )
    relation_sql, relation_params = next(
        (sql, params) for sql, params in conn.calls if "INSERT INTO kg_relations" in sql
    )
    assert entity_params["status"] == "needs_review"
    assert relation_params["status"] == "needs_review"
    assert "status = EXCLUDED.status" in entity_sql
    assert "status = EXCLUDED.status" in relation_sql
    projection_params = [
        params
        for sql, params in conn.calls
        if "UPDATE knowledge_chunks" in sql and "source_type = %(source_type)s" in sql
    ]
    assert {
        "id": "kg_ent_head",
        "status": "needs_review",
        "source_type": "kg_entity",
    } in projection_params
    assert {
        "id": "kg_rel_requires",
        "status": "needs_review",
        "source_type": "kg_relation",
    } in projection_params


def test_complete_document_kg_job_fences_manifest_before_owner_writes():
    """file/chunks/job/items 全量复核后，才可按实体到关系锁序发布 snapshot。"""
    state = _document_kg_completion_state()
    extraction = _document_kg_reduced_extraction(state)
    completed = _kg_public_job_row(
        phase="completed",
        processed_chunks=2,
        total_chunks=2,
        entity_count=3,
        relation_count=1,
        evidence_count=4,
    )
    conn = _document_kg_completion_connection(
        state,
        completed=completed,
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.complete_document_kg_extraction_job(
        "kg_job_1",
        lease_token="lease_1",
        extraction=extraction,
    )

    assert result == completed
    file_lock = _kg_call_index(conn, "FOR UPDATE OF imp")
    chunk_lock = _kg_call_index(conn, "FOR UPDATE OF chunk")
    job_lock = _kg_call_index(conn, "FOR UPDATE OF job")
    item_lock = _kg_call_index(conn, "FOR UPDATE OF item")
    entity_upsert = _kg_call_index(conn, "INSERT INTO kg_entities")
    fresh_relation_read = _kg_call_index(conn, "rel.head_entity_id = ANY")
    relation_upsert = _kg_call_index(conn, "INSERT INTO kg_relations")
    evidence_delete = _kg_call_index(conn, "DELETE FROM kg_evidence")
    evidence_insert = _kg_call_index(conn, "INSERT INTO kg_evidence")
    entity_reconcile = _kg_call_index(conn, "UPDATE kg_entities entity")
    relation_reconcile = _kg_call_index(conn, "UPDATE kg_relations relation")
    complete_job = _kg_call_index(conn, "SET phase = 'completed'")
    clear_staging = _kg_call_index(conn, "SET map_result = NULL")
    assert "resolution_result = NULL" in conn.calls[complete_job][0]
    assert (
        file_lock
        < chunk_lock
        < job_lock
        < item_lock
        < entity_upsert
        < fresh_relation_read
        < relation_upsert
        < evidence_delete
        < evidence_insert
        < entity_reconcile
        < relation_reconcile
        < complete_job
        < clear_staging
    )
    delete_params = conn.calls[evidence_delete][1]
    assert delete_params == {
        "source_type": "document",
        "source_ids": ["imp_1"],
        "source_chunk_ids": [],
    }
    evidence_params = [
        params
        for sql, params in conn.calls
        if "INSERT INTO kg_evidence" in sql
    ]
    assert len(evidence_params) == 4
    assert all(params["source_id"] == "imp_1" for params in evidence_params)
    assert all(
        isinstance(params["char_start"], int)
        and params["char_end"] > params["char_start"]
        for params in evidence_params
    )


@pytest.mark.parametrize(
    ("locked_overrides", "message"),
    [
        ({"phase": "completed"}, "phase"),
        ({"lease_token": "lease_other"}, "lease"),
        ({"lease_is_current": False}, "lease"),
    ],
)
def test_complete_document_kg_job_rechecks_lease_phase_after_source_locks(
    locked_overrides,
    message,
):
    """无锁快检后必须先锁来源再锁 job，二次 lease/phase 失败时不得写 snapshot。"""
    state = _document_kg_completion_state()
    locked_job = {**state["job"], **locked_overrides}
    conn = _document_kg_completion_connection(state, locked_job=locked_job)
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match=message):
        db.complete_document_kg_extraction_job(
            "kg_job_1",
            lease_token="lease_1",
            extraction={"entities": [], "relations": []},
        )

    assert _kg_call_index(conn, "FOR UPDATE OF imp") < _kg_call_index(
        conn,
        "FOR UPDATE OF chunk",
    ) < _kg_call_index(conn, "FOR UPDATE OF job")
    assert not any("FOR UPDATE OF item" in sql for sql, _params in conn.calls)
    _assert_no_kg_snapshot_writes(conn)


@pytest.mark.parametrize(
    "manifest_change",
    [
        "job_fingerprint",
        "missing_item",
        "item_fingerprint",
        "item_phase",
        "file_title",
        "chunk_text",
        "chunk_set",
    ],
)
def test_complete_document_kg_job_rejects_document_manifest_fence_before_writes(
    manifest_change,
):
    """父指纹、item 集合及任一 frozen locator 变化都必须挡在 owner 写入之前。"""
    state = _document_kg_completion_state()
    if manifest_change == "job_fingerprint":
        state["job"] = {**state["job"], "source_fingerprint": "sha256:stale"}
    elif manifest_change == "missing_item":
        state["items"] = state["items"][:-1]
    elif manifest_change == "item_fingerprint":
        state["items"][0] = {
            **state["items"][0],
            "source_fingerprint": "sha256:stale-item",
        }
    elif manifest_change == "item_phase":
        state["items"][0] = {**state["items"][0], "phase": "mapping"}
    elif manifest_change == "file_title":
        state["file"] = {**state["file"], "original_name": "已改名手册.pdf"}
    elif manifest_change == "chunk_text":
        state["chunks"][0] = {
            **state["chunks"][0],
            "source_text": "重解析后的新正文。",
        }
    else:
        state["chunks"] = state["chunks"][:-1]
    conn = _document_kg_completion_connection(state)
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="manifest|ready"):
        db.complete_document_kg_extraction_job(
            "kg_job_1",
            lease_token="lease_1",
            extraction={"entities": [], "relations": []},
        )

    assert _kg_call_index(conn, "FOR UPDATE OF imp") < _kg_call_index(
        conn,
        "FOR UPDATE OF chunk",
    ) < _kg_call_index(conn, "FOR UPDATE OF job") < _kg_call_index(
        conn,
        "FOR UPDATE OF item",
    )
    _assert_no_kg_snapshot_writes(conn)


@pytest.mark.parametrize(
    "evidence_change",
    [
        "negative_start",
        "end_out_of_bounds",
        "substring",
        "source_type",
        "source_id",
        "source_chunk_id",
        "source_title",
        "section_path",
        "page_start",
        "page_end",
    ],
)
def test_complete_document_kg_job_validates_evidence_before_any_snapshot_write(
    evidence_change,
):
    """文档 evidence 的 offset、原文和全部 file/chunk locator 必须先整体校验。"""
    state = _document_kg_completion_state()
    extraction = _document_kg_reduced_extraction(state)
    evidence = extraction["entities"][0]["evidence"][0]
    if evidence_change == "negative_start":
        evidence["char_start"] = -1
    elif evidence_change == "end_out_of_bounds":
        evidence["char_end"] = len(state["chunks"][0]["source_text"]) + 1
    elif evidence_change == "substring":
        evidence["excerpt"] = "错误正文"
    elif evidence_change == "source_type":
        evidence["source_type"] = "faq"
    elif evidence_change == "source_id":
        evidence["source_id"] = "imp_other"
    elif evidence_change == "source_chunk_id":
        evidence["source_chunk_id"] = "chunk_missing"
    elif evidence_change == "source_title":
        evidence["source_title"] = "另一份手册.pdf"
    elif evidence_change == "section_path":
        evidence["section_path"] = ["其他章节"]
    elif evidence_change == "page_start":
        evidence["page_start"] = 99
    else:
        evidence["page_end"] = 99
    conn = _document_kg_completion_connection(state)
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="evidence|offset|locator|source chunk"):
        db.complete_document_kg_extraction_job(
            "kg_job_1",
            lease_token="lease_1",
            extraction=extraction,
        )

    assert _kg_call_index(conn, "FOR UPDATE OF item") < len(conn.calls)
    _assert_no_kg_snapshot_writes(conn)


def test_complete_document_kg_job_rejects_missing_relation_endpoint_before_writes():
    """Reduce 关系端点不在最终实体集合时，不能先 upsert 任一 canonical owner。"""
    state = _document_kg_completion_state()
    extraction = _document_kg_reduced_extraction(state)
    extraction["relations"][0]["tail_entity_id"] = "kg_ent_missing"
    conn = _document_kg_completion_connection(state)
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="endpoints"):
        db.complete_document_kg_extraction_job(
            "kg_job_1",
            lease_token="lease_1",
            extraction=extraction,
        )

    _assert_no_kg_snapshot_writes(conn)


def test_complete_document_kg_job_empty_snapshot_retires_entire_file_scope():
    """完整文档 Reduce 为空时仍删除该 file 全部旧 evidence 并实时回退遗漏 owner。"""
    state = _document_kg_completion_state()
    completed = _kg_public_job_row(
        phase="completed",
        processed_chunks=2,
        total_chunks=2,
        entity_count=0,
        relation_count=0,
        evidence_count=0,
    )
    old_relation = {
        "id": "kg_rel_old",
        "head_entity_id": "kg_ent_old",
        "tail_entity_id": "kg_ent_endpoint",
    }
    conn = _document_kg_completion_connection(
        state,
        completed=completed,
        extra_responses=[
            ("SELECT DISTINCT evidence.entity_id AS id", [{"id": "kg_ent_old"}]),
            ("SELECT DISTINCT rel.id, rel.head_entity_id", [old_relation]),
            ("rel.head_entity_id = ANY", [{"id": "kg_rel_old"}]),
            ("FOR UPDATE OF ent", {"id": "kg_ent_old"}),
            ("FOR UPDATE OF rel", old_relation),
            (
                "UPDATE kg_entities entity",
                [{"id": "kg_ent_old", "status": "disabled"}],
            ),
            (
                "UPDATE kg_relations relation",
                [{"id": "kg_rel_old", "status": "disabled"}],
            ),
        ],
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.complete_document_kg_extraction_job(
        "kg_job_1",
        lease_token="lease_1",
        extraction={"entities": [], "relations": []},
    )

    assert result == completed
    deletion = next(
        (sql, params)
        for sql, params in conn.calls
        if "DELETE FROM kg_evidence" in sql
    )
    assert deletion[1] == {
        "source_type": "document",
        "source_ids": ["imp_1"],
        "source_chunk_ids": [],
    }
    assert not any("INSERT INTO kg_evidence" in sql for sql, _params in conn.calls)
    entity_reconcile = next(
        params
        for sql, params in conn.calls
        if "UPDATE kg_entities entity" in sql
    )
    relation_reconcile = next(
        params
        for sql, params in conn.calls
        if "UPDATE kg_relations relation" in sql
    )
    assert entity_reconcile == {
        "entity_ids": ["kg_ent_old"],
        "entity_revision_ids": ["kg_ent_old"],
    }
    assert relation_reconcile == {
        "relation_ids": ["kg_rel_old"],
        "relation_revision_ids": ["kg_rel_old"],
    }
    assert _kg_call_index(conn, "DELETE FROM kg_evidence") < _kg_call_index(
        conn,
        "UPDATE kg_entities entity",
    ) < _kg_call_index(conn, "UPDATE kg_relations relation")


def test_complete_faq_kg_job_uses_offsets_and_one_live_reconcile_path():
    """FAQ completion 复核单来源 guard，并用精确 offset 发布后实时重算 owner。"""
    state = _faq_kg_completion_state()
    conn = _RecordingConnection(
        [
            ("SET phase = 'completed'", state["completed"]),
            ("FOR UPDATE OF job", state["job"]),
            ("FOR UPDATE OF faq", state["faq"]),
            ("WHERE job.id = %(job_id)s", state["job"]),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.complete_faq_kg_extraction_job(
        "kg_job_1",
        lease_token="lease_1",
        extraction=state["extraction"],
    )

    assert result == state["completed"]
    faq_lock = _kg_call_index(conn, "FOR UPDATE OF faq")
    job_lock = _kg_call_index(conn, "FOR UPDATE OF job")
    entity_upsert = _kg_call_index(conn, "INSERT INTO kg_entities")
    evidence_delete = _kg_call_index(conn, "DELETE FROM kg_evidence")
    evidence_insert = _kg_call_index(conn, "INSERT INTO kg_evidence")
    reconcile = _kg_call_index(conn, "UPDATE kg_entities entity")
    complete = _kg_call_index(conn, "SET phase = 'completed'")
    assert faq_lock < job_lock < entity_upsert < evidence_delete < evidence_insert < reconcile < complete
    evidence_params = conn.calls[evidence_insert][1]
    evidence = state["extraction"]["entities"][0]["evidence"][0]
    assert evidence_params["char_start"] == evidence["char_start"]
    assert evidence_params["char_end"] == evidence["char_end"]
    assert state["source_text"][evidence["char_start"] : evidence["char_end"]] == evidence[
        "excerpt"
    ]
    assert conn.calls[evidence_delete][1] == {
        "source_type": "faq",
        "source_ids": ["faq_1"],
        "source_chunk_ids": [],
    }
    assert sum(
        "UPDATE kg_entities entity" in sql for sql, _params in conn.calls
    ) == 1


@pytest.mark.parametrize("fence_change", ["phase", "lease", "source"])
def test_complete_faq_kg_job_rechecks_phase_lease_and_source_before_writes(
    fence_change,
):
    """FAQ 来源锁后的 job 与正文指纹任一变化都必须丢弃 generation。"""
    state = _faq_kg_completion_state()
    locked_job = state["job"]
    current_faq = state["faq"]
    expected_message = "fingerprint"
    if fence_change == "phase":
        locked_job = {**locked_job, "phase": "failed"}
        expected_message = "phase"
    elif fence_change == "lease":
        locked_job = {**locked_job, "lease_token": "lease_other"}
        expected_message = "lease"
    else:
        current_faq = {**current_faq, "answer": "正文已在模型返回前修改。"}
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF job", locked_job),
            ("FOR UPDATE OF faq", current_faq),
            ("WHERE job.id = %(job_id)s", state["job"]),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match=expected_message):
        db.complete_faq_kg_extraction_job(
            "kg_job_1",
            lease_token="lease_1",
            extraction=state["extraction"],
        )

    assert _kg_call_index(conn, "FOR UPDATE OF faq") < _kg_call_index(
        conn,
        "FOR UPDATE OF job",
    )
    _assert_no_kg_snapshot_writes(conn)


@pytest.mark.parametrize("evidence_change", ["bounds", "substring", "locator"])
def test_complete_faq_kg_job_rejects_invalid_offsets_before_snapshot_writes(
    evidence_change,
):
    """FAQ evidence 同样必须在 owner 写入前校验 offset、substring 与来源 locator。"""
    state = _faq_kg_completion_state()
    evidence = state["extraction"]["entities"][0]["evidence"][0]
    if evidence_change == "bounds":
        evidence["char_end"] = len(state["source_text"]) + 1
    elif evidence_change == "substring":
        evidence["excerpt"] = "错误文本"
    else:
        evidence["source_id"] = "faq_other"
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF job", state["job"]),
            ("FOR UPDATE OF faq", state["faq"]),
            ("WHERE job.id = %(job_id)s", state["job"]),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="evidence|offset|locator"):
        db.complete_faq_kg_extraction_job(
            "kg_job_1",
            lease_token="lease_1",
            extraction=state["extraction"],
        )

    _assert_no_kg_snapshot_writes(conn)


def test_complete_faq_kg_job_offsets_flow_through_all_evidence_sql():
    """char_start/end 必须贯通参数、INSERT、审核列表和确认投影 SELECT。"""
    evidence = _faq_kg_completion_state()["extraction"]["entities"][0]["evidence"][0]
    params = Database._kg_evidence_params(evidence, entity_id="kg_ent_permission")

    assert params["char_start"] == evidence["char_start"]
    assert params["char_end"] == evidence["char_end"]
    insert_sql = Database._insert_kg_evidence_sql()
    assert "char_start, char_end" in " ".join(insert_sql.split())
    assert "%(char_start)s, %(char_end)s" in " ".join(insert_sql.split())
    for sql in (
        Database._list_kg_entities_sql(),
        Database._list_kg_relations_sql(),
    ):
        assert "'char_start', ev.char_start" in sql
        assert "'char_end', ev.char_end" in sql
    for sql in (
        Database._list_valid_kg_entity_evidence_sql(),
        Database._list_valid_kg_relation_evidence_sql(),
    ):
        assert "valid_ev.char_start" in sql
        assert "valid_ev.char_end" in sql


def test_failed_kg_generation_clears_staging_without_touching_snapshot():
    """失败 generation 只清 job/item staging，不得写 owner、evidence 或投影。"""
    internal = _kg_internal_job_row(
        phase="reducing",
        processed_chunks=2,
        total_chunks=2,
        resolution_result={"groups": []},
    )
    failed = _kg_public_job_row(
        phase="failed",
        processed_chunks=2,
        total_chunks=2,
        error="模型失败",
    )
    conn = _RecordingConnection(
        [
            ("SET phase = 'failed'", failed),
            ("FOR UPDATE OF job", internal),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.fail_kg_extraction_job(
        "kg_job_1",
        lease_token="lease_1",
        error="模型失败",
    )

    assert result == failed
    clear_items = _kg_call_index(conn, "SET phase = CASE")
    fail_job = _kg_call_index(conn, "SET phase = 'failed'")
    assert clear_items < fail_job
    clear_sql = conn.calls[clear_items][0]
    clear_params = conn.calls[clear_items][1]
    fail_sql = conn.calls[fail_job][0]
    assert "map_result = NULL" in clear_sql
    assert "job.lease_token = %(lease_token)s" in clear_sql
    assert "job.lease_expires_at > now()" in clear_sql
    assert clear_params["lease_token"] == "lease_1"
    assert "resolution_result = NULL" in fail_sql
    _assert_no_kg_snapshot_writes(conn)


def test_failed_kg_generation_parent_cas_failure_rolls_back_item_clear():
    """父任务 fenced UPDATE 失败时必须在事务退出前抛错，禁止提交 item 清理。"""

    class _ExitAwareConnection(_RecordingConnection):
        """记录事务退出时收到的异常类型，区分 rollback 与退出后才报错。"""

        def __init__(self, responses):
            """初始化预设响应并保留退出异常槽位。"""
            super().__init__(responses)
            self.exit_exception_type = None

        def __exit__(self, exc_type, exc, tb):
            """记录异常后沿用真实事务上下文的不吞异常行为。"""
            self.exit_exception_type = exc_type
            return False

    internal = _kg_internal_job_row(phase="reducing")
    conn = _ExitAwareConnection(
        [
            ("SET phase = 'failed'", None),
            ("FOR UPDATE OF job", internal),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="lease expired before failure"):
        db.fail_kg_extraction_job(
            "kg_job_1",
            lease_token="lease_1",
            error="迟到 worker",
        )

    assert conn.exit_exception_type is ValueError


def test_confirm_kg_entity_rejects_when_all_evidence_sources_are_invalid():
    """实体没有任何仍可用的 FAQ 或文档证据时不能确认。"""
    entity = {
        "id": "kg_ent_abc",
        "name": "报告导出",
        "entity_type": "feature_ui_action",
        "status": "needs_review",
        "review_revision": 1,
    }
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF ent", entity),
            ("AS has_valid_evidence", {**entity, "has_valid_evidence": False}),
            ("UPDATE kg_entities", {**entity, "status": "usable"}),
            ("FROM kg_evidence", []),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="valid evidence"):
        db.confirm_kg_entity("kg_ent_abc", expected_revision=1)

    assert not any("INSERT INTO knowledge_chunks" in sql for sql, _params in conn.calls)


def test_confirm_kg_relation_rejects_when_all_evidence_sources_are_invalid():
    """关系只有失效来源证据时不能确认，即使头尾实体仍可用。"""
    relation = {
        "id": "kg_rel_abc",
        "head_entity_id": "kg_ent_head",
        "head_entity_name": "报告导出",
        "head_entity_type": "feature_ui_action",
        "head_entity_status": "usable",
        "relation_type": "requires",
        "tail_entity_id": "kg_ent_tail",
        "tail_entity_name": "账号权限",
        "tail_entity_type": "role_permission_channel",
        "tail_entity_status": "usable",
        "status": "needs_review",
        "review_revision": 1,
    }
    conn = _RecordingConnection(
        [
            *_kg_relation_lock_responses(relation),
            ("AS has_valid_evidence", {**relation, "has_valid_evidence": False}),
            ("UPDATE kg_relations", {**relation, "status": "usable"}),
            ("FROM kg_evidence", []),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="valid evidence"):
        db.confirm_kg_relation("kg_rel_abc", expected_revision=1)

    assert not any("INSERT INTO knowledge_chunks" in sql for sql, _params in conn.calls)


def test_confirm_kg_relation_rejects_nonusable_endpoint_entities():
    """关系确认必须要求头尾实体都已确认，不能产生悬空的可检索边。"""
    relation = {
        "id": "kg_rel_abc",
        "head_entity_id": "kg_ent_head",
        "head_entity_name": "报告导出",
        "head_entity_type": "feature_ui_action",
        "head_entity_status": "needs_review",
        "relation_type": "requires",
        "tail_entity_id": "kg_ent_tail",
        "tail_entity_name": "账号权限",
        "tail_entity_type": "role_permission_channel",
        "tail_entity_status": "usable",
        "has_valid_evidence": True,
        "status": "needs_review",
        "review_revision": 1,
    }
    conn = _RecordingConnection(
        [
            *_kg_relation_lock_responses(relation),
            ("AS has_valid_evidence", relation),
            ("UPDATE kg_relations", {**relation, "status": "usable"}),
            ("FROM kg_evidence", [{"source_type": "faq", "source_id": "faq_1"}]),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="endpoint entities"):
        db.confirm_kg_relation("kg_rel_abc", expected_revision=1)


def test_set_kg_relation_status_rejects_usable_confirmation_bypass():
    """关系进入 usable 只能走 confirm，通用状态入口不得维护第二套确认逻辑。"""
    conn = _RecordingConnection()
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="confirm"):
        db.set_kg_relation_status("kg_rel_abc", "usable")

    assert conn.calls == []


def test_kg_review_checks_live_evidence_after_candidate_row_lock():
    """审核必须先锁 KG 行再查实时证据，让等待锁后的查询获得新 READ COMMITTED 快照。"""
    entity = {
        "id": "kg_ent_abc",
        "name": "报告导出",
        "entity_type": "feature_ui_action",
        "status": "needs_review",
        "review_revision": 1,
    }
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF ent", entity),
            ("AS has_valid_evidence", {**entity, "has_valid_evidence": False}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="valid evidence"):
        db.confirm_kg_entity("kg_ent_abc", expected_revision=1)

    lock_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "FOR UPDATE OF ent" in sql
    )
    evidence_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "AS has_valid_evidence" in sql
    )
    lock_sql = conn.calls[lock_index][0]
    evidence_sql = conn.calls[evidence_index][0]
    assert lock_index < evidence_index
    assert "has_valid_evidence" not in lock_sql
    assert "FOR UPDATE" not in evidence_sql


def test_source_invalidation_locks_owners_then_deletes_evidence_before_reconciliation():
    """来源失效必须按实体、关系、删证据、实体重算、关系重算的唯一顺序执行。"""
    conn = _RecordingConnection(
        [
            (
                "SELECT DISTINCT evidence.entity_id AS id",
                [{"id": "kg_ent_a"}, {"id": "kg_ent_z"}],
            ),
            (
                "SELECT DISTINCT rel.id, rel.head_entity_id",
                [],
            ),
            (
                "rel.head_entity_id = ANY",
                [{"id": "kg_rel_a"}, {"id": "kg_rel_z"}],
            ),
            (
                "FOR UPDATE OF ent",
                lambda _sql, params: {"id": params["id"]},
            ),
            (
                "FOR UPDATE OF rel",
                lambda _sql, params: {"id": params["id"]},
            ),
        ]
    )
    db = Database("postgresql://unused")

    db._reconcile_kg_source_change_in_conn(
        conn,
        source_type="faq",
        source_ids=["faq_1"],
        delete_evidence=True,
    )

    entity_locks = [
        (index, sql, params)
        for index, (sql, params) in enumerate(conn.calls)
        if "FOR UPDATE OF ent" in sql
    ]
    relation_locks = [
        (index, sql, params)
        for index, (sql, params) in enumerate(conn.calls)
        if "FOR UPDATE OF rel" in sql
    ]
    evidence_delete_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "DELETE FROM kg_evidence" in sql
    )
    entity_update_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "UPDATE kg_entities" in sql
    )
    relation_update_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "UPDATE kg_relations" in sql
    )
    assert max(index for index, _sql, _params in entity_locks) < min(
        index for index, _sql, _params in relation_locks
    )
    assert (
        max(index for index, _sql, _params in relation_locks)
        < evidence_delete_index
        < entity_update_index
        < relation_update_index
    )
    assert [params["id"] for _index, _sql, params in entity_locks] == [
        "kg_ent_a",
        "kg_ent_z",
    ]
    assert [params["id"] for _index, _sql, params in relation_locks] == [
        "kg_rel_a",
        "kg_rel_z",
    ]


def test_database_exposes_only_current_kg_source_reconciliation_contract():
    """来源变化只保留唯一重算入口，不保留旧 apply/invalidate 包装方法。"""
    reconciliation_methods = {
        name
        for name in dir(Database)
        if name.startswith("_reconcile_kg_source_change")
        or name.startswith("_apply_kg_source")
        or name.startswith("_invalidate_kg_sources")
    }

    assert reconciliation_methods == {"_reconcile_kg_source_change_in_conn"}


def test_kg_entity_params_ignore_payload_source_count():
    """实体候选参数不得信任 payload 或 evidence 数量生成数据库来源数。"""
    params = Database._kg_entity_params(
        {
            "id": "kg_ent_1",
            "name": "报告导出",
            "entity_type": "feature_ui_action",
            "source_count": 99,
            "evidence": [{"excerpt": "同一来源的证据"}],
        }
    )
    assert "source_count" not in params


def test_kg_entity_upsert_does_not_preserve_historical_source_count():
    """实体 upsert 不得用历史最大值或 payload 占位符写入来源数。"""
    sql = Database._upsert_kg_entity_sql()

    assert "GREATEST" not in sql
    assert "%(source_count)s" not in sql


def test_replace_kg_source_snapshot_mixes_source_and_candidate_entities_in_global_order():
    """旧来源 Z 与新候选 A 必须形成同一实体全序，之后才能读取并锁定关联关系。"""
    source_text = "问题：如何导出？\n答案：先检查权限。"
    source = {
        "source_type": "faq",
        "source_id": "faq_1",
        "source_chunk_id": None,
        "source_title": "如何导出？",
        "section_path": [],
        "page_start": None,
        "page_end": None,
    }
    conn = _RecordingConnection(
        [
            ("SELECT DISTINCT evidence.entity_id AS id", [{"id": "kg_ent_z"}]),
            ("SELECT DISTINCT rel.id, rel.head_entity_id", []),
            ("FOR UPDATE OF ent", lambda _sql, params: {"id": params["id"]}),
            ("rel.head_entity_id = ANY", [{"id": "kg_rel_middle"}]),
            ("FOR UPDATE OF rel", lambda _sql, params: {"id": params["id"]}),
        ]
    )
    db = Database("postgresql://unused")
    extraction = {
        "entities": [
            {
                "id": "kg_ent_a",
                "name": "报告导出",
                "entity_type": "feature_ui_action",
                "evidence": [
                    _kg_evidence_from_text(source_text, source, "先检查权限。")
                ],
            }
        ],
        "relations": [],
    }

    db._replace_kg_source_snapshot_in_conn(
        conn,
        source_type="faq",
        source_ids=["faq_1"],
        source_chunk_ids=None,
        extraction=extraction,
    )

    candidate_index = next(
        index
        for index, (sql, params) in enumerate(conn.calls)
        if "INSERT INTO kg_entities" in sql and params["id"] == "kg_ent_a"
    )
    source_lock_index = next(
        index
        for index, (sql, params) in enumerate(conn.calls)
        if "FOR UPDATE OF ent" in sql and params["id"] == "kg_ent_z"
    )
    relation_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "FOR UPDATE OF rel" in sql
    )
    assert candidate_index < source_lock_index < relation_lock_index
    entity_update = next(
        params
        for sql, params in conn.calls
        if "UPDATE kg_entities entity" in sql
    )
    assert entity_update == {
        "entity_ids": ["kg_ent_a", "kg_ent_z"],
        "entity_revision_ids": ["kg_ent_z"],
    }


def test_relation_only_source_endpoints_are_locked_without_entity_invalidation():
    """relation-only 来源的端点属于 lock-only 集合，不得递增实体 revision 或修改实体投影。"""
    conn = _RecordingConnection(
        [
            ("SELECT DISTINCT evidence.entity_id AS id", []),
            (
                "SELECT DISTINCT rel.id, rel.head_entity_id",
                [
                    {
                        "id": "kg_rel_1",
                        "head_entity_id": "kg_ent_a",
                        "tail_entity_id": "kg_ent_b",
                    }
                ],
            ),
            ("FOR UPDATE OF ent", lambda _sql, params: {"id": params["id"]}),
            ("FOR UPDATE OF rel", lambda _sql, params: {"id": params["id"]}),
        ]
    )
    db = Database("postgresql://unused")

    db._reconcile_kg_source_change_in_conn(
        conn,
        source_type="faq",
        source_ids=["faq_1"],
    )

    entity_lock_ids = [
        params["id"]
        for sql, params in conn.calls
        if "FOR UPDATE OF ent" in sql
    ]
    assert entity_lock_ids == ["kg_ent_a", "kg_ent_b"]
    assert not any(sql.lstrip().startswith("UPDATE kg_entities") for sql, _params in conn.calls)
    assert not any(
        "source_type = 'kg_entity'" in sql
        for sql, _params in conn.calls
        if "UPDATE knowledge_chunks" in sql
    )
    relation_update = next(
        params
        for sql, params in conn.calls
        if "UPDATE kg_relations relation" in sql
    )
    assert relation_update == {
        "relation_ids": ["kg_rel_1"],
        "relation_revision_ids": ["kg_rel_1"],
    }


def test_replace_kg_source_snapshot_requires_every_relation_endpoint_candidate():
    """关系端点必须显式存在于同一 extraction 实体集合，不从旧库或 ID 推断补齐。"""
    conn = _RecordingConnection()
    db = Database("postgresql://unused")

    with pytest.raises(ValueError, match="endpoints must be present"):
        db._replace_kg_source_snapshot_in_conn(
            conn,
            source_type="faq",
            source_ids=["faq_1"],
            source_chunk_ids=None,
            extraction={
                "entities": [
                    {
                        "id": "kg_ent_head",
                        "name": "报告导出",
                        "entity_type": "feature_ui_action",
                        "evidence": [],
                    }
                ],
                "relations": [
                    {
                        "id": "kg_rel_1",
                        "head_entity_id": "kg_ent_head",
                        "relation_type": "requires",
                        "tail_entity_id": "kg_ent_missing",
                        "evidence": [],
                    }
                ],
            },
        )

    assert not any("INSERT INTO kg_entities" in sql for sql, _params in conn.calls)
    assert not any("INSERT INTO kg_relations" in sql for sql, _params in conn.calls)


def test_setting_entity_nonusable_cascades_usable_relations_and_projections():
    """实体退出 usable 时，引用它的可用关系及关系投影必须同步回待复核。"""
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF ent", {"id": "kg_ent_abc"}),
            ("ORDER BY rel.id ASC", []),
            ("UPDATE kg_entities", {"id": "kg_ent_abc", "status": "needs_review"}),
            ("SELECT EXISTS", {"exists": True}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.set_kg_entity_status("kg_ent_abc", "needs_review")

    cascade_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE kg_relations" in sql and "source_type = 'kg_relation'" in sql
    ]
    assert cascade_calls
    assert cascade_calls[0][1] == {"entity_ids": ["kg_ent_abc"]}


def test_setting_entity_nonusable_locks_entity_then_sorted_relations():
    """实体降级必须先锁实体、再按关系 ID 升序锁边，之后才执行状态与投影更新。"""
    entity = {"id": "kg_ent_abc", "status": "usable"}
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF ent", entity),
            ("ORDER BY rel.id ASC", [{"id": "kg_rel_a"}, {"id": "kg_rel_z"}]),
            ("UPDATE kg_entities", {"id": "kg_ent_abc", "status": "needs_review"}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.set_kg_entity_status("kg_ent_abc", "needs_review")

    entity_lock_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "FOR UPDATE OF ent" in sql
    )
    relation_lock_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "ORDER BY rel.id ASC" in sql
    )
    update_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "UPDATE kg_entities" in sql
    )
    assert entity_lock_index < relation_lock_index < update_index


def test_save_faq_text_invalidates_kg_in_same_transaction_when_content_changes():
    """FAQ 正文变化时，锁定旧行、写入和 KG 回退必须共用一个连接事务。"""
    existing = {
        "id": "faq_1",
        "embedding_text": "旧问题与旧答案",
        "embedding_status": "ready",
        "content_hash": "old-hash",
        "status": "usable",
    }
    saved = {
        "id": "faq_1",
        "embedding_text": "标准问题：新问题\n答案：新答案",
        "status": "usable",
    }
    conn = _RecordingConnection(
        [
            ("SELECT * FROM faq_documents", existing),
            ("INSERT INTO faq_documents", saved),
        ]
    )
    connect_count = 0

    def connect():
        """记录事务开启次数，内容读取、写入和 KG 回退必须只开启一次。"""
        nonlocal connect_count
        connect_count += 1
        return conn

    db = Database("postgresql://unused")
    db.connect = connect

    db.save_faq_text(
        {
            "id": "faq_1",
            "question": "新问题",
            "answer": "新答案",
            "status": "usable",
            "confidence": "high",
        }
    )

    assert connect_count == 1
    old_row_read_sql = next(
        sql for sql, _params in conn.calls if "SELECT * FROM faq_documents" in sql
    )
    assert "FOR UPDATE" in old_row_read_sql
    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    assert invalidations[0][1] == {
        "source_type": "faq",
        "source_ids": ["faq_1"],
        "source_chunk_ids": [],
    }
    evidence_deletions = _kg_source_evidence_deletion_calls(conn)
    assert evidence_deletions
    assert conn.calls.index(invalidations[0]) < conn.calls.index(evidence_deletions[0])
    stale_calls = _faq_projection_stale_calls(conn)
    assert stale_calls
    assert stale_calls[0][1] == {"source_id": "faq_1"}


def test_upsert_faq_invalidates_kg_when_existing_content_changes():
    """带向量 upsert 必须锁定旧 FAQ，并在同一事务撤销旧事实的 KG 结论。"""
    conn = _RecordingConnection(
        [
            (
                "SELECT * FROM faq_documents",
                {
                    "id": "faq_1",
                    "embedding_text": "旧内容",
                    "content_hash": "old-hash",
                    "status": "usable",
                },
            )
        ]
    )
    connect_count = 0

    def connect():
        """记录事务开启次数，旧行锁、FAQ 写入和 KG 回退必须只开启一次。"""
        nonlocal connect_count
        connect_count += 1
        return conn

    db = Database("postgresql://unused")
    db.connect = connect

    db.upsert_faq(
        {
            "id": "faq_1",
            "question": "新问题",
            "answer": "新答案",
            "confidence": "high",
            "status": "usable",
        },
        [0.1, 0.2],
        embedding_model="embedding-current",
        embedding_dimensions=2,
    )

    assert connect_count == 1
    old_row_read_sql = next(
        sql for sql, _params in conn.calls if "SELECT * FROM faq_documents" in sql
    )
    assert "FOR UPDATE" in old_row_read_sql
    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    assert invalidations[0][1]["source_ids"] == ["faq_1"]
    assert _kg_source_evidence_deletion_calls(conn)
    stale_calls = _faq_projection_stale_calls(conn)
    assert stale_calls
    assert stale_calls[0][1] == {"source_id": "faq_1"}


def test_upsert_faq_projects_ready_vector_in_the_same_transaction():
    """带向量 FAQ 写入必须原子生成统一投影，CLI 导入后无需第二条同步命令。"""
    conn = _RecordingConnection([("SELECT * FROM faq_documents", None)])
    connect_count = 0

    def connect():
        """记录事务次数，FAQ 与统一投影必须共用同一连接上下文。"""
        nonlocal connect_count
        connect_count += 1
        return conn

    db = Database("postgresql://unused")
    db.connect = connect

    db.upsert_faq(
        {
            "id": "faq_imported",
            "question": "如何导出报告？",
            "answer": "进入报告页后点击导出。",
            "confidence": "high",
            "status": "usable",
        },
        [0.1, 0.2],
        embedding_model="embedding-current",
        embedding_dimensions=2,
    )

    projection_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "INSERT INTO knowledge_chunks" in sql
    ]
    assert connect_count == 1
    assert len(projection_calls) == 1
    assert projection_calls[0][1]["source_type"] == "faq"
    assert projection_calls[0][1]["source_id"] == "faq_imported"
    assert projection_calls[0][1]["embedding"] == "[0.1,0.2]"
    assert projection_calls[0][1]["embedding_status"] == "ready"


def test_upsert_faq_requires_embedding_provider_metadata():
    """带向量 FAQ 必须显式记录模型和维度，不允许无元数据的 ready 写入路径。"""
    import inspect

    signature = inspect.signature(Database.upsert_faq)

    assert signature.parameters["embedding_model"].default is inspect.Parameter.empty
    assert signature.parameters["embedding_dimensions"].default is inspect.Parameter.empty


def test_update_faq_embedding_binds_content_hash_and_projects_atomically():
    """迟到 FAQ 向量只能提交到同一内容指纹，并在该事务内刷新统一投影。"""
    import inspect

    assert "expected_content_hash" in inspect.signature(
        Database.update_faq_embedding
    ).parameters
    embedding_text = "标准问题：当前问题\n答案：当前答案"
    content_hash = compute_content_hash({"embedding_text": embedding_text})
    updated = {
        "id": "faq_1",
        "question": "当前问题",
        "answer": "当前答案",
        "question_variants": [],
        "tags": [],
        "category": None,
        "evidence": [],
        "confidence": "high",
        "status": "usable",
        "embedding_text": embedding_text,
        "content_hash": content_hash,
        "embedding_status": "ready",
    }
    conn = _RecordingConnection([("UPDATE faq_documents", updated)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.update_faq_embedding(
        "faq_1",
        [0.3, 0.4],
        embedding_model="embedding-current",
        embedding_dimensions=2,
        expected_content_hash=content_hash,
    )

    update_sql, update_params = next(
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE faq_documents" in sql
    )
    projection_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "INSERT INTO knowledge_chunks" in sql
    ]
    assert result == updated
    assert "content_hash = %(expected_content_hash)s" in update_sql
    assert update_params["expected_content_hash"] == content_hash
    assert len(projection_calls) == 1
    assert projection_calls[0][1]["content_hash"] == content_hash
    assert projection_calls[0][1]["embedding"] == "[0.3,0.4]"


def test_prepare_faq_embedding_establishes_current_hash_for_stale_null_row():
    """正式 embedding 入口应锁定当前正文并建立指纹，使迁移后的 NULL hash 行可重建。"""
    assert hasattr(Database, "prepare_faq_embedding")
    embedding_text = "标准问题：当前问题\n答案：当前答案"
    expected_hash = compute_content_hash({"embedding_text": embedding_text})
    existing = {
        "id": "faq_legacy",
        "embedding_text": embedding_text,
        "embedding_status": "stale",
        "content_hash": None,
    }
    prepared = {**existing, "content_hash": expected_hash}
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF faq", existing),
            ("UPDATE faq_documents", prepared),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.prepare_faq_embedding("faq_legacy")

    lock_sql = next(sql for sql, _params in conn.calls if "FOR UPDATE OF faq" in sql)
    update_sql, update_params = next(
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE faq_documents" in sql
    )
    assert result["content_hash"] == expected_hash
    assert "content_hash" in lock_sql
    assert "content_hash = %(content_hash)s" in update_sql
    assert update_params == {"id": "faq_legacy", "content_hash": expected_hash}


def test_mark_embedding_failed_is_bound_to_the_prepared_content_hash():
    """provider 失败只能写回本次准备的 FAQ 版本，不能污染并发保存的新正文。"""
    import inspect

    assert "expected_content_hash" in inspect.signature(
        Database.mark_embedding_failed
    ).parameters
    failed = {
        "id": "faq_1",
        "content_hash": "hash-current",
        "embedding_status": "failed",
        "embedding_error": "provider timeout",
    }
    conn = _RecordingConnection([("UPDATE faq_documents", failed)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.mark_embedding_failed(
        "faq_1",
        "provider timeout",
        expected_content_hash="hash-current",
    )

    sql, params = next(
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE faq_documents" in sql
    )
    assert result == failed
    assert "content_hash = %(expected_content_hash)s" in sql
    assert params["expected_content_hash"] == "hash-current"


def test_save_faq_text_marks_missing_hash_projection_stale():
    """ready FAQ 缺少当前指纹时必须重建，不用正文相等推测旧向量可用。"""
    row = {
        "id": "faq_1",
        "question": "原问题",
        "answer": "原答案",
        "status": "usable",
        "confidence": "high",
    }
    embedding_text = build_embedding_text(row)
    existing = {
        "id": "faq_1",
        "embedding_text": embedding_text,
        "embedding_status": "ready",
        "content_hash": None,
        "status": "usable",
    }
    conn = _RecordingConnection(
        [
            ("SELECT * FROM faq_documents", existing),
            ("INSERT INTO faq_documents", {**existing, "content_hash": "new-hash"}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.save_faq_text(row)

    save_payload = next(
        params for sql, params in conn.calls if "INSERT INTO faq_documents" in sql
    )
    assert save_payload["embedding_status"] == "stale"
    assert _faq_projection_stale_calls(conn)
    assert _kg_source_invalidation_calls(conn)
    assert _kg_source_evidence_deletion_calls(conn)


def test_update_faq_statuses_invalidates_kg_when_source_becomes_nonusable():
    """FAQ 批量转为待复核或禁用时，相关 KG 状态必须在同一事务回退。"""
    conn = _RecordingConnection(
        [
            (
                "FROM faq_documents faq",
                [{"id": "faq_1", "status": "usable"}],
            ),
            (
                "UPDATE faq_documents",
                [{"id": "faq_1", "status": "disabled"}],
            )
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    rows = db.update_faq_statuses(["faq_1"], "disabled")

    assert rows == [{"id": "faq_1", "status": "disabled"}]
    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    assert invalidations[0][1]["source_ids"] == ["faq_1"]
    assert _kg_source_evidence_deletion_calls(conn) == []


def test_update_faq_statuses_syncs_projection_when_source_becomes_usable():
    """FAQ 从非 usable 恢复时必须保留 evidence，并进入唯一 KG 实时重算入口。"""
    conn = _RecordingConnection(
        [
            (
                "FROM faq_documents faq",
                [{"id": "faq_1", "status": "needs_review"}],
            ),
            (
                "UPDATE faq_documents",
                [{"id": "faq_1", "status": "usable"}],
            )
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    rows = db.update_faq_statuses(["faq_1"], "usable")

    assert rows == [{"id": "faq_1", "status": "usable"}]
    status_calls = _faq_projection_status_calls(conn)
    assert len(status_calls) == 1
    assert status_calls[0][1] == {
        "source_ids": ["faq_1"],
        "status": "usable",
    }
    reconciliations = _kg_source_invalidation_calls(conn)
    assert len(reconciliations) == 1
    assert reconciliations[0][1] == {
        "source_type": "faq",
        "source_ids": ["faq_1"],
        "source_chunk_ids": [],
    }
    assert _kg_source_evidence_deletion_calls(conn) == []


def test_save_faq_text_syncs_projection_status_when_content_is_unchanged():
    """只变审核状态时复用当前向量，但必须同步投影状态而非保留 needs_review。"""
    row = {
        "id": "faq_1",
        "question": "原问题",
        "answer": "原答案",
        "status": "usable",
        "confidence": "high",
    }
    embedding_text = build_embedding_text(row)
    content_hash = compute_content_hash({"embedding_text": embedding_text})
    existing = {
        "id": "faq_1",
        "embedding_text": embedding_text,
        "embedding_status": "ready",
        "content_hash": content_hash,
        "status": "needs_review",
    }
    saved = {**existing, "status": "usable"}
    conn = _RecordingConnection(
        [
            ("SELECT * FROM faq_documents", existing),
            ("INSERT INTO faq_documents", saved),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.save_faq_text(row)

    assert result == saved
    assert not _faq_projection_stale_calls(conn)
    sync_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE knowledge_chunks" in sql and "metadata = %(metadata)s::jsonb" in sql
    ]
    assert len(sync_calls) == 1
    assert sync_calls[0][1]["source_id"] == "faq_1"
    assert sync_calls[0][1]["status"] == "usable"
    assert "embedding_status =" not in sync_calls[0][0]


def test_save_faq_text_syncs_projection_metadata_without_replacing_embedding():
    """FAQ 向量文本未变时同步来源元数据和置信度，同时保留现有向量。"""
    row = {
        "id": "faq_1",
        "question": "原问题",
        "answer": "原答案",
        "status": "usable",
        "confidence": "medium",
        "source_file": "manual-v2.md",
        "source_group": "客服二组",
        "source_date": "2026-07-15",
        "evidence": [{"excerpt": "更新后的证据"}],
    }
    embedding_text = build_embedding_text(row)
    content_hash = compute_content_hash({"embedding_text": embedding_text})
    existing = {
        "id": "faq_1",
        "embedding_text": embedding_text,
        "embedding_status": "ready",
        "content_hash": content_hash,
        "status": "usable",
    }
    conn = _RecordingConnection(
        [
            ("SELECT * FROM faq_documents", existing),
            ("INSERT INTO faq_documents", {**existing, **row}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.save_faq_text(row)

    sync_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "UPDATE knowledge_chunks" in sql and "metadata = %(metadata)s::jsonb" in sql
    ]
    assert len(sync_calls) == 1
    sync_sql, sync_params = sync_calls[0]
    assert "embedding =" not in sync_sql
    assert "embedding_status =" not in sync_sql
    assert sync_params["confidence"] == "medium"
    assert json.loads(sync_params["metadata"])["source_file"] == "manual-v2.md"
    assert json.loads(sync_params["metadata"])["evidence"] == [
        {"excerpt": "更新后的证据"}
    ]


def test_delete_import_file_invalidates_document_kg_before_source_deletion():
    """删除文档按 file→chunks→parse jobs 锁序回退 KG 并级联删除。"""
    conn = _RecordingConnection(
        [
            ("FROM import_parse_jobs", []),
            ("FROM import_files imp", {"id": "imp_1"}),
            ("SELECT count(*)", {"c": 2}),
            ("DELETE FROM knowledge_chunks", [{"id": "kc_1"}]),
            ("DELETE FROM import_files", {"id": "imp_1", "original_name": "手册.pdf"}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.delete_import_file("imp_1")

    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    invalidation_index = conn.calls.index(invalidations[0])
    parse_job_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "FROM import_parse_jobs" in sql and "FOR UPDATE OF job" in sql
    )
    file_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "FROM import_files imp" in sql and "FOR UPDATE OF imp" in sql
    )
    chunk_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "chunk.file_id = %(source_id)s" in sql and "FOR UPDATE OF chunk" in sql
    )
    deletion_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "DELETE FROM import_files" in sql
    )
    assert file_lock_index < chunk_lock_index < parse_job_lock_index < invalidation_index
    assert invalidation_index < deletion_index
    assert invalidations[0][1]["source_ids"] == ["imp_1"]
    assert _kg_source_evidence_deletion_calls(conn)


def test_replace_import_chunks_invalidates_document_kg_before_reparse_cleanup():
    """重新解析替换切片前必须回退旧文档证据形成的 KG 事实。"""
    conn = _RecordingConnection([("FROM import_files imp", {"id": "imp_1"})])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.replace_import_chunks("imp_1", [])

    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    invalidation_index = conn.calls.index(invalidations[0])
    file_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "FROM import_files imp" in sql and "FOR UPDATE OF imp" in sql
    )
    chunk_lock_index = next(
        index
        for index, (sql, _params) in enumerate(conn.calls)
        if "chunk.file_id = %(source_id)s" in sql and "FOR UPDATE OF chunk" in sql
    )
    chunk_delete_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "DELETE FROM import_chunks" in sql
    )
    assert file_lock_index < chunk_lock_index < invalidation_index
    assert invalidation_index < chunk_delete_index
    assert invalidations[0][1]["source_ids"] == ["imp_1"]
    assert _kg_source_evidence_deletion_calls(conn)


def test_disabling_import_file_invalidates_document_kg():
    """文件被禁用后其证据不再有效，相关 KG 必须立即回待复核。"""
    conn = _RecordingConnection(
        [
            ("FROM import_files imp", {"id": "imp_1", "is_disabled": False}),
            ("UPDATE import_files", {"id": "imp_1", "is_disabled": True}),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.set_import_file_disabled("imp_1", True)

    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    assert invalidations[0][1]["source_ids"] == ["imp_1"]
    assert _kg_source_evidence_deletion_calls(conn) == []


def test_disabling_import_chunk_invalidates_only_that_chunk_kg_evidence():
    """切片被禁用时只回退该文件该切片派生的 KG，不扩大到同文件其它切片。"""
    conn = _RecordingConnection(
        [
            (
                "FROM import_chunks chunk",
                {"id": "chunk_1", "file_id": "imp_1", "is_disabled": False},
            ),
            (
                "UPDATE import_chunks",
                {"id": "chunk_1", "file_id": "imp_1", "is_disabled": True},
            )
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.set_import_chunk_disabled("chunk_1", True)

    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    assert invalidations[0][1] == {
        "source_type": "document",
        "source_ids": ["imp_1"],
        "source_chunk_ids": ["chunk_1"],
    }


def test_set_import_chunk_questions_marks_all_source_knowledge_stale_when_changed():
    """假设问题实际变化时，同一来源切片的 parent/child 必须在事务内全部 stale。"""
    conn = _RecordingConnection(
        [
            (
                "FOR UPDATE OF chunk",
                {"id": "chunk_1", "file_id": "imp_1", "questions": ["旧问题"]},
            ),
            (
                "UPDATE import_chunks",
                {"id": "chunk_1", "file_id": "imp_1", "questions": ["新问题"]},
            ),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.set_import_chunk_questions(
        "chunk_1",
        ["新问题"],
        model="question-model",
        status="ready",
    )

    lock_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "FOR UPDATE OF chunk" in sql
    )
    update_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "UPDATE import_chunks" in sql
    )
    stale_index, (stale_sql, stale_params) = next(
        (index, call)
        for index, call in enumerate(conn.calls)
        if "UPDATE knowledge_chunks" in call[0] and "embedding_status = 'stale'" in call[0]
    )
    assert lock_index < update_index < stale_index
    assert stale_params == {"source_id": "imp_1", "source_chunk_id": "chunk_1"}
    assert "source_type = 'document'" in stale_sql
    assert "source_id = %(source_id)s" in stale_sql
    assert "source_chunk_id = %(source_chunk_id)s" in stale_sql
    assert "content =" not in stale_sql
    assert "embedding_text =" not in stale_sql
    assert "search_text =" not in stale_sql
    assert "content_hash =" not in stale_sql


def test_set_import_chunk_questions_status_change_preserves_ready_knowledge():
    """问题列表不变时，pending/model/error 等状态更新不得让现有知识行 stale。"""
    existing = {"id": "chunk_1", "file_id": "imp_1", "questions": ["已有问题"]}
    updated = {
        **existing,
        "questions_status": "failed",
        "questions_model": "new-model",
        "questions_error": "provider timeout",
    }
    conn = _RecordingConnection(
        [
            ("FOR UPDATE OF chunk", existing),
            ("UPDATE import_chunks", updated),
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    row = db.set_import_chunk_questions(
        "chunk_1",
        ["已有问题"],
        model="new-model",
        status="failed",
        error="provider timeout",
    )

    assert row == updated
    assert any("UPDATE import_chunks" in sql for sql, _params in conn.calls)
    assert not any(
        "UPDATE knowledge_chunks" in sql and "embedding_status = 'stale'" in sql
        for sql, _params in conn.calls
    )


def test_update_import_chunk_text_invalidates_kg_evidence_for_changed_chunk():
    """人工修改切片正文后，引用旧正文的 KG 事实必须回待复核。"""
    conn = _RecordingConnection(
        [
            (
                "FOR UPDATE OF chunk",
                {"id": "chunk_1", "file_id": "imp_1", "source_text": "旧正文"},
            ),
            (
                "UPDATE import_chunks",
                {"id": "chunk_1", "file_id": "imp_1", "source_text": "新正文"},
            )
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.update_import_chunk_text("chunk_1", "新正文")

    invalidations = _kg_source_invalidation_calls(conn)
    assert invalidations
    assert invalidations[0][1]["source_chunk_ids"] == ["chunk_1"]
    evidence_deletions = _kg_source_evidence_deletion_calls(conn)
    assert evidence_deletions
    assert evidence_deletions[0][1]["source_chunk_ids"] == ["chunk_1"]
    projection_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "knowledge_chunks" in sql
        and "source_chunk_id = %(chunk_id)s" in sql
        and ("DELETE FROM" in sql or "embedding_status = 'stale'" in sql)
    ]
    assert len(projection_calls) == 2
    assert all(call[1]["source_id"] == "imp_1" for call in projection_calls)


def test_update_import_chunk_text_noop_preserves_kg_evidence_and_vectors():
    """切片正文未变化时保存必须是 no-op，不回退 KG、删证据或标记向量 stale。"""
    existing = {"id": "chunk_1", "file_id": "imp_1", "source_text": "原正文"}
    conn = _RecordingConnection([("FOR UPDATE OF chunk", existing)])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    row = db.update_import_chunk_text("chunk_1", "原正文")

    assert row == existing
    assert _kg_source_invalidation_calls(conn) == []
    assert _kg_source_evidence_deletion_calls(conn) == []
    assert not any("UPDATE import_chunks" in sql for sql, _params in conn.calls)
    assert not any("DELETE FROM knowledge_chunks" in sql for sql, _params in conn.calls)
    assert not any("embedding_status = 'stale'" in sql for sql, _params in conn.calls)


def test_database_has_no_faq_projection_backfill_entrypoint():
    """FAQ 正常写入已原子投影，数据库不得再暴露旧 backfill 第二路径。"""
    assert not hasattr(Database, "sync_ready_faq_knowledge_chunks")
    assert not hasattr(Database, "_sync_ready_faq_knowledge_chunks_sql")


def test_schema_marks_ready_faq_without_current_hash_stale():
    """初始化 SQL 必须让缺指纹的 ready FAQ 退出检索，不留旧行兼容分支。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    normalized = " ".join(schema.split())

    assert "UPDATE faq_documents SET embedding_status = 'stale'" in normalized
    assert "embedding_status = 'ready' AND content_hash IS NULL" in normalized


def test_schema_marks_ready_faq_without_current_projection_stale():
    """删除运行时 backfill 后，初始化 SQL 必须把缺 canonical 投影的 ready FAQ 送回重建队列。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    normalized = " ".join(schema.split())

    assert "UPDATE faq_documents AS faq SET embedding_status = 'stale'" in normalized
    assert "faq.embedding_status = 'ready' AND NOT EXISTS" in normalized
    assert "projection.source_type = 'faq'" in normalized
    assert "projection.source_id = faq.id" in normalized
    assert "projection.content_hash = faq.content_hash" in normalized
    assert "projection.embedding_status = 'ready'" in normalized


def test_schema_tracks_one_review_revision_for_each_kg_candidate():
    """实体和关系必须持久化审核 revision，旧页面不能确认未看过的新快照。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert schema.count("review_revision BIGINT NOT NULL DEFAULT 1") >= 2
    assert "ALTER TABLE kg_entities" in schema
    assert "ALTER TABLE kg_relations" in schema
    assert schema.count("ADD COLUMN IF NOT EXISTS review_revision") >= 2


def test_kg_snapshot_changes_increment_review_revision():
    """重抽取、来源失效和人工降级都必须让旧审核 revision 立即过期。"""
    assert "review_revision = kg_entities.review_revision + 1" in (
        Database._upsert_kg_entity_sql()
    )
    assert "review_revision = kg_relations.review_revision + 1" in (
        Database._upsert_kg_relation_sql()
    )
    assert "review_revision = review_revision + 1" in (
        Database._set_kg_entity_status_sql()
    )
    assert "review_revision = review_revision + 1" in (
        Database._set_kg_relation_status_sql()
    )
    assert "review_revision = review_revision + 1" in (
        Database._invalidate_kg_entity_relations_sql()
    )
    entity_reconcile_sql = Database._reconcile_kg_entities_after_evidence_change_sql()
    relation_reconcile_sql = Database._reconcile_kg_relations_after_evidence_change_sql()
    assert "review_revision = entity.review_revision + CASE" in entity_reconcile_sql
    assert "%(entity_revision_ids)s::text[]" in entity_reconcile_sql
    assert "review_revision = relation.review_revision + CASE" in relation_reconcile_sql
    assert "%(relation_revision_ids)s::text[]" in relation_reconcile_sql


def test_schema_aligns_existing_faq_projection_review_status():
    """初始化 SQL 必须让已有 FAQ 投影状态与实时审核状态一致，避免 KG 证据漏召回。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    normalized = " ".join(schema.split())

    assert "status = expected.status" in normalized
    assert "projection.source_type = 'faq'" in normalized
    assert "projection.source_id = expected.source_id" in normalized
    assert "projection.status IS DISTINCT FROM expected.status" in normalized


def test_schema_aligns_existing_current_faq_projection_nonvector_fields():
    """同指纹 FAQ 历史投影必须幂等同步来源元数据和置信度，同时保留现有向量。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    normalized = " ".join(schema.split())
    backfill = normalized.split("WITH expected_faq_projection_fields AS", 1)[1].split(
        "-- ready FAQ", 1
    )[0]

    assert "metadata = expected.metadata" in backfill
    assert "'evidence', faq.evidence" in backfill
    assert "'source_file', faq.source_file" in normalized
    assert "'source_group', faq.source_group" in normalized
    assert "'source_date', faq.source_date" in normalized
    assert "confidence = expected.confidence" in backfill
    assert "projection.metadata IS DISTINCT FROM expected.metadata" in backfill
    assert "projection.chunk_index = 0" in backfill
    assert "projection.chunk_level = 'chunk'" in backfill
    assert "projection.source_chunk_id IS NULL" in backfill
    assert "projection.parent_chunk_id IS NULL" in backfill
    assert "embedding =" not in backfill
    assert "embedding_status =" not in backfill
    assert "content_hash =" not in backfill


def test_search_knowledge_sql_reads_unified_chunks_without_confidence_filter():
    """智能问答检索应读取统一知识单元，允许文档切片这类无 confidence 来源参与。"""
    sql = Database._search_knowledge_sql()

    assert "FROM knowledge_chunks" in sql
    assert "source_type" in sql
    assert "parent_chunk_id" in sql
    assert "chunk_level" in sql
    assert "section_path" in sql
    assert "content" in sql
    assert "metadata" in sql
    assert "embedding_status = 'ready'" in sql
    assert "COALESCE(fq.status, kc.status)" not in sql
    assert "confidence = %(confidence)s" not in sql


def test_unified_search_requires_ready_projection_and_live_ready_faq():
    """向量和关键词两路都必须排除 stale 投影，FAQ 还要服从实时状态与向量状态。"""
    for sql in (Database._search_knowledge_sql(), Database._search_knowledge_text_sql()):
        assert "kc.embedding_status = 'ready'" in sql
        assert "kc.source_type = 'faq'" in sql
        assert "fq.status = %(status)s" in sql
        assert "fq.embedding_status = 'ready'" in sql
        assert "kc.source_type = 'document'" in sql
        assert "kc.status = %(status)s" in sql
        assert "COALESCE(fq.status, kc.status)" not in sql


def test_search_knowledge_sql_filters_disabled_files_and_chunks():
    """向量检索必须精确关联 live 文件/切片并过滤禁用项。"""
    sql = Database._search_knowledge_sql()

    assert "LEFT JOIN import_files imp" in sql
    assert "LEFT JOIN import_chunks ic" in sql
    assert "imp.id IS NOT NULL" in sql
    assert "ic.id IS NOT NULL" in sql
    assert "imp.is_disabled = false" in sql
    assert "ic.is_disabled = false" in sql


def test_search_knowledge_sql_requires_document_child_for_direct_retrieval():
    """文档直召回只能接受当前 child，旧 chunk 与 parent 层级都必须排除。"""
    sql = Database._search_knowledge_sql()

    assert "(kc.source_type <> 'document' OR kc.chunk_level = 'child')" in sql
    assert "kc.chunk_level <> 'parent'" not in sql


def test_search_knowledge_text_sql_filters_disabled_files_and_chunks():
    """关键词检索同样要精确关联 live 文件/切片并过滤禁用项。"""
    sql = Database._search_knowledge_text_sql()

    assert "LEFT JOIN import_files imp" in sql
    assert "LEFT JOIN import_chunks ic" in sql
    assert "imp.id IS NOT NULL" in sql
    assert "ic.id IS NOT NULL" in sql
    assert "imp.is_disabled = false" in sql
    assert "ic.is_disabled = false" in sql


def test_search_knowledge_text_sql_requires_document_child_for_direct_retrieval():
    """关键词直召回同样只允许当前 document child，不读取旧层级形状。"""
    sql = Database._search_knowledge_text_sql()

    assert "(kc.source_type <> 'document' OR kc.chunk_level = 'child')" in sql
    assert "kc.chunk_level <> 'parent'" not in sql


def test_get_parent_context_chunks_sql_filters_disabled_files_and_chunks():
    """parent 回填也必须精确关联 live 文件/切片并排除禁用来源。"""
    sql = Database._get_parent_context_chunks_sql()

    assert "LEFT JOIN import_files imp" in sql
    assert "LEFT JOIN import_chunks ic" in sql
    assert "imp.id IS NOT NULL" in sql
    assert "ic.id IS NOT NULL" in sql
    assert "imp.is_disabled = false" in sql
    assert "ic.is_disabled = false" in sql


def test_unified_retrieval_requires_exact_live_document_sources():
    """正式候选只允许 FAQ/document，文档三路查询都必须精确关联仍存在的文件和切片。"""
    for sql in (Database._search_knowledge_sql(), Database._search_knowledge_text_sql()):
        assert "kc.source_type IN ('faq', 'document')" in sql
        assert "ic.file_id = kc.source_id" in sql
        assert "imp.id IS NOT NULL" in sql
        assert "ic.id IS NOT NULL" in sql

    parent_sql = Database._get_parent_context_chunks_sql()
    assert "ic.file_id = parent.source_id" in parent_sql
    assert "imp.id IS NOT NULL" in parent_sql
    assert "ic.id IS NOT NULL" in parent_sql


def test_import_files_schema_supports_disabled_toggle():
    """import_files / import_chunks 必须各带 is_disabled 列，提供文件级 / 切片级开关。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "ALTER TABLE import_files" in schema
    assert "ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT false" in schema
    # 一次出现已覆盖 import_files；保证 import_chunks 也含同列
    assert schema.count("is_disabled BOOLEAN NOT NULL DEFAULT false") >= 2


def test_import_files_schema_persists_document_chunker_type():
    """import_files 必须持久化 chunker_type，让文件级后解析路线可追溯。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS chunker_type TEXT NOT NULL DEFAULT 'naive'" in schema


def test_import_files_schema_hard_cuts_legacy_parse_runtime_columns():
    """import_files 当前契约不得保留旧解析字段，仅允许幂等 DROP 迁移提及。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    create_block = schema.split("CREATE TABLE IF NOT EXISTS import_files (", 1)[
        1
    ].split(");", 1)[0]
    migration_block = schema.split("ALTER TABLE import_files", 1)[1].split(
        "CREATE TABLE IF NOT EXISTS import_parse_jobs",
        1,
    )[0]

    for field in ("parse_batch_id", "parse_file_name", "parse_progress"):
        assert field not in create_block
        assert f"ADD COLUMN IF NOT EXISTS {field}" not in migration_block
        assert f"DROP COLUMN IF EXISTS {field}" in migration_block
    assert "import_files_parse_progress_object_check" not in schema


def test_search_knowledge_text_sql_reads_keyword_fields():
    """关键词召回应读取统一知识单元的 search_text、标题和正文。"""
    sql = Database._search_knowledge_text_sql()

    assert "FROM knowledge_chunks" in sql
    assert "parent_chunk_id" in sql
    assert "chunk_level" in sql
    assert "section_path" in sql
    assert "unnest(%(query_terms)s::text[])" in sql
    assert "source_title ILIKE %(query_like)s" in sql
    assert "content ILIKE %(query_like)s" in sql
    assert "search_text ILIKE %(query_like)s" in sql
    assert "source_title ILIKE ('%%' || term || '%%')" in sql
    assert "ORDER BY score DESC" in sql


def test_search_knowledge_text_sql_escapes_percent_literals_for_psycopg():
    """关键词 SQL 里的 LIKE 百分号必须转义，避免 psycopg 误判为占位符。"""
    sql = Database._search_knowledge_text_sql()

    assert "('%%' || term || '%%')" in sql
    assert "('%' || term || '%')" not in sql


def test_retrieval_alias_schema_records_canonical_terms_and_aliases():
    """检索别名词典需要记录标准词、别名和启用状态。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS retrieval_aliases" in schema
    assert "canonical TEXT NOT NULL" in schema
    assert "aliases JSONB NOT NULL DEFAULT '[]'::jsonb" in schema
    assert "retrieval_aliases_status_idx" in schema


def test_retrieval_eval_schema_records_expected_hits_and_runs():
    """检索评测表需要保存测试问题、期望命中和每次运行结果。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS retrieval_eval_cases" in schema
    assert "expected_source_ids JSONB NOT NULL DEFAULT '[]'::jsonb" in schema
    assert "expected_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb" in schema
    assert "CREATE TABLE IF NOT EXISTS retrieval_eval_runs" in schema
    assert "metrics JSONB NOT NULL DEFAULT '{}'::jsonb" in schema


def test_retrieval_eval_schema_deletes_runs_outside_current_contract():
    """schema 重跑只保留 JSONB 数字版 contract_version=2。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    compact_schema = "".join(schema.split())

    assert "DELETEFROMretrieval_eval_runs" in compact_schema
    assert "analysis->'contract_version'ISDISTINCTFROM'2'::jsonb" in compact_schema
    assert "analysis->>'contract_version'" not in schema
    assert "UPDATEretrieval_eval_runs" not in compact_schema
    assert "jsonb_array_elements(run.retrieved_items)" not in schema


def test_retrieval_eval_schema_removes_synthetic_kg_expected_ids_by_exact_relations():
    """期望 ID 清理必须查真实 KG 关系，禁止用易误伤业务 ID 的前缀规则。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    cleanup_updates = re.findall(
        r"UPDATE\s+retrieval_eval_cases\b.*?;",
        schema,
        flags=re.IGNORECASE | re.DOTALL,
    )

    assert cleanup_updates, "schema must clean synthetic KG expectations"
    cleanup_sql = "\n".join(cleanup_updates)
    compact_cleanup = "".join(cleanup_sql.split())
    chunk_cleanup_sql = "\n".join(
        statement for statement in cleanup_updates if "expected_chunk_ids" in statement
    )
    source_cleanup_sql = "\n".join(
        statement for statement in cleanup_updates if "expected_source_ids" in statement
    )
    assert chunk_cleanup_sql
    assert "jsonb_array_elements_text" in cleanup_sql
    assert re.search(
        r"\bknowledge_chunks\s+(?:AS\s+)?(?P<chunk_alias>\w+)"
        r".*?(?P=chunk_alias)\.id\s*=\s*\w+\.value\b",
        chunk_cleanup_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "source_typeIN('kg_entity','kg_relation')" in compact_cleanup
    assert source_cleanup_sql
    assert re.search(
        r"\bkg_entities\s+(?:AS\s+)?(?P<entity_alias>\w+)"
        r".*?(?P=entity_alias)\.id\s*=\s*\w+\.value\b",
        source_cleanup_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert re.search(
        r"\bkg_relations\s+(?:AS\s+)?(?P<relation_alias>\w+)"
        r".*?(?P=relation_alias)\.id\s*=\s*\w+\.value\b",
        source_cleanup_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "LIKE" not in cleanup_sql.upper()
    assert "STARTS_WITH" not in cleanup_sql.upper()


def test_list_retrieval_eval_cases_includes_latest_runs_by_current_strategy():
    """评测列表按固定策略顺序返回各自最新运行，不再暴露 singular latest_run。"""

    class _FakeConn:
        """记录列表 SQL，并返回包含两种 current strategy 的当前契约快照。"""

        def __init__(self):
            """初始化 SQL 调用记录。"""
            self.calls = []

        def execute(self, sql, params=None):
            """记录 SQL 与参数，模拟连接游标链式接口。"""
            self.calls.append((sql, params or {}))
            return self

        def fetchall(self):
            """按 baseline、KG debug 固定顺序返回每策略最新一条。"""
            return [
                {
                    "id": "eval_1",
                    "question": "报告导出失败怎么办？",
                    "latest_runs": [
                        {
                            "id": "eval_run_baseline_new",
                            "strategy": "retrieval_hybrid_v1",
                        },
                        {
                            "id": "eval_run_kg_new",
                            "strategy": "retrieval_hybrid_v1_kg_debug",
                        },
                    ],
                }
            ]

        def fetchone(self):
            """返回用例总数。"""
            return {"total": 1}

        def __enter__(self):
            """进入模拟事务。"""
            return self

        def __exit__(self, exc_type, exc, tb):
            """退出模拟事务且不吞异常。"""
            return False

    conn = _FakeConn()
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.list_retrieval_eval_cases(status="active", limit=20, offset=0)

    item = result["items"][0]
    assert [run["id"] for run in item["latest_runs"]] == [
        "eval_run_baseline_new",
        "eval_run_kg_new",
    ]
    assert "latest_run" not in item
    rows_sql = conn.calls[0][0]
    assert "LEFT JOIN LATERAL" in rows_sql
    assert "FROM retrieval_eval_runs" in rows_sql
    assert "run.case_id = c.id" in rows_sql
    assert re.search(
        r"(?:DISTINCT\s+ON|PARTITION\s+BY)[^\n]*strategy",
        rows_sql,
        flags=re.IGNORECASE,
    )
    assert "run.created_at DESC" in rows_sql
    assert "retrieval_hybrid_v1" in rows_sql
    assert "retrieval_hybrid_v1_kg_debug" in rows_sql
    assert re.search(r"run\.strategy\s+(?:IN|=\s*ANY)", rows_sql, flags=re.IGNORECASE)
    assert re.search(
        r"CASE\b.*?'retrieval_hybrid_v1'.*?'retrieval_hybrid_v1_kg_debug'.*?END",
        rows_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "COALESCE" in rows_sql
    assert "'[]'::jsonb" in rows_sql
    assert "AS latest_runs" in rows_sql
    assert not re.search(r"\blatest\.latest_run\b", rows_sql)
    assert "run.analysis->'contract_version' = '2'::jsonb" in rows_sql
    assert "run.analysis->>'contract_version'" not in rows_sql
    assert "jsonb_agg" in rows_sql
    assert "jsonb_build_object" in rows_sql
    assert conn.calls[0][1] == {"status": "active", "limit": 20, "offset": 0}


@pytest.mark.parametrize(
    "row",
    [
        {"case_id": "eval_1", "analysis": {"contract_version": 2}},
        {
            "case_id": "eval_1",
            "strategy": "retrieval_hybrid_v0",
            "analysis": {"contract_version": 2},
        },
    ],
)
def test_record_retrieval_eval_run_requires_explicit_current_strategy(row):
    """评测运行必须显式使用两种 current strategy 之一，不能默认或接收任意值。"""
    conn = _RecordingConnection()
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(ValueError, match="strategy"):
        db.record_retrieval_eval_run(row)

    assert conn.calls == []


@pytest.mark.parametrize("contract_version", ["2", 2.0, True, None])
def test_record_retrieval_eval_run_requires_exact_integer_contract_version(
    contract_version,
):
    """评测运行只接受整数 2，不容忍文本或数值类型推测。"""
    conn = _RecordingConnection()
    db = Database("postgresql://unused")
    db.connect = lambda: conn
    row = {
        "case_id": "eval_1",
        "strategy": "retrieval_hybrid_v1",
        "retrieved_items": [],
        "metrics": {},
        "analysis": {"contract_version": contract_version},
    }

    with pytest.raises(ValueError, match="contract_version"):
        db.record_retrieval_eval_run(row)

    assert conn.calls == []


def test_import_file_embedding_summaries_sql_counts_document_chunks():
    """文档摘要 SQL 只返回每片事实计数，child 预期数由 Python 唯一算法计算。"""
    sql = Database._import_file_embedding_summaries_sql()

    assert "FROM import_chunks" in sql
    assert "JOIN knowledge_chunks" in sql
    assert "source_type = 'document'" in sql
    assert "source_text" in sql
    assert "source_blocks" in sql
    assert "children_delimiter" in sql
    assert "COALESCE(is_disabled, false) = false" in sql
    assert "expected_knowledge_count" not in sql
    assert "jsonb_array_elements" not in sql
    assert "ready_count" in sql
    assert "stale_count" in sql
    assert "failed_count" in sql
    assert "pending_count" in sql


def test_import_file_embedding_summary_prefers_structured_block_count():
    """结构块与 delimiter 同时存在时，摘要必须按结构块优先口径计算预期行数。"""
    conn = _RecordingConnection(
        [
            (
                "FROM import_chunks",
                [
                    {
                        "file_id": "imp_1",
                        "chunk_id": "chunk_1",
                        "source_text": "第一问\n第二问",
                        "source_blocks": [{"text": "第一问\n第二问"}],
                        "children_delimiter": r"\n",
                        "knowledge_count": 2,
                        "ready_count": 2,
                        "stale_count": 0,
                        "failed_count": 0,
                        "pending_count": 0,
                    }
                ],
            )
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    summary = db.get_import_file_embedding_summary("imp_1")

    assert summary == {
        "status": "ready",
        "total_chunks": 2,
        "knowledge_count": 2,
        "ready_count": 2,
        "stale_count": 0,
        "failed_count": 0,
        "pending_count": 0,
        "missing_count": 0,
    }


def test_import_chunks_schema_preserves_parser_structure_for_retrieval():
    """导入切片表需要保存解析器给出的章节、页码和块类型，供后续生成父子知识单元。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    insert_sql = Database._insert_import_chunk_sql()

    assert "section_path JSONB NOT NULL DEFAULT '[]'::jsonb" in schema
    assert "page_start INTEGER" in schema
    assert "page_end INTEGER" in schema
    assert "block_type TEXT" in schema
    assert "source_offsets JSONB NOT NULL DEFAULT '{}'::jsonb" in schema
    assert "source_blocks JSONB NOT NULL DEFAULT '[]'::jsonb" in schema
    assert "children_delimiter TEXT NOT NULL DEFAULT ''" in schema
    assert "DROP COLUMN IF EXISTS parent_chunk_id" in schema
    assert "DROP COLUMN IF EXISTS chunk_level" in schema
    assert "section_path" in insert_sql
    assert "page_start" in insert_sql
    assert "page_end" in insert_sql
    assert "block_type" in insert_sql
    assert "source_blocks" in insert_sql
    assert "children_delimiter" in insert_sql


def test_update_import_chunk_text_sql_marks_existing_knowledge_chunk_stale():
    """切片原文保存后，应删除旧 child 并只把 parent 知识单元标记为 stale。"""
    update_sql = Database._update_import_chunk_text_sql()
    delete_sql = Database._delete_document_chunk_child_knowledge_sql()
    stale_sql = Database._mark_document_chunk_knowledge_stale_sql()

    assert "UPDATE import_chunks" in update_sql
    assert "source_text = %(source_text)s" in update_sql
    assert "source_blocks = '[]'::jsonb" in update_sql
    assert "DELETE FROM knowledge_chunks" in delete_sql
    assert "source_id = %(source_id)s" in delete_sql
    assert "source_chunk_id = %(chunk_id)s" in delete_sql
    assert "chunk_level = 'child'" in delete_sql
    assert "parent_chunk_id =" not in delete_sql
    assert "UPDATE knowledge_chunks" in stale_sql
    assert "source_type = 'document'" in stale_sql
    assert "source_id = %(source_id)s" in stale_sql
    assert "source_chunk_id = %(chunk_id)s" in stale_sql
    assert "parent_chunk_id = ('kc_document_' || %(chunk_id)s)" not in stale_sql
    assert "embedding_status = 'stale'" in stale_sql


def test_import_chunk_queries_use_only_canonical_source_chunk_identity():
    """切片状态聚合只能按原始 import chunk ID 关联，不保留 synthetic parent 兼容 OR。"""
    sql = Database._list_import_chunks_sql()
    normalized_sql = " ".join(sql.split())

    assert "kc.source_chunk_id = ic.id" in sql
    assert "kc.source_id = ic.file_id" in sql
    assert "OR kc.parent_chunk_id" not in sql
    assert "('kc_document_' || ic.id)" not in sql
    assert "expected_knowledge_count" not in sql
    assert "jsonb_array_elements" not in sql
    assert "knowledge_count" in sql
    assert "ready_count" in sql
    assert "END AS embedding_status" not in normalized_sql


def test_list_import_chunks_prefers_structured_block_count_for_status():
    """切片状态必须按结构块优先口径识别 ready，不得被 delimiter 改写结构语义。"""
    conn = _RecordingConnection(
        [
            (
                "FROM import_chunks ic",
                [
                    {
                        "id": "chunk_1",
                        "file_id": "imp_1",
                        "source_text": "第一问\n第二问",
                        "source_blocks": [{"text": "第一问\n第二问"}],
                        "children_delimiter": r"\n",
                        "knowledge_count": 2,
                        "ready_count": 2,
                        "stale_count": 0,
                        "failed_count": 0,
                        "pending_count": 0,
                    }
                ],
            )
        ]
    )
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    chunks = db.list_import_chunks("imp_1")

    assert chunks[0]["expected_knowledge_count"] == 2
    assert chunks[0]["embedding_status"] == "ready"


def test_schema_repairs_old_knowledge_identity_and_stale_projections_once():
    """初始化 SQL 必须一次性校正旧 child 身份，并让缺 child 或正文指纹漂移的数据重建。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "UPDATE knowledge_chunks AS child" in schema
    assert "FROM knowledge_chunks AS parent" in schema
    assert "child.parent_chunk_id = parent.id" in schema
    assert "source_chunk_id = parent.source_chunk_id" in schema
    assert "child.source_id = parent.source_id" in schema
    assert "child.parent_chunk_id = parent.id" in schema
    assert "child.source_chunk_id IS DISTINCT FROM parent.source_chunk_id" in schema
    assert "jsonb_set(" in schema
    assert "'{chunk_id}'" in schema
    assert "embedding_status = 'stale'" in schema
    assert "UPDATE knowledge_chunks AS parent" in schema
    assert "parent.embedding_status = 'ready'" in schema
    assert "NOT EXISTS (" in schema
    assert "child.source_chunk_id = parent.source_chunk_id" in schema
    assert "child.chunk_level = 'child'" in schema
    parent_only_repair = schema.split("-- 只有 parent", 1)[1].split("-- FAQ", 1)[0]
    assert "child.parent_chunk_id = parent.id" in parent_only_repair
    assert "FROM faq_documents AS faq" in schema
    assert "projection.content_hash IS DISTINCT FROM faq.content_hash" in schema
    assert "FROM import_chunks AS source_chunk" in schema
    assert "projection.metadata->'questions'" in schema
    assert "source_chunk.questions" in schema


def test_schema_deletes_legacy_document_chunk_levels():
    """幂等迁移必须删除旧 document chunk 层级，只保留当前 parent/child 契约。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")
    normalized = " ".join(schema.split())

    assert re.search(
        r"DELETE FROM knowledge_chunks\s+WHERE source_type = 'document'\s+"
        r"AND chunk_level NOT IN \('parent', 'child'\)",
        normalized,
    )


def test_replace_import_chunks_removes_old_document_knowledge_chunks():
    """重新解析替换切片前，应先清理同文件旧知识单元，避免问答页召回旧向量。"""

    class _FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((sql, params or {}))
            return self

        def fetchone(self):
            return {}

        def fetchall(self):
            """返回空锁定结果，模拟当前文件没有旧切片。"""
            return []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    conn = _FakeConn()
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    db.replace_import_chunks("imp_1", [])

    assert conn.calls
    delete_knowledge_calls = [
        (sql, params)
        for sql, params in conn.calls
        if "DELETE FROM knowledge_chunks" in sql
    ]
    assert delete_knowledge_calls, "replace_import_chunks must clear old document knowledge"
    sql, params = delete_knowledge_calls[0]
    assert "source_type = 'document'" in sql
    assert "source_id = %(file_id)s" in sql
    assert params == {"file_id": "imp_1"}
    import_delete_index = next(
        index for index, (sql, _params) in enumerate(conn.calls) if "DELETE FROM import_chunks" in sql
    )
    knowledge_delete_index = conn.calls.index(delete_knowledge_calls[0])
    assert knowledge_delete_index < import_delete_index


def test_parent_context_sql_reads_same_source_parent_chunks():
    """父级上下文回填只能读取同来源、可用且 ready 的 parent chunk。"""
    sql = Database._get_parent_context_chunks_sql()

    assert "FROM knowledge_chunks child" in sql
    assert "JOIN knowledge_chunks parent" in sql
    assert "parent.id = child.parent_chunk_id" in sql
    assert "parent.source_type = child.source_type" in sql
    assert "parent.source_id = child.source_id" in sql
    assert "parent.chunk_level = 'parent'" in sql
    assert "parent.status = %(status)s" in sql
    assert "parent.embedding_status = 'ready'" in sql


def test_query_analytics_events_schema_records_query_intent_and_hits():
    """查询打点表需要保存原始 query、意图、命中数、score、rerank 标记、来源标识。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS query_analytics_events" in schema
    assert "query TEXT NOT NULL" in schema
    assert "intent TEXT" in schema
    assert "retrieved_chunk_ids TEXT[] NOT NULL DEFAULT '{}'" in schema
    assert "top_score DOUBLE PRECISION" in schema
    assert "hit_count INT NOT NULL DEFAULT 0" in schema
    assert "rerank_used BOOLEAN NOT NULL DEFAULT false" in schema
    assert "latency_ms INT" in schema
    assert "requester_type TEXT NOT NULL DEFAULT 'unknown'" in schema
    assert "requester_id TEXT" in schema
    assert "metadata JSONB NOT NULL DEFAULT '{}'::jsonb" in schema
    assert "idx_query_analytics_created_at" in schema
    assert "idx_query_analytics_hit_zero" in schema


def test_query_analytics_cluster_summaries_schema_supports_llm_clustering():
    """零命中 LLM 聚类结果需要存到独立表，保留 period_start / period_end / sample_queries。"""
    schema = Path("sql/001_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS query_analytics_cluster_summaries" in schema
    assert "period_start TIMESTAMPTZ NOT NULL" in schema
    assert "period_end TIMESTAMPTZ NOT NULL" in schema
    assert "cluster_label TEXT NOT NULL" in schema
    assert "suggested_content TEXT" in schema
    assert "event_count INT NOT NULL" in schema
    assert "sample_queries TEXT[] NOT NULL DEFAULT '{}'" in schema


def test_record_query_event_sql_inserts_all_fields():
    """打点写入应覆盖所有分析维度，确保看板查询有数据。"""
    sql = Database._record_query_event_sql()

    assert "INSERT INTO query_analytics_events" in sql
    assert "query" in sql
    assert "intent" in sql
    assert "retrieved_chunk_ids" in sql
    assert "top_score" in sql
    assert "hit_count" in sql
    assert "rerank_used" in sql
    assert "latency_ms" in sql
    assert "requester_type" in sql
    assert "requester_id" in sql
    assert "metadata" in sql


def test_list_top_queries_sql_groups_by_normalized_query():
    """高频查询聚合应按归一化后的 query 计数并按时间过滤。"""
    sql = Database._list_top_queries_sql()

    assert "FROM query_analytics_events" in sql
    assert "GROUP BY" in sql
    assert "COUNT(*)" in sql
    assert "created_at >= %(since)s" in sql
    assert "ORDER BY" in sql
    assert "LIMIT %(limit)s" in sql


def test_list_zero_hit_queries_sql_filters_hit_count_zero():
    """零命中查询读取应只看 hit_count = 0 的记录。"""
    sql = Database._list_zero_hit_queries_sql()

    assert "FROM query_analytics_events" in sql
    assert "hit_count = 0" in sql
    assert "created_at >= %(since)s" in sql
    assert "ORDER BY created_at DESC" in sql


def test_list_low_score_queries_sql_filters_top_score_below_threshold():
    """低置信查询读取应只看 top_score 低于阈值且至少命中一条的记录。"""
    sql = Database._list_low_score_queries_sql()

    assert "FROM query_analytics_events" in sql
    assert "hit_count > 0" in sql
    assert "top_score < %(threshold)s" in sql
    assert "created_at >= %(since)s" in sql


def test_query_hit_rate_timeseries_sql_buckets_by_date():
    """命中率时序应按时间桶聚合 hit_count > 0 的比例。"""
    sql = Database._query_hit_rate_timeseries_sql()

    assert "FROM query_analytics_events" in sql
    assert "date_trunc" in sql
    assert "hit_count > 0" in sql
    assert "created_at >= %(since)s" in sql


def test_top_referenced_chunks_sql_unnests_retrieved_chunk_ids():
    """chunk 引用频次需要展开 retrieved_chunk_ids 数组并按 chunk_id 聚合。"""
    sql = Database._top_referenced_chunks_sql()

    assert "FROM query_analytics_events" in sql
    assert "unnest(retrieved_chunk_ids)" in sql
    assert "GROUP BY" in sql
    assert "COUNT(*)" in sql
    assert "LIMIT %(limit)s" in sql


def test_database_record_query_event_writes_via_connection():
    """Database.record_query_event 应通过 connect 把事件写入 query_analytics_events。"""

    class _FakeCursor:
        def __init__(self, store):
            self.store = store

        def execute(self, sql, params=None):
            self.store.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakeConn:
        def __init__(self, store):
            self.store = store

        def execute(self, sql, params=None):
            self.store.append((sql, params))
            return _FakeCursor(self.store)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    store: list = []
    db = Database("postgresql://unused")
    db.connect = lambda: _FakeConn(store)

    db.record_query_event(
        {
            "query": "如何重置密码",
            "intent": "procedure",
            "retrieved_chunk_ids": ["kc_1", "kc_2"],
            "top_score": 0.78,
            "hit_count": 2,
            "rerank_used": True,
            "latency_ms": 154,
            "requester_type": "agent",
            "requester_id": "listing-writer",
            "metadata": {"flow": "basic_rag"},
        }
    )

    assert store, "record_query_event must execute SQL via connect()"
    sql, params = store[0]
    assert "INSERT INTO query_analytics_events" in sql
    assert params["query"] == "如何重置密码"
    assert params["intent"] == "procedure"
    assert params["retrieved_chunk_ids"] == ["kc_1", "kc_2"]
    assert params["top_score"] == 0.78
    assert params["hit_count"] == 2
    assert params["rerank_used"] is True
    assert params["latency_ms"] == 154
    assert params["requester_type"] == "agent"
    assert params["requester_id"] == "listing-writer"


def test_kg_subgraph_sql_hardcodes_usable_and_preserves_isolated_center():
    """子图 SQL 必须由 usable center 左连边，缺边时仍返回中心实体。"""
    sql = Database._kg_subgraph_sql()

    assert "WITH RECURSIVE center AS" in sql
    assert "center.status = 'usable'" in sql
    assert "relation.status = 'usable'" in sql
    assert "neighbor.status = 'usable'" in sql
    assert "head.status = 'usable'" in sql
    assert "tail.status = 'usable'" in sql
    assert "LEFT JOIN" in sql
    assert "%(hops)s" in sql
    assert "reachable.depth < %(hops)s" in sql
    assert "%(center_entity_id)s" in sql
    assert "%(status)s" not in sql


def test_kg_subgraph_counts_only_live_relation_evidence():
    """子图边 evidence_count 必须忽略已失效但仍保留的历史证据。"""
    sql = Database._kg_subgraph_sql()
    normalized_sql = " ".join(sql.split())

    assert "count(DISTINCT valid_ev.id)::integer" in normalized_sql
    assert "valid_ev.relation_id = relation.id" in normalized_sql
    assert "valid_ev.source_type = 'faq' AND faq.status = 'usable'" in normalized_sql
    assert "valid_ev.source_type = 'document'" in normalized_sql
    assert "imp.id = valid_ev.source_id" in normalized_sql
    assert "chunk.id = valid_ev.source_chunk_id" in normalized_sql
    assert "chunk.file_id = imp.id" in normalized_sql
    assert "imp.is_disabled = false" in normalized_sql
    assert "chunk.is_disabled = false" in normalized_sql
    assert (
        "FROM kg_evidence evidence WHERE evidence.relation_id = relation.id"
        not in normalized_sql
    )


def test_get_kg_subgraph_returns_none_without_usable_center():
    """中心实体不存在或非 usable 时，DB 返回 None 供 Admin 明确映射 404。"""
    conn = _RecordingConnection([("WITH RECURSIVE center AS", [])])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.get_kg_subgraph(center_entity_id="kg_ent_missing")

    assert result is None
    assert "status" not in conn.calls[0][1]


def test_get_kg_subgraph_returns_explicit_isolated_center():
    """usable 中心没有过滤后关系时，必须返回 isolated 而不是空图或不存在。"""
    row = {
        "center_id": "kg_ent_1",
        "center_name": "报告导出",
        "center_entity_type": "feature_ui_action",
        "center_description": "导出报告",
        "center_status": "usable",
        "center_confidence": "high",
        "relation_id": None,
    }
    conn = _RecordingConnection([("WITH RECURSIVE center AS", [row])])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.get_kg_subgraph(center_entity_id="kg_ent_1")

    assert result == {
        "state": "isolated",
        "center": {
            "id": "kg_ent_1",
            "name": "报告导出",
            "entity_type": "feature_ui_action",
            "description": "导出报告",
            "status": "usable",
            "confidence": "high",
        },
        "nodes": [
            {
                "id": "kg_ent_1",
                "name": "报告导出",
                "entity_type": "feature_ui_action",
                "description": "导出报告",
                "status": "usable",
                "confidence": "high",
            }
        ],
        "edges": [],
    }


def test_get_kg_subgraph_returns_connected_usable_nodes_and_edges():
    """存在 usable 关系时，子图返回 center-first 节点和 connected 状态。"""
    row = {
        "center_id": "kg_ent_1",
        "center_name": "报告导出",
        "center_entity_type": "feature_ui_action",
        "center_description": None,
        "center_status": "usable",
        "center_confidence": "high",
        "relation_id": "kg_rel_1",
        "relation_type": "requires",
        "relation_description": "需要权限",
        "relation_confidence": "high",
        "relation_status": "usable",
        "evidence_count": 1,
        "head_entity_id": "kg_ent_1",
        "head_entity_name": "报告导出",
        "head_entity_type": "feature_ui_action",
        "head_entity_description": None,
        "head_entity_status": "usable",
        "head_entity_confidence": "high",
        "tail_entity_id": "kg_ent_2",
        "tail_entity_name": "导出权限",
        "tail_entity_type": "condition_policy",
        "tail_entity_description": None,
        "tail_entity_status": "usable",
        "tail_entity_confidence": "high",
    }
    conn = _RecordingConnection([("WITH RECURSIVE center AS", [row])])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    result = db.get_kg_subgraph(center_entity_id="kg_ent_1")

    assert result is not None
    assert result["state"] == "connected"
    assert result["center"]["id"] == "kg_ent_1"
    assert [node["id"] for node in result["nodes"]] == ["kg_ent_1", "kg_ent_2"]
    assert result["edges"] == [
        {
            "id": "kg_rel_1",
            "source": "kg_ent_1",
            "target": "kg_ent_2",
            "relation_type": "requires",
            "description": "需要权限",
            "confidence": "high",
            "status": "usable",
            "evidence_count": 1,
        }
    ]


def test_get_kg_subgraph_rejects_missing_mandatory_evidence_count():
    """子图边缺少当前必填计数时必须显式失败，不得伪造 0。"""
    row = {
        "center_id": "kg_ent_1",
        "center_name": "报告导出",
        "center_entity_type": "feature_ui_action",
        "center_description": None,
        "center_status": "usable",
        "center_confidence": "high",
        "relation_id": "kg_rel_1",
        "relation_type": "requires",
        "relation_description": "需要权限",
        "relation_confidence": "high",
        "relation_status": "usable",
        "head_entity_id": "kg_ent_1",
        "head_entity_name": "报告导出",
        "head_entity_type": "feature_ui_action",
        "head_entity_description": None,
        "head_entity_status": "usable",
        "head_entity_confidence": "high",
        "tail_entity_id": "kg_ent_2",
        "tail_entity_name": "导出权限",
        "tail_entity_type": "condition_policy",
        "tail_entity_description": None,
        "tail_entity_status": "usable",
        "tail_entity_confidence": "high",
    }
    conn = _RecordingConnection([("WITH RECURSIVE center AS", [row])])
    db = Database("postgresql://unused")
    db.connect = lambda: conn

    with pytest.raises(KeyError, match="evidence_count"):
        db.get_kg_subgraph(center_entity_id="kg_ent_1")
