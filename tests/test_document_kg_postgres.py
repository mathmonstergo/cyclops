from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Any, Iterator
import uuid

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from cyclops.db import Database
from cyclops.document_kg import (
    localize_document_kg_map_result,
    premerge_document_kg_map_results,
    reduce_document_kg,
)
from cyclops.kg import canonical_kg_entity_id, canonical_kg_relation_id


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for document KG PostgreSQL tests",
)


class _SchemaDatabase(Database):
    """把生产 Database 方法固定到随机 schema，禁止测试触碰业务表。"""

    def __init__(self, database_url: str, schema: str):
        """保存可信随机 schema 名称，连接时同时保留 public 扩展可见性。"""
        super().__init__(database_url)
        self.schema = schema

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """为每个生产事务设置隔离 search_path 和有限锁等待。"""
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            conn.execute(
                sql.SQL("SET search_path TO {}, public").format(
                    sql.Identifier(self.schema)
                )
            )
            conn.execute("SET lock_timeout = '5s'")
            yield conn


@pytest.fixture
def isolated_document_kg_schema() -> Iterator[_SchemaDatabase]:
    """创建完整生产 schema，并在测试后级联删除随机 schema。"""
    schema = f"cyclops_document_kg_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = _SchemaDatabase(TEST_DATABASE_URL, schema)
    try:
        database.init_schema()
        yield database
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def _document_chunk(
    file_id: str,
    chunk_id: str,
    chunk_index: int,
    source_text: str,
    *,
    section_path: list[str],
    page_start: int,
) -> dict[str, Any]:
    """构造可由生产 replace_import_chunks 保存的完整切片。"""
    return {
        "id": chunk_id,
        "file_id": file_id,
        "chunk_index": chunk_index,
        "section_path": section_path,
        "page_start": page_start,
        "page_end": page_start,
        "block_type": "text",
        "source_offsets": {"start": 0, "end": len(source_text)},
        "source_blocks": [],
        "children_delimiter": "",
        "start_at": None,
        "end_at": None,
        "message_count": 0,
        "keywords": [],
        "source_text": source_text,
        "status": "pending",
        "candidate_count": 0,
    }


def _create_document(
    database: _SchemaDatabase,
    file_id: str,
    chunks: list[dict[str, Any]],
    *,
    title: str = "产品手册.pdf",
) -> None:
    """用生产文件与切片入口创建可进入 document KG 的来源。"""
    database.create_import_file(
        {
            "id": file_id,
            "original_name": title,
            "stored_path": f"/tmp/{file_id}.pdf",
            "file_type": "pdf",
            "parser": "mineru",
            "chunker_type": "naive",
            "status": "needs_review",
        }
    )
    database.replace_import_chunks(file_id, chunks)


def _source_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """读取 Map item 返回的 canonical source locator，避免测试重建字段。"""
    return dict(item["source"])


def _evidence(item: dict[str, Any], excerpt: str) -> dict[str, Any]:
    """用 source_text 的 Python Unicode 下标构造精确 evidence。"""
    source_text = item["source_text"]
    char_start = source_text.index(excerpt)
    return {
        **_source_from_item(item),
        "excerpt": excerpt,
        "char_start": char_start,
        "char_end": char_start + len(excerpt),
    }


