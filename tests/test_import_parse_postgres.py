from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import os
from queue import Queue
import threading
import time
from typing import Any, Iterator
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from cyclops.db import Database


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL import parse tests",
)


class _SchemaDatabase(Database):
    """把生产 DB 方法固定到随机 schema，禁止测试写入业务表。"""

    def __init__(self, database_url: str, schema: str):
        """保存可信随机 schema 名称，连接时同时保留 public 扩展可见性。"""
        super().__init__(database_url)
        self.schema = schema

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """为每个生产事务创建独立连接，并设置有限锁等待。"""
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            conn.execute(
                sql.SQL("SET search_path TO {}, public").format(
                    sql.Identifier(self.schema)
                )
            )
            conn.execute("SET lock_timeout = '5s'")
            yield conn


@pytest.fixture
def isolated_import_schema() -> Iterator[tuple[str, _SchemaDatabase]]:
    """创建完整 schema 并在测试后级联删除，业务 schema 始终不进入 search_path。"""
    schema = f"cyclops_import_parse_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = _SchemaDatabase(TEST_DATABASE_URL, schema)
    try:
        database.init_schema()
        yield schema, database
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def _replacement_chunk(file_id: str) -> dict[str, Any]:
    """构造原子完成入口接受的完整 current chunk。"""
    return {
        "id": "chunk_new",
        "file_id": file_id,
        "chunk_index": 0,
        "section_path": ["新章节"],
        "page_start": 2,
        "page_end": 2,
        "block_type": "text",
        "source_offsets": {"start": 0, "end": 5},
        "source_blocks": [],
        "children_delimiter": "",
        "start_at": None,
        "end_at": None,
        "message_count": 0,
        "keywords": [],
        "source_text": "新的解析正文",
        "status": "pending",
        "candidate_count": 0,
    }


