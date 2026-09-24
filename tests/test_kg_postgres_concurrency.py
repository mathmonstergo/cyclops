from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from queue import Queue
import threading
import time
import uuid
from typing import Any, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from cyclops.db import Database
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL concurrency tests",
)


class _SchemaDatabase(Database):
    """把每条测试连接固定到独立 schema，避免并发夹具接触业务表。"""

    def __init__(self, database_url: str, schema: str):
        """保存测试 schema；schema 名由测试生成，不接收外部 SQL 片段。"""
        super().__init__(database_url)
        self.schema = schema

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """连接隔离 schema，并只追加 public 以解析已安装的 vector 扩展类型。"""
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            conn.execute(
                sql.SQL("SET search_path TO {}, public").format(
                    sql.Identifier(self.schema)
                )
            )
            conn.execute("SET lock_timeout = '5s'")
            yield conn


class _CoordinatedDatabase(_SchemaDatabase):
    """暴露实体阶段到达事件，只协调测试时序，不改变生产锁实现。"""

    def __init__(
        self,
        database_url: str,
        schema: str,
        entity_phase_started: threading.Event,
    ):
        """接收一次性事件，供另一事务在候选实体阻塞后继续锁关系。"""
        super().__init__(database_url, schema)
        self.entity_phase_started = entity_phase_started
        self.backend_pid: int | None = None

    def _lock_or_upsert_kg_entities_in_conn(
        self,
        conn: Any,
        entity_ids: list[str],
        candidate_by_id: dict[str, dict[str, Any]],
    ) -> None:
        """在真实实体操作前发信号；随后完全复用生产实现。"""
        self.backend_pid = conn.info.backend_pid
        self.entity_phase_started.set()
        super()._lock_or_upsert_kg_entities_in_conn(
            conn,
            entity_ids,
            candidate_by_id,
        )


def _wait_for_backend_lock(database_url: str, backend_pid: int, *, timeout: float) -> None:
    """轮询 pg_stat_activity，直到目标事务真实等待数据库锁或达到超时。"""
    deadline = time.monotonic() + timeout
    with psycopg.connect(
        database_url,
        autocommit=True,
        row_factory=dict_row,
    ) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                """
                SELECT wait_event_type
                FROM pg_stat_activity
                WHERE pid = %(pid)s
                """,
                {"pid": backend_pid},
            ).fetchone()
            if row is not None and row["wait_event_type"] == "Lock":
                return
            time.sleep(0.01)
    raise TimeoutError(f"backend {backend_pid} did not wait for a lock")


class _BorrowedConnectionDatabase(_SchemaDatabase):
    """让公开审核方法复用测试线程已持锁连接，同时保留生产 SQL 和业务流程。"""

    def __init__(self, database_url: str, schema: str):
        """初始化线程局部借用连接，避免跨线程共享 psycopg 事务。"""
        super().__init__(database_url, schema)
        self._borrowed = threading.local()

    @contextmanager
    def borrow(self, conn: Any) -> Iterator[None]:
        """在当前线程临时借用外层事务，供公开确认方法继续同一锁序。"""
        self._borrowed.connection = conn
        try:
            yield
        finally:
            del self._borrowed.connection

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """优先返回线程借用连接，否则创建独立隔离 schema 连接。"""
        borrowed = getattr(self._borrowed, "connection", None)
        if borrowed is not None:
            yield borrowed
            return
        with super().connect() as conn:
            yield conn

    @staticmethod
    def _insert_knowledge_chunk_sql() -> str:
        """适配最小测试投影表；确认流程其余查询全部使用生产实现。"""
        return """
        INSERT INTO knowledge_chunks (source_type, source_id, status)
        VALUES (%(source_type)s, %(source_id)s, %(status)s)
        ON CONFLICT (source_type, source_id) DO UPDATE
        SET status = EXCLUDED.status, updated_at = now()
        RETURNING *
        """


@pytest.fixture
def isolated_kg_schema() -> Iterator[tuple[str, str]]:
    """创建最小 KG schema 并在测试后级联删除，不写入现有业务表。"""
    schema = f"cyclops_kg_lock_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(
            """
            CREATE TABLE faq_documents (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                category TEXT,
                tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                confidence TEXT NOT NULL DEFAULT 'high',
                status TEXT NOT NULL,
                embedding_status TEXT NOT NULL DEFAULT 'pending',
                embedding_model TEXT,
                embedding_dimensions INTEGER,
                embedding_updated_at TIMESTAMPTZ,
                embedding_error TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE import_files (
                id TEXT PRIMARY KEY,
                is_disabled BOOLEAN NOT NULL DEFAULT false,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE import_chunks (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL REFERENCES import_files(id),
                is_disabled BOOLEAN NOT NULL DEFAULT false,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE kg_entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'needs_review',
                review_revision BIGINT NOT NULL DEFAULT 1,
                confidence DOUBLE PRECISION,
                source_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE kg_relations (
                id TEXT PRIMARY KEY,
                head_entity_id TEXT NOT NULL REFERENCES kg_entities(id),
                relation_type TEXT NOT NULL,
                tail_entity_id TEXT NOT NULL REFERENCES kg_entities(id),
                description TEXT,
                status TEXT NOT NULL DEFAULT 'needs_review',
                review_revision BIGINT NOT NULL DEFAULT 1,
                confidence DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (head_entity_id, relation_type, tail_entity_id)
            );

            CREATE TABLE kg_evidence (
                id TEXT PRIMARY KEY,
                entity_id TEXT REFERENCES kg_entities(id) ON DELETE CASCADE,
                relation_id TEXT REFERENCES kg_relations(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_chunk_id TEXT,
                source_title TEXT,
                section_path JSONB NOT NULL DEFAULT '[]'::jsonb,
                page_start INTEGER,
                page_end INTEGER,
                excerpt TEXT NOT NULL,
                char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE knowledge_chunks (
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (source_type, source_id)
            );
            """
        )
    try:
        yield TEST_DATABASE_URL, schema
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