def _raw_map_extraction(
    item: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """按 Map spec 生成模型 canonical payload，所有 evidence 来自当前切片原文。"""
    entity_ids: dict[str, str] = {}
    entities: list[dict[str, Any]] = []
    for entity_spec in spec.get("entities", []):
        entity_id = canonical_kg_entity_id(
            entity_spec["name"],
            entity_spec["entity_type"],
        )
        entity_ids[entity_spec["key"]] = entity_id
        entities.append(
            {
                "id": entity_id,
                "name": entity_spec["name"],
                "entity_type": entity_spec["entity_type"],
                "aliases": list(entity_spec.get("aliases", [])),
                "description": entity_spec.get("description", ""),
                "status": "needs_review",
                "confidence": entity_spec.get("confidence", 0.9),
                "evidence": [
                    _evidence(item, excerpt)
                    for excerpt in entity_spec["evidence"]
                ],
            }
        )
    relations: list[dict[str, Any]] = []
    for relation_spec in spec.get("relations", []):
        head_id = entity_ids[relation_spec["head"]]
        tail_id = entity_ids[relation_spec["tail"]]
        relation_id = canonical_kg_relation_id(
            head_id,
            relation_spec["relation_type"],
            tail_id,
        )
        head = next(entity for entity in entities if entity["id"] == head_id)
        tail = next(entity for entity in entities if entity["id"] == tail_id)
        relations.append(
            {
                "id": relation_id,
                "head_entity_id": head_id,
                "head_entity_name": head["name"],
                "head_entity_type": head["entity_type"],
                "relation_type": relation_spec["relation_type"],
                "tail_entity_id": tail_id,
                "tail_entity_name": tail["name"],
                "tail_entity_type": tail["entity_type"],
                "description": relation_spec.get("description", ""),
                "status": "needs_review",
                "confidence": relation_spec.get("confidence", 0.85),
                "evidence": [
                    _evidence(item, excerpt)
                    for excerpt in relation_spec["evidence"]
                ],
            }
        )
    return {"entities": entities, "relations": relations}


def _complete_next_map_item(
    database: _SchemaDatabase,
    job_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """领取并完成一个 Map item，完整复用生产 lease、manifest 和 staging 门禁。"""
    claimed = database.claim_kg_extraction_job(lease_seconds=30)
    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["phase"] == "mapping"
    item = database.load_document_kg_map_item(
        job_id,
        lease_token=claimed["lease_token"],
    )
    assert item is not None
    raw_extraction = _raw_map_extraction(item, spec)
    localized = localize_document_kg_map_result(
        raw_extraction,
        job_id=job_id,
        chunk_id=item["chunk_id"],
        chunk_order=item["chunk_order"],
    )
    completed = database.complete_document_kg_map_item(
        job_id,
        item["id"],
        lease_token=claimed["lease_token"],
        map_result=localized,
    )
    assert completed["phase"] in {"mapping", "resolving"}
    return localized


def _complete_map_items(
    database: _SchemaDatabase,
    job_id: str,
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 immutable manifest 顺序完成全部 Map items 并返回持久化 JSON。"""
    return [_complete_next_map_item(database, job_id, spec) for spec in specs]


def _save_resolution(
    database: _SchemaDatabase,
    job_id: str,
    map_results: list[dict[str, Any]],
    resolution: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """领取 resolving lease、重读全部 Map staging 并保存 validated resolution。"""
    claimed = database.claim_kg_extraction_job(lease_seconds=30)
    assert claimed is not None
    assert claimed["phase"] == "resolving"
    persisted = database.load_document_kg_map_results(
        job_id,
        lease_token=claimed["lease_token"],
    )
    assert persisted == map_results
    premerged = premerge_document_kg_map_results(persisted)
    saved = database.save_document_kg_resolution(
        job_id,
        lease_token=claimed["lease_token"],
        resolution_result=resolution,
    )
    assert saved["phase"] == "reducing"
    return premerged


def _claim_and_reduce(
    database: _SchemaDatabase,
    job_id: str,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """领取 reducing lease、重读 staging/resolution 并调用确定性 Reduce。"""
    claimed = database.claim_kg_extraction_job(lease_seconds=30)
    assert claimed is not None
    assert claimed["phase"] == "reducing"
    persisted = database.load_document_kg_map_results(
        job_id,
        lease_token=claimed["lease_token"],
    )
    premerged = premerge_document_kg_map_results(persisted)
    reduced = reduce_document_kg(premerged, claimed["resolution_result"])
    return claimed, reduced


def _expire_lease(database: _SchemaDatabase, job_id: str) -> None:
    """在隔离 schema 内直接让当前 lease 过期，模拟 worker 崩溃而不等待 wall clock。"""
    with database.connect() as conn:
        conn.execute(
            """
            UPDATE kg_extraction_jobs
            SET lease_expires_at = now() - interval '1 second'
            WHERE id = %(job_id)s
            """,
            {"job_id": job_id},
        )


def _snapshot(database: _SchemaDatabase) -> dict[str, list[dict[str, Any]]]:
    """读取 owner/evidence 的完整行集，比较来源失败前后的精确快照。"""
    with database.connect() as conn:
        return {
            "entities": conn.execute(
                "SELECT * FROM kg_entities ORDER BY id"
            ).fetchall(),
            "relations": conn.execute(
                "SELECT * FROM kg_relations ORDER BY id"
            ).fetchall(),
            "evidence": conn.execute(
                "SELECT * FROM kg_evidence ORDER BY id"
            ).fetchall(),
        }


def _review_ids(database: _SchemaDatabase) -> tuple[list[str], list[str]]:
    """通过生产审核列表读取当前可见 owner，确保 staging 不伪装成 canonical snapshot。"""
    entities = database.list_kg_entities(limit=100, offset=0)["items"]
    relations = database.list_kg_relations(limit=100, offset=0)["items"]
    return (
        [row["id"] for row in entities],
        [row["id"] for row in relations],
    )


def _assert_offsets(database: _SchemaDatabase, file_id: str) -> None:
    """核对落库 evidence 的每个 Unicode offset 与对应当前 chunk substring 完全一致。"""
    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT evidence.*, chunk.source_text
            FROM kg_evidence AS evidence
            JOIN import_chunks AS chunk
              ON evidence.source_type = 'document'
             AND chunk.id = evidence.source_chunk_id
             AND chunk.file_id = evidence.source_id
            WHERE evidence.source_id = %(file_id)s
            ORDER BY evidence.id
            """,
            {"file_id": file_id},
        ).fetchall()
    assert rows
    for row in rows:
        assert row["source_text"][row["char_start"] : row["char_end"]] == row["excerpt"]


def _seed_old_snapshot(
    database: _SchemaDatabase,
    file_id: str,
    chunk: dict[str, Any],
) -> tuple[str, str, str]:
    """写入一份可审计旧 owner/evidence snapshot，供失败回归做字节级比较。"""
    source_text = chunk["source_text"]
    excerpt_head = "控制台"
    excerpt_tail = "管理员权限"
    excerpt_relation = "控制台需要管理员权限"
    source_title = "产品手册.pdf"
    section_path = '["导出"]'  # JSON literal is fixed test data, not a schema name.
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO kg_entities (
                id, name, entity_type, status, review_revision, source_count
            )
            VALUES
                ('kg_ent_old_head', '旧控制台', 'feature_ui_action', 'usable', 4, 1),
                ('kg_ent_old_tail', '旧权限', 'role_permission_channel', 'usable', 5, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO kg_relations (
                id, head_entity_id, relation_type, tail_entity_id,
                status, review_revision
            )
            VALUES (
                'kg_rel_old_requires', 'kg_ent_old_head', 'requires',
                'kg_ent_old_tail', 'usable', 6
            )
            """
        )
        conn.execute(
            """
            INSERT INTO kg_evidence (
                id, entity_id, relation_id, source_type, source_id, source_chunk_id,
                source_title, section_path, page_start, page_end, excerpt,
                char_start, char_end
            )
            VALUES
                (
                    'kg_ev_old_head', 'kg_ent_old_head', NULL, 'document',
                    %(file_id)s, %(chunk_id)s, %(source_title)s,
                    %(section_path)s::jsonb, %(page_start)s, %(page_end)s,
                    %(excerpt_head)s, %(head_start)s, %(head_end)s
                ),
                (
                    'kg_ev_old_tail', 'kg_ent_old_tail', NULL, 'document',
                    %(file_id)s, %(chunk_id)s, %(source_title)s,
                    %(section_path)s::jsonb, %(page_start)s, %(page_end)s,
                    %(excerpt_tail)s, %(tail_start)s, %(tail_end)s
                ),
                (
                    'kg_ev_old_relation', NULL, 'kg_rel_old_requires', 'document',
                    %(file_id)s, %(chunk_id)s, %(source_title)s,
                    %(section_path)s::jsonb, %(page_start)s, %(page_end)s,
                    %(excerpt_relation)s, %(relation_start)s, %(relation_end)s
                )
            """,
            {
                "file_id": file_id,
                "chunk_id": chunk["id"],
                "source_title": source_title,
                "section_path": section_path,
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
                "excerpt_head": excerpt_head,
                "head_start": source_text.index(excerpt_head),
                "head_end": source_text.index(excerpt_head) + len(excerpt_head),
                "excerpt_tail": excerpt_tail,
                "tail_start": source_text.index(excerpt_tail),
                "tail_end": source_text.index(excerpt_tail) + len(excerpt_tail),
                "excerpt_relation": excerpt_relation,
                "relation_start": source_text.index(excerpt_relation),
                "relation_end": source_text.index(excerpt_relation) + len(excerpt_relation),
            },
        )
    return "kg_ent_old_head", "kg_ent_old_tail", "kg_rel_old_requires"


def _simple_entity_spec(
    name: str,
    entity_type: str,
    evidence: list[str],
    *,
    key: str = "entity",
) -> dict[str, Any]:
    """构造无关系 Map 的实体 spec，供多个真实流程复用。"""
    return {
        "key": key,
        "name": name,
        "entity_type": entity_type,
        "evidence": evidence,
        "description": f"{name}说明",
        "confidence": 0.9,
    }


def _relation_spec(
    head: str,
    tail: str,
    evidence: list[str],
    *,
    relation_type: str = "requires",
) -> dict[str, Any]:
    """构造 Map 内显式关系 spec，端点只引用同片实体 key。"""
    return {
        "head": head,
        "tail": tail,
        "relation_type": relation_type,
        "evidence": evidence,
        "description": "来源关系说明",
        "confidence": 0.85,
    }


def _entity_relation_spec(
    head_name: str,
    tail_name: str,
    relation_evidence: list[str],
    *,
    head_evidence: list[str] | None = None,
    tail_evidence: list[str] | None = None,
    head_type: str = "product_platform_module",
    tail_type: str = "role_permission_channel",
) -> dict[str, Any]:
    """构造含两个实体和一条 Map 关系的完整 spec。"""
    return {
        "entities": [
            _simple_entity_spec(
                head_name,
                head_type,
                list(head_evidence or [head_name]),
                key="head",
            ),
            _simple_entity_spec(
                tail_name,
                tail_type,
                list(tail_evidence or [tail_name]),
                key="tail",
            ),
        ],
        "relations": [_relation_spec("head", "tail", relation_evidence)],
    }


@pytest.mark.parametrize("manifest_change", ["file_title", "chunk_locator"])
def test_document_kg_final_fence_keeps_old_snapshot_until_valid_manifest(
    isolated_document_kg_schema: _SchemaDatabase,
    manifest_change: str,
) -> None:
    """Map/resolution 只写 staging；manifest 变化时 final 失败且旧审核 snapshot 原样保留。"""
    database = isolated_document_kg_schema
    file_id = f"imp_manifest_{manifest_change}"
    chunks = [
        _document_chunk(
            file_id,
            "chunk_manifest_a",
            0,
            "控制台需要管理员权限。",
            section_path=["导出"],
            page_start=1,
        ),
        _document_chunk(
            file_id,
            "chunk_manifest_b",
            1,
            "故障时联系技术支持。",
            section_path=["故障"],
            page_start=2,
        ),
    ]
    _create_document(database, file_id, chunks)
    old_ids = _seed_old_snapshot(database, file_id, chunks[0])
    before_snapshot = _snapshot(database)
    job = database.create_document_kg_extraction_job(file_id, model="test-model")
    specs = [
        {
            "entities": [
                _simple_entity_spec(
                    "控制台",
                    "product_platform_module",
                    ["控制台"],
                )
            ],
            "relations": [],
        },
        {
            "entities": [
                _simple_entity_spec(
                    "技术支持",
                    "role_permission_channel",
                    ["技术支持"],
                )
            ],
            "relations": [],
        },
    ]
    map_results = _complete_map_items(database, job["id"], specs)
    assert _review_ids(database) == (list(old_ids[:2]), [old_ids[2]])
    premerged = _save_resolution(
        database,
        job["id"],
        map_results,
        {"groups": []},
    )
    assert _review_ids(database) == (list(old_ids[:2]), [old_ids[2]])

    if manifest_change == "file_title":
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE import_files
                SET original_name = '改名后的手册.pdf'
                WHERE id = %(file_id)s
                """,
                {"file_id": file_id},
            )
    else:
        with database.connect() as conn:
            conn.execute(
                """
                UPDATE import_chunks
                SET section_path = '["改后章节"]'::jsonb, page_start = 99, page_end = 99
                WHERE id = 'chunk_manifest_a'
                """
            )

    reducing, reduced = _claim_and_reduce(database, job["id"])
    with pytest.raises(ValueError, match="manifest|source"):
        database.complete_document_kg_extraction_job(
            job["id"],
            lease_token=reducing["lease_token"],
            extraction=reduced,
        )
    failed = database.fail_kg_extraction_job(
        job["id"],
        lease_token=reducing["lease_token"],
        error="manifest changed",
    )
    assert failed["phase"] == "failed"
    assert _snapshot(database) == before_snapshot
    assert _review_ids(database) == (list(old_ids[:2]), [old_ids[2]])
    local_ids = {
        entity["local_entity_id"]
        for result in map_results
        for entity in result["entities"]
    }
    canonical_ids = {entity["id"] for entity in reduced["entities"]}
    with database.connect() as conn:
        candidates = conn.execute(
            """
            SELECT id
            FROM kg_entities
            WHERE id = ANY(%(ids)s::text[])
            ORDER BY id
            """,
            {"ids": sorted(local_ids | canonical_ids)},
        ).fetchall()
        staging = conn.execute(
            """
            SELECT count(*) AS count
            FROM kg_extraction_job_items
            WHERE job_id = %(job_id)s AND map_result IS NOT NULL
            """,
            {"job_id": job["id"]},
        ).fetchone()
    assert candidates == []
    assert staging == {"count": 0}
    assert premerged["relations"] == []


def test_document_kg_reparse_invalidates_old_evidence_before_failed_final(
    isolated_document_kg_schema: _SchemaDatabase,
) -> None:
    """生产正文更新先使旧 evidence 失效，随后失败 generation 不得写入新 candidate staging。"""
    database = isolated_document_kg_schema
    file_id = "imp_reparse_failure"
    chunks = [
        _document_chunk(
            file_id,
            "chunk_reparse_a",
            0,
            "控制台需要管理员权限。",
            section_path=["导出"],
            page_start=1,
        ),
        _document_chunk(
            file_id,
            "chunk_reparse_b",
            1,
            "故障时联系技术支持。",
            section_path=["故障"],
            page_start=2,
        ),
    ]
    _create_document(database, file_id, chunks)
    old_ids = _seed_old_snapshot(database, file_id, chunks[0])
    job = database.create_document_kg_extraction_job(file_id, model="test-model")
    specs = [
        {
            "entities": [
                _simple_entity_spec(
                    "控制台",
                    "product_platform_module",
                    ["控制台"],
                )
            ],
            "relations": [],
        },
        {
            "entities": [
                _simple_entity_spec(
                    "技术支持",
                    "role_permission_channel",
                    ["技术支持"],
                )
            ],
            "relations": [],
        },
    ]
    map_results = _complete_map_items(database, job["id"], specs)
    _save_resolution(database, job["id"], map_results, {"groups": []})
    updated = database.update_import_chunk_text(
        "chunk_reparse_a",
        "正文已经重新解析并改变。",
    )
    assert updated["source_text"] == "正文已经重新解析并改变。"
    with database.connect() as conn:
        invalidated = conn.execute(
            """
            SELECT count(*) AS count
            FROM kg_evidence
            WHERE source_type = 'document' AND source_id = %(file_id)s
            """,
            {"file_id": file_id},
        ).fetchone()
    assert invalidated == {"count": 0}

    reducing, reduced = _claim_and_reduce(database, job["id"])
    with pytest.raises(ValueError, match="manifest|source"):
        database.complete_document_kg_extraction_job(
            job["id"],
            lease_token=reducing["lease_token"],
            extraction=reduced,
        )
    failed = database.fail_kg_extraction_job(
        job["id"],
        lease_token=reducing["lease_token"],
        error="source changed",
    )
    assert failed["phase"] == "failed"
    with database.connect() as conn:
        owners = conn.execute(
            """
            SELECT id, status, source_count
            FROM kg_entities
            ORDER BY id
            """
        ).fetchall()
        relation = conn.execute(
            """
            SELECT status, review_revision
            FROM kg_relations
            WHERE id = 'kg_rel_old_requires'
            """
        ).fetchone()
        candidate_count = conn.execute(
            """
            SELECT count(*) AS count
            FROM kg_entities
            WHERE id = ANY(%(ids)s::text[])
            """,
            {
                "ids": sorted(
                    {
                        entity["id"]
                        for entity in reduced["entities"]
                    }
                )
            },
        ).fetchone()
        staging = conn.execute(
            """
            SELECT count(*) AS count
            FROM kg_extraction_job_items
            WHERE job_id = %(job_id)s AND map_result IS NOT NULL
            """,
            {"job_id": job["id"]},
        ).fetchone()
    assert owners == [
        {"id": "kg_ent_old_head", "status": "disabled", "source_count": 0},
        {"id": "kg_ent_old_tail", "status": "disabled", "source_count": 0},
    ]
    assert relation["status"] == "disabled"
    assert candidate_count == {"count": 0}
    assert staging == {"count": 0}
    assert old_ids == (
        "kg_ent_old_head",
        "kg_ent_old_tail",
        "kg_rel_old_requires",
    )


def test_document_kg_success_publishes_complete_snapshot_and_distinct_source_counts(
    isolated_document_kg_schema: _SchemaDatabase,
) -> None:
    """成功 final 同时提交完整 entity/evidence/job，重复同片 evidence 只算一个来源。"""
    database = isolated_document_kg_schema
    file_id = "imp_success_no_relation"
    chunks = [
        _document_chunk(
            file_id,
            "chunk_success_a",
            0,
            "控制台入口支持导出，控制台需要管理员权限。",
            section_path=["导出"],
            page_start=1,
        ),
        _document_chunk(
            file_id,
            "chunk_success_b",
            1,
            "控制台在移动端也可使用。",
            section_path=["移动端"],
            page_start=2,
        ),
    ]
    _create_document(database, file_id, chunks)
    job = database.create_document_kg_extraction_job(file_id, model="test-model")
    specs = [
        {
            "entities": [
                _simple_entity_spec(
                    "控制台",
                    "product_platform_module",
                    ["控制台入口", "控制台需要管理员权限"],
                )
            ],
            "relations": [],
        },
        {
            "entities": [
                _simple_entity_spec(
                    "控制台",
                    "product_platform_module",
                    ["控制台"],
                )
            ],
            "relations": [],
        },
    ]
    map_results = _complete_map_items(database, job["id"], specs)
    premerged = _save_resolution(database, job["id"], map_results, {"groups": []})
    reducing, reduced = _claim_and_reduce(database, job["id"])
    completed = database.complete_document_kg_extraction_job(
        job["id"],
        lease_token=reducing["lease_token"],
        extraction=reduced,
    )

    assert completed["phase"] == "completed"
    assert completed["processed_chunks"] == completed["total_chunks"] == 2
    assert completed["entity_count"] == 1
    assert completed["relation_count"] == 0
    assert completed["evidence_count"] == 3
    canonical_id = reduced["entities"][0]["id"]
    with database.connect() as conn:
        entity = conn.execute(
            """
            SELECT id, status, source_count
            FROM kg_entities
            WHERE id = %(id)s
            """,
            {"id": canonical_id},
        ).fetchone()
        evidence = conn.execute(
            """
            SELECT source_chunk_id, excerpt, char_start, char_end
            FROM kg_evidence
            WHERE entity_id = %(id)s
            ORDER BY source_chunk_id, char_start
            """,
            {"id": canonical_id},
        ).fetchall()
        counts = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM kg_relations) AS relation_count,
                (SELECT count(*) FROM kg_extraction_job_items
                 WHERE job_id = %(job_id)s AND map_result IS NOT NULL) AS staged_count,
                (SELECT phase FROM kg_extraction_jobs WHERE id = %(job_id)s) AS phase
            """,
            {"job_id": job["id"]},
        ).fetchone()
    assert entity == {
        "id": canonical_id,
        "status": "needs_review",
        "source_count": 2,
    }
    assert len(evidence) == 3
    assert len({row["source_chunk_id"] for row in evidence}) == 2
    assert counts == {"relation_count": 0, "staged_count": 0, "phase": "completed"}
    _assert_offsets(database, file_id)
    assert premerged["relations"] == []


def test_document_kg_resolution_merges_duplicate_relations_and_preserves_two_offsets(
    isolated_document_kg_schema: _SchemaDatabase,
) -> None:
    """resolution 后重复 Map relation 合并为一条 canonical relation，并保留两片证据 offsets。"""
    database = isolated_document_kg_schema
    file_id = "imp_duplicate_relation"
    chunks = [
        _document_chunk(
            file_id,
            "chunk_relation_a",
            0,
            "控制台需要管理员权限。",
            section_path=["导出"],
            page_start=1,
        ),
        _document_chunk(
            file_id,
            "chunk_relation_b",
            1,
            "管理后台要求管理权限。",
            section_path=["后台"],
            page_start=2,
        ),
    ]
    _create_document(database, file_id, chunks)
    job = database.create_document_kg_extraction_job(file_id, model="test-model")
    specs = [
        _entity_relation_spec(
            "控制台",
            "管理员权限",
            ["控制台需要管理员权限"],
        ),
        _entity_relation_spec(
            "管理后台",
            "管理权限",
            ["管理后台要求管理权限"],
        ),
    ]
    map_results = _complete_map_items(database, job["id"], specs)
    premerged = premerge_document_kg_map_results(map_results)
    by_name = {entity["name"]: entity["local_entity_id"] for entity in premerged["entities"]}
    resolution = {
        "groups": [
            [by_name["控制台"], by_name["管理后台"]],
            [by_name["管理员权限"], by_name["管理权限"]],
        ]
    }
    staged_premerged = _save_resolution(
        database,
        job["id"],
        map_results,
        resolution,
    )
    assert staged_premerged == premerged
    reducing, reduced = _claim_and_reduce(database, job["id"])
    assert len(reduced["entities"]) == 2
    assert len(reduced["relations"]) == 1
    relation = reduced["relations"][0]
    completed = database.complete_document_kg_extraction_job(
        job["id"],
        lease_token=reducing["lease_token"],
        extraction=reduced,
    )

    assert completed["entity_count"] == 2
    assert completed["relation_count"] == 1
    assert completed["evidence_count"] == 6
    with database.connect() as conn:
        relation_rows = conn.execute(
            """
            SELECT id, head_entity_id, tail_entity_id
            FROM kg_relations
            ORDER BY id
            """
        ).fetchall()
        relation_evidence = conn.execute(
            """
            SELECT source_chunk_id, excerpt, char_start, char_end
            FROM kg_evidence
            WHERE relation_id = %(relation_id)s
            ORDER BY source_chunk_id
            """,
            {"relation_id": relation["id"]},
        ).fetchall()
    assert relation_rows == [
        {
            "id": relation["id"],
            "head_entity_id": relation["head_entity_id"],
            "tail_entity_id": relation["tail_entity_id"],
        }
    ]
    assert [row["source_chunk_id"] for row in relation_evidence] == [
        "chunk_relation_a",
        "chunk_relation_b",
    ]
    assert len(relation_evidence) == 2
    _assert_offsets(database, file_id)


def test_document_kg_expired_lease_reclaims_unfinished_map_and_reduce_once(
    isolated_document_kg_schema: _SchemaDatabase,
) -> None:
    """模拟 worker 崩溃后重领未完成 item/reducing，最终只能发布一次 snapshot。"""
    database = isolated_document_kg_schema
    file_id = "imp_lease_recovery"
    chunks = [
        _document_chunk(
            file_id,
            "chunk_lease_a",
            0,
            "控制台需要管理员权限。",
            section_path=["导出"],
            page_start=1,
        ),
        _document_chunk(
            file_id,
            "chunk_lease_b",
            1,
            "故障时联系技术支持。",
            section_path=["故障"],
            page_start=2,
        ),
    ]
    _create_document(database, file_id, chunks)
    job = database.create_document_kg_extraction_job(file_id, model="test-model")
    specs = [
        {
            "entities": [
                _simple_entity_spec(
                    "控制台",
                    "product_platform_module",
                    ["控制台"],
                )
            ],
            "relations": [],
        },
        {
            "entities": [
                _simple_entity_spec(
                    "技术支持",
                    "role_permission_channel",
                    ["技术支持"],
                )
            ],
            "relations": [],
        },
    ]
    crashed_mapping = database.claim_kg_extraction_job(lease_seconds=30)
    assert crashed_mapping is not None
    first_item = database.load_document_kg_map_item(
        job["id"],
        lease_token=crashed_mapping["lease_token"],
    )
    assert first_item is not None
    _expire_lease(database, job["id"])
    reclaimed_mapping = database.claim_kg_extraction_job(lease_seconds=30)
    assert reclaimed_mapping is not None
    assert reclaimed_mapping["phase"] == "mapping"
    assert reclaimed_mapping["lease_token"] != crashed_mapping["lease_token"]
    resumed_item = database.load_document_kg_map_item(
        job["id"],
        lease_token=reclaimed_mapping["lease_token"],
    )
    assert resumed_item is not None
    assert resumed_item["id"] == first_item["id"]
    raw_first = _raw_map_extraction(resumed_item, specs[0])
    localized_first = localize_document_kg_map_result(
        raw_first,
        job_id=job["id"],
        chunk_id=resumed_item["chunk_id"],
        chunk_order=resumed_item["chunk_order"],
    )
    database.complete_document_kg_map_item(
        job["id"],
        resumed_item["id"],
        lease_token=reclaimed_mapping["lease_token"],
        map_result=localized_first,
    )
    localized_second = _complete_next_map_item(database, job["id"], specs[1])
    map_results = [localized_first, localized_second]
    _save_resolution(database, job["id"], map_results, {"groups": []})

    crashed_reducing = database.claim_kg_extraction_job(lease_seconds=30)
    assert crashed_reducing is not None
    assert crashed_reducing["phase"] == "reducing"
    database.load_document_kg_map_results(
        job["id"],
        lease_token=crashed_reducing["lease_token"],
    )
    _expire_lease(database, job["id"])
    reclaimed_reducing, reduced = _claim_and_reduce(database, job["id"])
    assert reclaimed_reducing["phase"] == "reducing"
    assert reclaimed_reducing["lease_token"] != crashed_reducing["lease_token"]
    completed = database.complete_document_kg_extraction_job(
        job["id"],
        lease_token=reclaimed_reducing["lease_token"],
        extraction=reduced,
    )
    assert completed["phase"] == "completed"
    with pytest.raises(ValueError, match="phase|lease"):
        database.complete_document_kg_extraction_job(
            job["id"],
            lease_token=reclaimed_reducing["lease_token"],
            extraction=reduced,
        )
    with database.connect() as conn:
        state = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM kg_extraction_jobs WHERE source_id = %(file_id)s)
                    AS job_count,
                (SELECT attempt_count FROM kg_extraction_jobs WHERE id = %(job_id)s)
                    AS attempt_count,
                (SELECT count(*) FROM kg_evidence WHERE source_id = %(file_id)s)
                    AS evidence_count,
                (SELECT count(*) FROM kg_extraction_job_items
                 WHERE job_id = %(job_id)s AND map_result IS NOT NULL)
                    AS staged_count
            """,
            {"file_id": file_id, "job_id": job["id"]},
        ).fetchone()
    assert state == {
        "job_count": 1,
        "attempt_count": 6,
        "evidence_count": 2,
        "staged_count": 0,
    }
    assert database.claim_kg_extraction_job(lease_seconds=30) is None