def test_expired_lease_and_finalizing_recover_into_one_atomic_snapshot(
    isolated_import_schema: tuple[str, _SchemaDatabase],
) -> None:
    """无 HTTP 请求时可连续重领两次，最终只提交一份完整来源 snapshot。"""
    _schema, database = isolated_import_schema
    file_id = "imp_parse_recovery"
    database.create_import_file(
        {
            "id": file_id,
            "original_name": "恢复手册.pdf",
            "stored_path": "/tmp/recovery.pdf",
            "file_type": "pdf",
            "parser": "mineru",
            "chunker_type": "naive",
            "status": "pending",
        }
    )
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO import_chunks (
                id, file_id, chunk_index, source_text, status, candidate_count
            )
            VALUES ('chunk_old', %(file_id)s, 0, '旧正文', 'pending', 0)
            """,
            {"file_id": file_id},
        )
        conn.execute(
            """
            INSERT INTO knowledge_chunks (
                id, source_type, source_id, source_chunk_id, chunk_level,
                source_title, chunk_index, content, embedding_text, search_text,
                status, embedding_status, content_hash
            )
            VALUES (
                'kc_old_document', 'document', %(file_id)s, 'chunk_old', 'child',
                '恢复手册.pdf', 0, '旧正文', '旧正文', '旧正文',
                'usable', 'pending', 'old-document-hash'
            )
            """,
            {"file_id": file_id},
        )
        conn.execute(
            """
            INSERT INTO kg_entities (
                id, name, entity_type, status, source_count
            )
            VALUES (
                'kg_ent_old_source', '旧来源实体', 'feature_ui_action', 'usable', 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO kg_evidence (
                id, entity_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES (
                'kg_ev_old_source', 'kg_ent_old_source', 'document',
                %(file_id)s, 'chunk_old', '旧正文', 0, 3
            )
            """,
            {"file_id": file_id},
        )
        conn.execute(
            """
            INSERT INTO knowledge_chunks (
                id, source_type, source_id, chunk_level, source_title,
                chunk_index, content, embedding_text, search_text,
                status, embedding_status, content_hash
            )
            VALUES (
                'kc_kg_old_source', 'kg_entity', 'kg_ent_old_source', 'chunk',
                '旧来源实体', 0, '旧来源实体', '旧来源实体', '旧来源实体',
                'usable', 'pending', 'old-kg-hash'
            )
            """
        )

    job = database.create_import_parse_job(
        file_id,
        chunker_type="naive",
        input_fingerprint="sha256:recovery-v1",
    )
    worker_a = database.claim_import_parse_job(lease_seconds=1)
    assert worker_a is not None
    assert worker_a["status"] == "submitting"
    time.sleep(0.6)
    assert database.renew_import_parse_job_lease(
        job["id"],
        lease_token=worker_a["lease_token"],
        lease_seconds=2,
    ) is True
    time.sleep(0.6)
    assert database.claim_import_parse_job(lease_seconds=30) is None

    with database.connect() as conn:
        conn.execute(
            """
            UPDATE import_parse_jobs
            SET lease_expires_at = now() - interval '1 second'
            WHERE id = %(job_id)s
            """,
            {"job_id": job["id"]},
        )
    worker_b = database.claim_import_parse_job(lease_seconds=30)
    assert worker_b is not None
    assert worker_b["lease_token"] != worker_a["lease_token"]

    database.update_import_parse_job_progress(
        job["id"],
        lease_token=worker_b["lease_token"],
        status="polling",
        progress={"state": "running", "percent": 40},
        provider_batch_id="batch_recovery",
        provider_file_name="recovery.pdf",
        next_poll_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    polling = database.claim_import_parse_job(lease_seconds=30)
    assert polling is not None
    finalizing = database.begin_import_parse_job_finalization(
        job["id"],
        lease_token=polling["lease_token"],
        progress={"state": "completed", "percent": 100},
    )
    assert finalizing["status"] == "finalizing"

    with database.connect() as conn:
        conn.execute(
            """
            UPDATE import_parse_jobs
            SET lease_expires_at = now() - interval '1 second'
            WHERE id = %(job_id)s
            """,
            {"job_id": job["id"]},
        )
    recovered = database.claim_import_parse_job(lease_seconds=30)
    assert recovered is not None
    assert recovered["status"] == "finalizing"
    assert recovered["lease_token"] != polling["lease_token"]

    completed = database.complete_import_parse_job(
        job["id"],
        lease_token=recovered["lease_token"],
        input_fingerprint="sha256:recovery-v1",
        chunks=[_replacement_chunk(file_id)],
        progress={"state": "completed", "percent": 100},
    )
    assert completed["status"] == "completed"

    with database.connect() as conn:
        final_state = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM import_parse_jobs WHERE file_id = %(file_id)s)
                    AS job_count,
                (SELECT status FROM import_files WHERE id = %(file_id)s)
                    AS file_status,
                (SELECT array_agg(id ORDER BY id) FROM import_chunks WHERE file_id = %(file_id)s)
                    AS chunk_ids,
                (SELECT count(*) FROM knowledge_chunks
                    WHERE source_type = 'document' AND source_id = %(file_id)s)
                    AS document_knowledge_count,
                (SELECT count(*) FROM kg_evidence
                    WHERE source_type = 'document' AND source_id = %(file_id)s)
                    AS evidence_count,
                (SELECT status FROM kg_entities WHERE id = 'kg_ent_old_source')
                    AS entity_status,
                (SELECT source_count FROM kg_entities WHERE id = 'kg_ent_old_source')
                    AS entity_source_count,
                (SELECT status FROM knowledge_chunks
                    WHERE source_type = 'kg_entity' AND source_id = 'kg_ent_old_source')
                    AS projection_status
            """,
            {"file_id": file_id},
        ).fetchone()

    assert final_state == {
        "job_count": 1,
        "file_status": "needs_review",
        "chunk_ids": ["chunk_new"],
        "document_knowledge_count": 0,
        "evidence_count": 0,
        "entity_status": "disabled",
        "entity_source_count": 0,
        "projection_status": "disabled",
    }


def test_delete_and_complete_share_job_before_file_lock_order(
    isolated_import_schema: tuple[str, _SchemaDatabase],
) -> None:
    """并发删除与完成允许任一方先赢，但不得死锁或留下孤立 job/file。"""
    schema, database = isolated_import_schema
    file_id = "imp_delete_complete"
    database.create_import_file(
        {
            "id": file_id,
            "original_name": "并发手册.pdf",
            "stored_path": "/tmp/concurrent.pdf",
            "file_type": "pdf",
            "parser": "mineru",
            "chunker_type": "naive",
            "status": "pending",
        }
    )
    job = database.create_import_parse_job(
        file_id,
        chunker_type="naive",
        input_fingerprint="sha256:delete-complete",
    )
    claimed = database.claim_import_parse_job(lease_seconds=30)
    assert claimed is not None

    barrier = threading.Barrier(3)
    outcomes: Queue[tuple[str, str, str | None]] = Queue()

    def complete_snapshot() -> None:
        """在独立生产连接中尝试提交空 snapshot，并记录允许的竞争结果。"""
        worker_db = _SchemaDatabase(TEST_DATABASE_URL, schema)
        barrier.wait()
        try:
            result = worker_db.complete_import_parse_job(
                job["id"],
                lease_token=claimed["lease_token"],
                input_fingerprint="sha256:delete-complete",
                chunks=[],
                progress={"state": "completed", "percent": 100},
            )
        except Exception as exc:
            outcomes.put(("complete", "error", exc.__class__.__name__))
        else:
            outcomes.put(("complete", result["status"], None))

    def delete_source() -> None:
        """在独立生产连接中删除来源，验证级联 job 不引入反向锁。"""
        worker_db = _SchemaDatabase(TEST_DATABASE_URL, schema)
        barrier.wait()
        try:
            result = worker_db.delete_import_file(file_id)
        except Exception as exc:
            outcomes.put(("delete", "error", exc.__class__.__name__))
        else:
            outcomes.put(("delete", "deleted" if result else "missing", None))

    complete_thread = threading.Thread(target=complete_snapshot, daemon=True)
    delete_thread = threading.Thread(target=delete_source, daemon=True)
    complete_thread.start()
    delete_thread.start()
    barrier.wait()
    complete_thread.join(timeout=10)
    delete_thread.join(timeout=10)

    assert complete_thread.is_alive() is False
    assert delete_thread.is_alive() is False
    recorded = [outcomes.get_nowait(), outcomes.get_nowait()]
    delete_outcome = next(item for item in recorded if item[0] == "delete")
    complete_outcome = next(item for item in recorded if item[0] == "complete")
    assert delete_outcome == ("delete", "deleted", None)
    assert complete_outcome in {
        ("complete", "completed", None),
        ("complete", "error", "KeyError"),
        ("complete", "error", "ValueError"),
    }

    with database.connect() as conn:
        remaining = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM import_files WHERE id = %(file_id)s) AS files,
                (SELECT count(*) FROM import_parse_jobs WHERE file_id = %(file_id)s) AS jobs
            """,
            {"file_id": file_id},
        ).fetchone()
    assert remaining == {"files": 0, "jobs": 0}