@pytest.fixture
def isolated_empty_schema() -> Iterator[tuple[str, str]]:
    """创建供完整初始化 SQL 使用的空 schema，并在验证后连同 extension 一并删除。"""
    schema = f"cyclops_schema_migration_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield TEST_DATABASE_URL, schema
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def test_full_schema_resets_legacy_kg_and_migrates_faq_projection_idempotently(
    isolated_empty_schema: tuple[str, str],
) -> None:
    """旧 KG 只重置一次；FAQ 非向量回填可重复且不改写 embedding 生命周期字段。"""
    database_url, schema = isolated_empty_schema
    db = _SchemaDatabase(database_url, schema)
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE kg_entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'needs_review',
                confidence DOUBLE PRECISION,
                source_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE kg_relations (
                id TEXT PRIMARY KEY,
                head_entity_id TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL,
                tail_entity_id TEXT NOT NULL REFERENCES kg_entities(id) ON DELETE CASCADE,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'needs_review',
                confidence DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (head_entity_id, relation_type, tail_entity_id)
            );
            INSERT INTO kg_entities (id, name, entity_type)
            VALUES
                ('kg_ent_a', 'A', 'feature_ui_action'),
                ('kg_ent_b', 'B', 'condition_policy');
            INSERT INTO kg_relations (
                id, head_entity_id, relation_type, tail_entity_id
            )
            VALUES ('kg_rel_1', 'kg_ent_a', 'requires', 'kg_ent_b');
            """
        )

    db.init_schema()
    db.init_schema()

    with db.connect() as conn:
        reset_state = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM kg_entities
                 WHERE id IN ('kg_ent_a', 'kg_ent_b')) AS entity_count,
                (SELECT count(*) FROM kg_relations
                 WHERE id = 'kg_rel_1') AS relation_count,
                (SELECT count(*) FROM cyclops_schema_migrations
                 WHERE id = '20260715_document_kg_pipeline_v1') AS reset_marker_count
            """
        ).fetchone()
        conn.execute(
            """
            INSERT INTO faq_documents (
                id, doc_type, source_file, source_group, source_date, category,
                question, question_variants, answer, tags, evidence, confidence,
                status, embedding_text, embedding_status, content_hash
            )
            VALUES (
                'faq_1', 'faq_qa', '新来源.md', '审核组', '2026-07-15', '导出',
                '如何导出？', '["怎么导出"]'::jsonb, '先检查权限。', '["导出"]'::jsonb,
                '[{"excerpt":"新证据"}]'::jsonb, 'high', 'needs_review',
                '标准问题：如何导出？', 'pending', 'same-hash'
            );
            INSERT INTO knowledge_chunks (
                id, source_type, source_id, source_chunk_id, parent_chunk_id,
                chunk_level, source_title, chunk_index, content, embedding_text,
                search_text, metadata, confidence, status, embedding_status,
                content_hash
            )
            VALUES (
                'kc_faq_faq_1', 'faq', 'faq_1', NULL, NULL,
                'chunk', '如何导出？', 0, '旧内容', '标准问题：如何导出？',
                '旧搜索文本', '{"evidence":[{"excerpt":"旧证据"}]}'::jsonb,
                'low', 'disabled', 'pending', 'same-hash'
            );
            """
        )
    assert reset_state == {
        "entity_count": 0,
        "relation_count": 0,
        "reset_marker_count": 1,
    }

    db.init_schema()
    db.init_schema()

    with db.connect() as conn:
        projection = conn.execute(
            """
            SELECT metadata, confidence, status, embedding IS NULL AS embedding_is_null,
                   embedding_status, content_hash
            FROM knowledge_chunks
            WHERE source_type = 'faq' AND source_id = 'faq_1' AND chunk_index = 0
            """
        ).fetchone()
    assert projection == {
        "metadata": {
            "category": "导出",
            "question_variants": ["怎么导出"],
            "evidence": [{"excerpt": "新证据"}],
            "source_file": "新来源.md",
            "source_group": "审核组",
            "source_date": "2026-07-15",
        },
        "confidence": "high",
        "status": "needs_review",
        "embedding_is_null": True,
        "embedding_status": "pending",
        "content_hash": "same-hash",
    }


def test_full_schema_repairs_kg_live_state_once_and_is_idempotent(
    isolated_empty_schema: tuple[str, str],
) -> None:
    """完整初始化首次精确修复 count/status/projection，第二次不得再次递增 revision。"""
    database_url, schema = isolated_empty_schema
    db = _SchemaDatabase(database_url, schema)
    db.init_schema()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO faq_documents (
                id, doc_type, question, answer, confidence, status,
                embedding_text, embedding_status, content_hash
            )
            VALUES (
                'faq_live', 'faq_qa', '如何导出？', '先检查权限。', 'high',
                'usable', '标准问题：如何导出？', 'pending', 'faq-live-hash'
            );
            INSERT INTO kg_entities (
                id, name, entity_type, status, review_revision, source_count,
                updated_at
            )
            VALUES
                (
                    'kg_ent_invalid', '失效实体', 'feature_ui_action',
                    'usable', 3, 8, '2026-01-01T00:00:00Z'
                ),
                (
                    'kg_ent_shared', '共享实体', 'condition_policy',
                    'usable', 7, 9, '2026-01-01T00:00:00Z'
                );
            INSERT INTO kg_relations (
                id, head_entity_id, relation_type, tail_entity_id,
                status, review_revision, updated_at
            )
            VALUES
                (
                    'kg_rel_invalid', 'kg_ent_invalid', 'requires',
                    'kg_ent_shared', 'usable', 4, '2026-01-01T00:00:00Z'
                ),
                (
                    'kg_rel_live', 'kg_ent_shared', 'belongs_to',
                    'kg_ent_invalid', 'usable', 9, '2026-01-01T00:00:00Z'
                );
            INSERT INTO kg_evidence (
                id, entity_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES
                (
                    'kg_ev_invalid_document', 'kg_ent_invalid', 'document',
                    'missing_file', 'missing_chunk', '已经失效的文档证据', 0, 9
                ),
                (
                    'kg_ev_shared_a', 'kg_ent_shared', 'faq',
                    'faq_live', NULL, '共享 FAQ 证据一', 0, 10
                ),
                (
                    'kg_ev_shared_b', 'kg_ent_shared', 'faq',
                    'faq_live', NULL, '共享 FAQ 证据二', 0, 10
                );
            INSERT INTO kg_evidence (
                id, relation_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES (
                'kg_ev_relation_live', 'kg_rel_live', 'faq',
                'faq_live', NULL, '关系实时证据', 0, 6
            );
            INSERT INTO knowledge_chunks (
                id, source_type, source_id, content, embedding_text,
                search_text, status, content_hash, updated_at
            )
            VALUES
                (
                    'kc_ent_invalid', 'kg_entity', 'kg_ent_invalid', '实体',
                    '实体', '实体', 'usable', 'hash-ent-invalid',
                    '2026-01-01T00:00:00Z'
                ),
                (
                    'kc_ent_shared', 'kg_entity', 'kg_ent_shared', '实体',
                    '实体', '实体', 'disabled', 'hash-ent-shared',
                    '2026-01-01T00:00:00Z'
                ),
                (
                    'kc_rel_invalid', 'kg_relation', 'kg_rel_invalid', '关系',
                    '关系', '关系', 'usable', 'hash-rel-invalid',
                    '2026-01-01T00:00:00Z'
                ),
                (
                    'kc_rel_live', 'kg_relation', 'kg_rel_live', '关系',
                    '关系', '关系', 'disabled', 'hash-rel-live',
                    '2026-01-01T00:00:00Z'
                );
            DELETE FROM cyclops_schema_migrations
            WHERE id = '20260715_kg_live_state_repair_v1';
            """
        )

    db.init_schema()
    with db.connect() as conn:
        first_entities = conn.execute(
            """
            SELECT id, status, review_revision, source_count, updated_at
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        first_relations = conn.execute(
            """
            SELECT id, status, review_revision, updated_at
            FROM kg_relations
            ORDER BY id
            """
        ).fetchall()
        first_projections = conn.execute(
            """
            SELECT source_type, source_id, status, updated_at
            FROM knowledge_chunks
            WHERE source_type IN ('kg_entity', 'kg_relation')
            ORDER BY source_type, source_id
            """
        ).fetchall()

    assert [
        (row["id"], row["status"], row["review_revision"], row["source_count"])
        for row in first_entities
    ] == [
        ("kg_ent_invalid", "disabled", 4, 0),
        ("kg_ent_shared", "needs_review", 8, 1),
    ]
    assert [
        (row["id"], row["status"], row["review_revision"])
        for row in first_relations
    ] == [
        ("kg_rel_invalid", "disabled", 5),
        ("kg_rel_live", "needs_review", 10),
    ]
    assert [
        (row["source_type"], row["source_id"], row["status"])
        for row in first_projections
    ] == [
        ("kg_entity", "kg_ent_invalid", "disabled"),
        ("kg_entity", "kg_ent_shared", "needs_review"),
        ("kg_relation", "kg_rel_invalid", "disabled"),
        ("kg_relation", "kg_rel_live", "needs_review"),
    ]

    db.init_schema()
    with db.connect() as conn:
        second_entities = conn.execute(
            """
            SELECT id, status, review_revision, source_count, updated_at
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        second_relations = conn.execute(
            """
            SELECT id, status, review_revision, updated_at
            FROM kg_relations
            ORDER BY id
            """
        ).fetchall()
        second_projections = conn.execute(
            """
            SELECT source_type, source_id, status, updated_at
            FROM knowledge_chunks
            WHERE source_type IN ('kg_entity', 'kg_relation')
            ORDER BY source_type, source_id
            """
        ).fetchall()

    assert second_entities == first_entities
    assert second_relations == first_relations
    assert second_projections == first_projections

    with db.connect() as conn:
        conn.execute(
            """
            UPDATE kg_entities
            SET status = 'usable', updated_at = now()
            WHERE id = 'kg_ent_shared';
            UPDATE kg_relations
            SET status = 'usable', updated_at = now()
            WHERE id = 'kg_rel_live';
            UPDATE knowledge_chunks
            SET status = 'usable', updated_at = now()
            WHERE (source_type = 'kg_entity' AND source_id = 'kg_ent_shared')
               OR (source_type = 'kg_relation' AND source_id = 'kg_rel_live');
            """
        )
        confirmed_before_restart = conn.execute(
            """
            SELECT
                (SELECT status FROM kg_entities WHERE id = 'kg_ent_shared')
                    AS entity_status,
                (SELECT review_revision FROM kg_entities WHERE id = 'kg_ent_shared')
                    AS entity_revision,
                (SELECT status FROM kg_relations WHERE id = 'kg_rel_live')
                    AS relation_status,
                (SELECT review_revision FROM kg_relations WHERE id = 'kg_rel_live')
                    AS relation_revision,
                (SELECT status FROM knowledge_chunks
                 WHERE source_type = 'kg_entity' AND source_id = 'kg_ent_shared')
                    AS entity_projection_status,
                (SELECT status FROM knowledge_chunks
                 WHERE source_type = 'kg_relation' AND source_id = 'kg_rel_live')
                    AS relation_projection_status
            """
        ).fetchone()

    db.init_schema()
    with db.connect() as conn:
        confirmed_after_restart = conn.execute(
            """
            SELECT
                (SELECT status FROM kg_entities WHERE id = 'kg_ent_shared')
                    AS entity_status,
                (SELECT review_revision FROM kg_entities WHERE id = 'kg_ent_shared')
                    AS entity_revision,
                (SELECT status FROM kg_relations WHERE id = 'kg_rel_live')
                    AS relation_status,
                (SELECT review_revision FROM kg_relations WHERE id = 'kg_rel_live')
                    AS relation_revision,
                (SELECT status FROM knowledge_chunks
                 WHERE source_type = 'kg_entity' AND source_id = 'kg_ent_shared')
                    AS entity_projection_status,
                (SELECT status FROM knowledge_chunks
                 WHERE source_type = 'kg_relation' AND source_id = 'kg_rel_live')
                    AS relation_projection_status
            """
        ).fetchone()

    assert confirmed_before_restart == {
        "entity_status": "usable",
        "entity_revision": 8,
        "relation_status": "usable",
        "relation_revision": 10,
        "entity_projection_status": "usable",
        "relation_projection_status": "usable",
    }
    assert confirmed_after_restart == confirmed_before_restart


def test_concurrent_init_schema_claims_kg_live_repair_marker_once(
    isolated_empty_schema: tuple[str, str],
) -> None:
    """两个初始化线程竞争同一 marker 时只能修复一次，且 owner/projection 必须精确一致。"""
    schema_sql = Path("sql/001_init.sql").read_text(encoding="utf-8")
    assert "LOCK TABLE cyclops_schema_migrations IN EXCLUSIVE MODE" in schema_sql
    database_url, schema = isolated_empty_schema
    seed_db = _SchemaDatabase(database_url, schema)
    seed_db.init_schema()
    with seed_db.connect() as conn:
        conn.execute(
            """
            INSERT INTO faq_documents (
                id, doc_type, question, answer, confidence, status,
                embedding_text, embedding_status, content_hash
            )
            VALUES (
                'faq_concurrent_repair', 'faq_qa', '如何导出？',
                '先检查权限。', 'high', 'usable', '标准问题：如何导出？',
                'pending', 'faq-concurrent-repair-hash'
            );
            INSERT INTO kg_entities (
                id, name, entity_type, status, review_revision, source_count,
                updated_at
            )
            VALUES
                (
                    'kg_ent_concurrent_live', '报告导出', 'feature_ui_action',
                    'usable', 3, 9, '2026-01-01T00:00:00Z'
                ),
                (
                    'kg_ent_concurrent_zero', '失效权限', 'condition_policy',
                    'usable', 5, 4, '2026-01-01T00:00:00Z'
                );
            INSERT INTO kg_relations (
                id, head_entity_id, relation_type, tail_entity_id,
                status, review_revision, updated_at
            )
            VALUES (
                'kg_rel_concurrent_live', 'kg_ent_concurrent_live', 'requires',
                'kg_ent_concurrent_zero', 'usable', 7,
                '2026-01-01T00:00:00Z'
            );
            INSERT INTO kg_evidence (
                id, entity_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES (
                'kg_ev_concurrent_entity', 'kg_ent_concurrent_live', 'faq',
                'faq_concurrent_repair', NULL, '实体实时证据', 0, 6
            );
            INSERT INTO kg_evidence (
                id, relation_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES (
                'kg_ev_concurrent_relation', 'kg_rel_concurrent_live', 'faq',
                'faq_concurrent_repair', NULL, '关系实时证据', 0, 6
            );
            INSERT INTO knowledge_chunks (
                id, source_type, source_id, content, embedding_text,
                search_text, status, content_hash, updated_at
            )
            VALUES
                (
                    'kc_ent_concurrent_live', 'kg_entity',
                    'kg_ent_concurrent_live', '实体', '实体', '实体',
                    'disabled', 'hash-ent-concurrent-live',
                    '2026-01-01T00:00:00Z'
                ),
                (
                    'kc_ent_concurrent_zero', 'kg_entity',
                    'kg_ent_concurrent_zero', '实体', '实体', '实体',
                    'usable', 'hash-ent-concurrent-zero',
                    '2026-01-01T00:00:00Z'
                ),
                (
                    'kc_rel_concurrent_live', 'kg_relation',
                    'kg_rel_concurrent_live', '关系', '关系', '关系',
                    'disabled', 'hash-rel-concurrent-live',
                    '2026-01-01T00:00:00Z'
                );
            DELETE FROM cyclops_schema_migrations
            WHERE id = '20260715_kg_live_state_repair_v1';
            """
        )

    start_event = threading.Event()
    ready_events = [threading.Event(), threading.Event()]
    errors: Queue[BaseException] = Queue()
    workers = [
        _SchemaDatabase(database_url, schema),
        _SchemaDatabase(database_url, schema),
    ]

    def initialize_schema(worker_db: _SchemaDatabase, ready_event: threading.Event) -> None:
        """等待统一起跑事件后执行完整初始化，并把线程异常回传主测试。"""
        try:
            ready_event.set()
            if not start_event.wait(timeout=10):
                raise TimeoutError("concurrent schema initialization did not start")
            worker_db.init_schema()
        except BaseException as exc:  # noqa: BLE001 - 线程异常必须回传主测试线程。
            errors.put(exc)

    threads = [
        threading.Thread(
            target=initialize_schema,
            args=(worker, ready_event),
            daemon=True,
        )
        for worker, ready_event in zip(workers, ready_events, strict=True)
    ]
    for thread in threads:
        thread.start()
    assert all(event.wait(timeout=10) for event in ready_events)
    start_event.set()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    if not errors.empty():
        raise errors.get()

    with seed_db.connect() as conn:
        marker_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM cyclops_schema_migrations
            WHERE id = '20260715_kg_live_state_repair_v1'
            """
        ).fetchone()["count"]
        entities = conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        relations = conn.execute(
            """
            SELECT id, status, review_revision
            FROM kg_relations
            ORDER BY id
            """
        ).fetchall()
        projections = conn.execute(
            """
            SELECT source_type, source_id, status
            FROM knowledge_chunks
            WHERE source_type IN ('kg_entity', 'kg_relation')
            ORDER BY source_type, source_id
            """
        ).fetchall()

    assert marker_count == 1
    assert entities == [
        {
            "id": "kg_ent_concurrent_live",
            "status": "needs_review",
            "review_revision": 4,
            "source_count": 1,
        },
        {
            "id": "kg_ent_concurrent_zero",
            "status": "disabled",
            "review_revision": 6,
            "source_count": 0,
        },
    ]
    assert relations == [
        {
            "id": "kg_rel_concurrent_live",
            "status": "needs_review",
            "review_revision": 8,
        }
    ]
    assert projections == [
        {
            "source_type": "kg_entity",
            "source_id": "kg_ent_concurrent_live",
            "status": "needs_review",
        },
        {
            "source_type": "kg_entity",
            "source_id": "kg_ent_concurrent_zero",
            "status": "disabled",
        },
        {
            "source_type": "kg_relation",
            "source_id": "kg_rel_concurrent_live",
            "status": "needs_review",
        },
    ]


def test_full_schema_migrates_persistent_parse_job_progress_contract(
    isolated_empty_schema: tuple[str, str],
) -> None:
    """旧文件运行列被删除，独立解析任务连续初始化后只接受 object progress。"""
    database_url, schema = isolated_empty_schema
    db = _SchemaDatabase(database_url, schema)
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE import_files (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                parser TEXT NOT NULL,
                chunker_type TEXT NOT NULL DEFAULT 'naive',
                status TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                parse_batch_id TEXT,
                parse_file_name TEXT,
                parse_progress JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_disabled BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            INSERT INTO import_files (
                id, original_name, stored_path, file_type, parser,
                chunker_type, status, parse_progress
            )
            VALUES (
                'imp_object', 'manual.pdf', '/tmp/manual.pdf', 'pdf', 'mineru',
                'naive', 'pending', '{"state":"pending"}'::jsonb
            );
            """
        )
    db.init_schema()
    db.init_schema()

    with db.connect() as conn:
        constraint_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM pg_constraint
            WHERE conname = 'import_parse_jobs_progress_object_check'
              AND conrelid = 'import_parse_jobs'::regclass
            """
        ).fetchone()["count"]
        legacy_columns = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'import_files'
              AND column_name IN ('parse_batch_id', 'parse_file_name', 'parse_progress')
            """
        ).fetchall()
    assert constraint_count == 1
    assert legacy_columns == []

    job = db.create_import_parse_job(
        "imp_object",
        chunker_type="naive",
        input_fingerprint="sha256:object-progress",
    )
    assert job["progress"] == {}

    for raw_progress in ('"running"', "[]", "null"):
        with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
            with db.connect() as conn:
                conn.execute(
                    """
                    UPDATE import_parse_jobs
                    SET progress = %(progress)s::jsonb
                    WHERE id = %(job_id)s
                    """,
                    {"job_id": job["id"], "progress": raw_progress},
                )
        assert (
            exc_info.value.diag.constraint_name
            == "import_parse_jobs_progress_object_check"
        )

    persisted = db.get_import_parse_job(job["id"])
    assert persisted is not None
    assert persisted["progress"] == {}


def test_faq_reenable_restores_live_owner_once_without_deleting_evidence(
    isolated_kg_schema: tuple[str, str],
) -> None:
    """FAQ 从 disabled 恢复 usable 后精确重算 owner，重复设置不得再次递增 revision。"""
    database_url, schema = isolated_kg_schema
    db = _SchemaDatabase(database_url, schema)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO faq_documents (id, question, answer, status)
            VALUES ('faq_restore', '如何导出？', '先检查权限。', 'disabled');
            INSERT INTO kg_entities (
                id, name, entity_type, status, review_revision, source_count
            )
            VALUES (
                'kg_ent_faq_restore', '报告导出', 'feature_ui_action',
                'disabled', 5, 0
            );
            INSERT INTO kg_evidence (
                id, entity_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES (
                'kg_ev_faq_restore', 'kg_ent_faq_restore', 'faq',
                'faq_restore', NULL, '先检查权限。', 0, 6
            );
            INSERT INTO knowledge_chunks (source_type, source_id, status)
            VALUES ('kg_entity', 'kg_ent_faq_restore', 'disabled');
            """
        )

    restored_rows = db.update_faq_statuses(["faq_restore"], "usable")
    assert len(restored_rows) == 1
    assert restored_rows[0]["status"] == "usable"
    with db.connect() as conn:
        restored_owner = conn.execute(
            """
            SELECT status, review_revision, source_count
            FROM kg_entities
            WHERE id = 'kg_ent_faq_restore'
            """
        ).fetchone()
        restored_projection = conn.execute(
            """
            SELECT status
            FROM knowledge_chunks
            WHERE source_type = 'kg_entity'
              AND source_id = 'kg_ent_faq_restore'
            """
        ).fetchone()
        evidence_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM kg_evidence
            WHERE entity_id = 'kg_ent_faq_restore'
            """
        ).fetchone()["count"]
    assert restored_owner == {
        "status": "needs_review",
        "review_revision": 6,
        "source_count": 1,
    }
    assert restored_projection == {"status": "needs_review"}
    assert evidence_count == 1

    repeated_rows = db.update_faq_statuses(["faq_restore"], "usable")
    assert len(repeated_rows) == 1
    assert repeated_rows[0]["status"] == "usable"
    with db.connect() as conn:
        repeated_owner = conn.execute(
            """
            SELECT status, review_revision, source_count
            FROM kg_entities
            WHERE id = 'kg_ent_faq_restore'
            """
        ).fetchone()
        repeated_evidence_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM kg_evidence
            WHERE entity_id = 'kg_ent_faq_restore'
            """
        ).fetchone()["count"]
    assert repeated_owner == restored_owner
    assert repeated_evidence_count == 1


def test_source_disable_and_reenable_recounts_distinct_live_entity_source_count(
    isolated_kg_schema: tuple[str, str],
) -> None:
    """文件禁用与恢复必须按唯一 locator 精确重算独占及共享实体来源数。"""
    database_url, schema = isolated_kg_schema
    db = _SchemaDatabase(database_url, schema)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO faq_documents (id, question, answer, status)
            VALUES ('faq_shared', '如何导出？', '先检查权限。', 'usable');
            INSERT INTO import_files (id, is_disabled)
            VALUES ('imp_lifecycle', false);
            INSERT INTO import_chunks (id, file_id, is_disabled)
            VALUES ('chunk_lifecycle', 'imp_lifecycle', false);
            INSERT INTO kg_entities (
                id, name, entity_type, status, review_revision, source_count
            )
            VALUES
                (
                    'kg_ent_only', '独占来源实体', 'feature_ui_action',
                    'usable', 3, 1
                ),
                (
                    'kg_ent_shared', '共享来源实体', 'condition_policy',
                    'usable', 7, 2
                );
            INSERT INTO kg_evidence (
                id, entity_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES
                (
                    'kg_ev_only_document', 'kg_ent_only', 'document',
                    'imp_lifecycle', 'chunk_lifecycle', '独占文档证据', 0, 6
                ),
                (
                    'kg_ev_shared_document', 'kg_ent_shared', 'document',
                    'imp_lifecycle', 'chunk_lifecycle', '共享文档证据', 0, 6
                ),
                (
                    'kg_ev_shared_faq_a', 'kg_ent_shared', 'faq',
                    'faq_shared', NULL, '共享 FAQ 证据一', 0, 10
                ),
                (
                    'kg_ev_shared_faq_b', 'kg_ent_shared', 'faq',
                    'faq_shared', NULL, '共享 FAQ 证据二', 0, 10
                );
            INSERT INTO knowledge_chunks (source_type, source_id, status)
            VALUES
                ('kg_entity', 'kg_ent_only', 'usable'),
                ('kg_entity', 'kg_ent_shared', 'usable');
            """
        )

    disabled_file = db.set_import_file_disabled("imp_lifecycle", True)
    assert disabled_file is not None
    assert disabled_file["is_disabled"] is True
    with db.connect() as conn:
        disabled_entities = conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        disabled_projections = conn.execute(
            """
            SELECT source_id, status
            FROM knowledge_chunks
            WHERE source_type = 'kg_entity'
            ORDER BY source_id
            """
        ).fetchall()
    assert disabled_entities == [
        {
            "id": "kg_ent_only",
            "status": "disabled",
            "review_revision": 4,
            "source_count": 0,
        },
        {
            "id": "kg_ent_shared",
            "status": "needs_review",
            "review_revision": 8,
            "source_count": 1,
        },
    ]
    assert disabled_projections == [
        {"source_id": "kg_ent_only", "status": "disabled"},
        {"source_id": "kg_ent_shared", "status": "needs_review"},
    ]
    repeated_disabled_file = db.set_import_file_disabled("imp_lifecycle", True)
    assert repeated_disabled_file is not None
    assert repeated_disabled_file["is_disabled"] is True
    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall() == disabled_entities

    enabled_file = db.set_import_file_disabled("imp_lifecycle", False)
    assert enabled_file is not None
    assert enabled_file["is_disabled"] is False
    with db.connect() as conn:
        enabled_entities = conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        enabled_projections = conn.execute(
            """
            SELECT source_id, status
            FROM knowledge_chunks
            WHERE source_type = 'kg_entity'
            ORDER BY source_id
            """
        ).fetchall()
    assert enabled_entities == [
        {
            "id": "kg_ent_only",
            "status": "needs_review",
            "review_revision": 5,
            "source_count": 1,
        },
        {
            "id": "kg_ent_shared",
            "status": "needs_review",
            "review_revision": 9,
            "source_count": 2,
        },
    ]
    assert enabled_projections == [
        {"source_id": "kg_ent_only", "status": "needs_review"},
        {"source_id": "kg_ent_shared", "status": "needs_review"},
    ]
    repeated_enabled_file = db.set_import_file_disabled("imp_lifecycle", False)
    assert repeated_enabled_file is not None
    assert repeated_enabled_file["is_disabled"] is False
    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall() == enabled_entities

    disabled_chunk = db.set_import_chunk_disabled("chunk_lifecycle", True)
    assert disabled_chunk is not None
    assert disabled_chunk["is_disabled"] is True
    with db.connect() as conn:
        chunk_disabled_entities = conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
    assert chunk_disabled_entities == [
        {
            "id": "kg_ent_only",
            "status": "disabled",
            "review_revision": 6,
            "source_count": 0,
        },
        {
            "id": "kg_ent_shared",
            "status": "needs_review",
            "review_revision": 10,
            "source_count": 1,
        },
    ]
    repeated_disabled_chunk = db.set_import_chunk_disabled("chunk_lifecycle", True)
    assert repeated_disabled_chunk is not None
    assert repeated_disabled_chunk["is_disabled"] is True
    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall() == chunk_disabled_entities

    enabled_chunk = db.set_import_chunk_disabled("chunk_lifecycle", False)
    assert enabled_chunk is not None
    assert enabled_chunk["is_disabled"] is False
    with db.connect() as conn:
        chunk_enabled_entities = conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
    assert chunk_enabled_entities == [
        {
            "id": "kg_ent_only",
            "status": "needs_review",
            "review_revision": 7,
            "source_count": 1,
        },
        {
            "id": "kg_ent_shared",
            "status": "needs_review",
            "review_revision": 11,
            "source_count": 2,
        },
    ]
    repeated_enabled_chunk = db.set_import_chunk_disabled("chunk_lifecycle", False)
    assert repeated_enabled_chunk is not None
    assert repeated_enabled_chunk["is_disabled"] is False
    with db.connect() as conn:
        assert conn.execute(
            """
            SELECT id, status, review_revision, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall() == chunk_enabled_entities


def test_relation_only_source_invalidation_preserves_lock_only_endpoints(
    isolated_kg_schema: tuple[str, str],
) -> None:
    """关系独占来源禁用或恢复时，只重算关系，锁定端点及投影必须原样。"""
    database_url, schema = isolated_kg_schema
    db = _SchemaDatabase(database_url, schema)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO import_files (id, is_disabled)
            VALUES ('imp_relation_only', false);
            INSERT INTO import_chunks (id, file_id, is_disabled)
            VALUES ('chunk_relation_only', 'imp_relation_only', false);
            INSERT INTO kg_entities (
                id, name, entity_type, status, review_revision, updated_at
            )
            VALUES
                (
                    'kg_ent_head', '报告导出', 'feature_ui_action', 'usable', 7,
                    '2026-01-01T00:00:00Z'
                ),
                (
                    'kg_ent_tail', '账号权限', 'condition_policy', 'usable', 9,
                    '2026-01-01T00:00:00Z'
                );
            INSERT INTO kg_relations (
                id, head_entity_id, relation_type, tail_entity_id,
                status, review_revision
            )
            VALUES (
                'kg_rel_relation_only', 'kg_ent_head', 'requires', 'kg_ent_tail',
                'usable', 4
            );
            INSERT INTO kg_evidence (
                id, relation_id, source_type, source_id, source_chunk_id, excerpt,
                char_start, char_end
            )
            VALUES (
                'kg_ev_relation_only', 'kg_rel_relation_only', 'document',
                'imp_relation_only', 'chunk_relation_only', '导出需要账号权限', 0, 8
            );
            INSERT INTO knowledge_chunks (
                source_type, source_id, status, updated_at
            )
            VALUES
                ('kg_entity', 'kg_ent_head', 'usable', '2026-01-01T00:00:00Z'),
                ('kg_entity', 'kg_ent_tail', 'usable', '2026-01-01T00:00:00Z'),
                (
                    'kg_relation', 'kg_rel_relation_only', 'usable',
                    '2026-01-01T00:00:00Z'
                );
            """
        )
        before_entities = conn.execute(
            """
            SELECT id, status, review_revision, updated_at
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        before_entity_projections = conn.execute(
            """
            SELECT source_type, source_id, status, updated_at
            FROM knowledge_chunks
            WHERE source_type = 'kg_entity'
            ORDER BY source_id
            """
        ).fetchall()

    updated_file = db.set_import_file_disabled("imp_relation_only", True)
    assert updated_file is not None
    assert updated_file["is_disabled"] is True

    with db.connect() as conn:
        after_entities = conn.execute(
            """
            SELECT id, status, review_revision, updated_at
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        relation = conn.execute(
            """
            SELECT status, review_revision
            FROM kg_relations
            WHERE id = 'kg_rel_relation_only'
            """
        ).fetchone()
        after_entity_projections = conn.execute(
            """
            SELECT source_type, source_id, status, updated_at
            FROM knowledge_chunks
            WHERE source_type = 'kg_entity'
            ORDER BY source_id
            """
        ).fetchall()
        relation_projection = conn.execute(
            """
            SELECT status
            FROM knowledge_chunks
            WHERE source_type = 'kg_relation'
              AND source_id = 'kg_rel_relation_only'
            """
        ).fetchone()
        evidence_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM kg_evidence
            WHERE id = 'kg_ev_relation_only'
            """
        ).fetchone()["count"]
    assert after_entities == before_entities
    assert after_entity_projections == before_entity_projections
    assert relation == {"status": "disabled", "review_revision": 5}
    assert relation_projection == {"status": "disabled"}
    assert evidence_count == 1

    enabled_file = db.set_import_file_disabled("imp_relation_only", False)
    assert enabled_file is not None
    assert enabled_file["is_disabled"] is False
    with db.connect() as conn:
        reenabled_entities = conn.execute(
            """
            SELECT id, status, review_revision, updated_at
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        reenabled_entity_projections = conn.execute(
            """
            SELECT source_type, source_id, status, updated_at
            FROM knowledge_chunks
            WHERE source_type = 'kg_entity'
            ORDER BY source_id
            """
        ).fetchall()
        reenabled_relation = conn.execute(
            """
            SELECT status, review_revision
            FROM kg_relations
            WHERE id = 'kg_rel_relation_only'
            """
        ).fetchone()
        reenabled_relation_projection = conn.execute(
            """
            SELECT status
            FROM knowledge_chunks
            WHERE source_type = 'kg_relation'
              AND source_id = 'kg_rel_relation_only'
            """
        ).fetchone()
    assert reenabled_entities == before_entities
    assert reenabled_entity_projections == before_entity_projections
    assert reenabled_relation == {"status": "needs_review", "review_revision": 6}
    assert reenabled_relation_projection == {"status": "needs_review"}


def test_snapshot_replacement_and_relation_confirmation_do_not_deadlock(
    isolated_kg_schema: tuple[str, str],
) -> None:
    """候选 A 必须在旧实体 Z/关系 R 前仲裁，使确认事务只能等待而不会形成环。"""
    database_url, schema = isolated_kg_schema
    source_entity_id = "kg_ent_z_source"
    candidate_entity_id = "kg_ent_a_candidate"
    relation_id = "kg_rel_middle"
    seed_db = _SchemaDatabase(database_url, schema)
    with seed_db.connect() as conn:
        conn.execute(
            """
            INSERT INTO faq_documents (id, question, answer, status)
            VALUES ('faq_1', '如何导出？', '先检查权限。', 'usable')
            """
        )
        conn.execute(
            """
            INSERT INTO kg_entities (id, name, entity_type, status)
            VALUES
                (%(candidate_id)s, '旧候选', 'feature_ui_action', 'usable'),
                (%(source_id)s, '旧来源实体', 'condition_policy', 'usable')
            """,
            {"candidate_id": candidate_entity_id, "source_id": source_entity_id},
        )
        conn.execute(
            """
            INSERT INTO kg_relations (
                id, head_entity_id, relation_type, tail_entity_id, status
            )
            VALUES (%(id)s, %(candidate_id)s, 'requires', %(source_id)s, 'usable')
            """,
            {
                "id": relation_id,
                "candidate_id": candidate_entity_id,
                "source_id": source_entity_id,
            },
        )
        conn.execute(
            """
            INSERT INTO kg_evidence (
                id, entity_id, source_type, source_id, excerpt, char_start, char_end
            )
            VALUES ('kg_ev_old', %(entity_id)s, 'faq', 'faq_1', '旧来源证据', 0, 5)
            """,
            {"entity_id": source_entity_id},
        )
        conn.execute(
            """
            INSERT INTO kg_evidence (
                id, relation_id, source_type, source_id, excerpt, char_start, char_end
            )
            VALUES ('kg_ev_relation', %(relation_id)s, 'faq', 'faq_1', '关系证据', 0, 4)
            """,
            {"relation_id": relation_id},
        )

    reviewer_holds_candidate = threading.Event()
    entity_phase_started = threading.Event()
    replacement_waiting = threading.Event()
    errors: Queue[BaseException] = Queue()
    replacement_db = _CoordinatedDatabase(
        database_url,
        schema,
        entity_phase_started,
    )
    reviewer_db = _BorrowedConnectionDatabase(database_url, schema)
    extraction = {
        "entities": [
            {
                "id": candidate_entity_id,
                "name": "新候选",
                "entity_type": "feature_ui_action",
                "aliases": [],
                "description": "新快照",
                "confidence": 0.9,
                "evidence": [
                    {
                        "source_type": "faq",
                        "source_id": "faq_1",
                        "source_chunk_id": None,
                        "source_title": "如何导出？",
                        "section_path": [],
                        "page_start": None,
                            "page_end": None,
                            "excerpt": "先检查权限。",
                            "char_start": 12,
                            "char_end": 18,
                        }
                ],
            }
        ],
        "relations": [],
    }

    def confirm_relation_locks() -> None:
        """让公开关系确认复用已持有 A 的事务，覆盖生产端点锁和 revision 门禁。"""
        try:
            with reviewer_db.connect() as conn:
                conn.execute(
                    reviewer_db._lock_kg_entity_review_sql(),
                    {"id": candidate_entity_id},
                ).fetchone()
                reviewer_holds_candidate.set()
                if not replacement_waiting.wait(timeout=10):
                    raise TimeoutError("snapshot replacement did not wait for candidate lock")
                with reviewer_db.borrow(conn):
                    reviewer_db.confirm_kg_relation(
                        relation_id,
                        expected_revision=1,
                    )
        except BaseException as exc:  # noqa: BLE001 - 线程异常必须回传主测试线程。
            errors.put(exc)

    def replace_snapshot() -> None:
        """运行真实来源替换事务，任何数据库异常都交回主测试线程断言。"""
        try:
            with replacement_db.connect() as conn:
                replacement_db._replace_kg_source_snapshot_in_conn(
                    conn,
                    source_type="faq",
                    source_ids=["faq_1"],
                    source_chunk_ids=None,
                    extraction=extraction,
                )
        except BaseException as exc:  # noqa: BLE001 - 线程异常必须回传主测试线程。
            errors.put(exc)

    reviewer = threading.Thread(target=confirm_relation_locks, daemon=True)
    replacement = threading.Thread(target=replace_snapshot, daemon=True)
    reviewer.start()
    assert reviewer_holds_candidate.wait(timeout=10)
    replacement.start()
    assert entity_phase_started.wait(timeout=10)
    assert replacement_db.backend_pid is not None
    _wait_for_backend_lock(
        database_url,
        replacement_db.backend_pid,
        timeout=10,
    )
    replacement_waiting.set()
    reviewer.join(timeout=15)
    replacement.join(timeout=15)

    assert not reviewer.is_alive()
    assert not replacement.is_alive()
    if not errors.empty():
        raise errors.get()

    with seed_db.connect() as conn:
        entity = conn.execute(
            "SELECT status, review_revision FROM kg_entities WHERE id = %(id)s",
            {"id": source_entity_id},
        ).fetchone()
        relation = conn.execute(
            "SELECT status, review_revision FROM kg_relations WHERE id = %(id)s",
            {"id": relation_id},
        ).fetchone()
    assert entity == {"status": "disabled", "review_revision": 2}
    assert relation == {"status": "disabled", "review_revision": 2}
