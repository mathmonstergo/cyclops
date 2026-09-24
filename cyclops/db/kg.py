from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from cyclops.document_kg import (
    build_document_kg_manifest,
    premerge_document_kg_map_results,
)
from cyclops.db.models import KgExpandedCandidate, KgFactHit
from cyclops.kg import (
    build_faq_kg_source_text,
    build_kg_entity_knowledge_chunk_row,
    build_kg_relation_knowledge_chunk_row,
    kg_source_fingerprint,
)


_KG_JOB_PUBLIC_FIELDS = (
    "id",
    "source_type",
    "source_id",
    "phase",
    "processed_chunks",
    "total_chunks",
    "entity_count",
    "relation_count",
    "evidence_count",
    "model",
    "error",
    "created_at",
    "updated_at",
)
_KG_JOB_PUBLIC_RETURNING_SQL = ", ".join(_KG_JOB_PUBLIC_FIELDS)
_KG_JOB_PUBLIC_SELECT_SQL = ", ".join(
    f"job.{field}" for field in _KG_JOB_PUBLIC_FIELDS
)


class KgReviewConflictError(ValueError):
    """审核页面 revision 已过期时抛出，调用方必须刷新后重新人工确认。"""


def _kg_live_evidence_predicate_sql() -> str:
    """返回唯一实时证据门禁，FAQ 与文档 locator 必须精确匹配当前来源。"""
    return """
        (valid_ev.source_type = 'faq' AND faq.status = 'usable')
        OR (
            valid_ev.source_type = 'document'
            AND imp.id IS NOT NULL
            AND chunk.id IS NOT NULL
            AND imp.is_disabled = false
            AND chunk.is_disabled = false
        )
    """


def _kg_valid_evidence_select_sql(
    owner_condition: str,
    *,
    columns: str,
) -> str:
    """生成实时证据查询，FAQ/文档门禁只在这里定义一次。"""
    return f"""
        SELECT {columns}
        FROM kg_evidence valid_ev
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
        WHERE {owner_condition}
          AND ({_kg_live_evidence_predicate_sql()})
    """


def _kg_valid_evidence_exists_sql(owner_condition: str) -> str:
    """生成实时证据 EXISTS，关键约束是复用投影证据的同一来源门禁。"""
    select_sql = _kg_valid_evidence_select_sql(owner_condition, columns="1")
    return f"EXISTS ({select_sql})"


def _kg_valid_evidence_count_sql(owner_condition: str) -> str:
    """生成实时证据计数，关键约束是按 evidence ID 去重并复用唯一来源门禁。"""
    return _kg_valid_evidence_select_sql(
        owner_condition,
        columns="count(DISTINCT valid_ev.id)::integer",
    )


class KnowledgeGraphMixin:
    """知识图谱候选审核、投影和局部子图查询。"""

    def create_faq_kg_extraction_job(
        self,
        faq_id: str,
        *,
        model: str,
    ) -> dict[str, Any]:
        """锁定 usable FAQ 并创建 total_chunks=1 的唯一 queued job。"""
        normalized_faq_id = self._required_kg_job_text(faq_id, "faq_id")
        normalized_model = self._required_kg_job_text(model, "model")
        with self.connect() as conn:
            faq = conn.execute(
                self._lock_kg_faq_source_sql(),
                {"source_id": normalized_faq_id},
            ).fetchone()
            if faq is None:
                raise KeyError(f"FAQ not found: {normalized_faq_id}")
            if faq.get("status") != "usable":
                raise ValueError("FAQ must be usable before KG extraction")
            self._ensure_no_active_kg_job_in_conn(
                conn,
                source_type="faq",
                source_id=normalized_faq_id,
            )
            source_text = build_faq_kg_source_text(faq)
            if not source_text.strip():
                raise ValueError("FAQ KG source_text is required")
            row = conn.execute(
                self._insert_kg_extraction_job_sql(),
                {
                    "id": self._new_kg_job_id(),
                    "source_type": "faq",
                    "source_id": normalized_faq_id,
                    "total_chunks": 1,
                    "source_fingerprint": kg_source_fingerprint(
                        source_text=source_text,
                        source={"source_type": "faq", "source_id": normalized_faq_id},
                    ),
                    "model": normalized_model,
                },
            ).fetchone()
        return self._validate_kg_job_public_row(row)

    def create_document_kg_extraction_job(
        self,
        file_id: str,
        *,
        model: str,
    ) -> dict[str, Any]:
        """锁定整篇文档、生成 manifest 并原子写父任务及全部 item。"""
        normalized_file_id = self._required_kg_job_text(file_id, "file_id")
        normalized_model = self._required_kg_job_text(model, "model")
        source_params = {"source_id": normalized_file_id}
        with self.connect() as conn:
            import_file = conn.execute(
                self._lock_kg_document_file_source_sql(),
                source_params,
            ).fetchone()
            if import_file is None:
                raise KeyError(f"Import file not found: {normalized_file_id}")
            if import_file.get("is_disabled"):
                raise ValueError("disabled import file cannot be used for KG extraction")
            if import_file.get("status") != "needs_review":
                raise ValueError("import file must be in parsed review state for KG extraction")
            chunks = conn.execute(
                self._lock_kg_document_chunks_source_sql(),
                source_params,
            ).fetchall()
            self._ensure_no_active_kg_job_in_conn(
                conn,
                source_type="document",
                source_id=normalized_file_id,
            )
            manifest = build_document_kg_manifest(import_file, chunks)
            job_id = self._new_kg_job_id()
            row = conn.execute(
                self._insert_kg_extraction_job_sql(),
                {
                    "id": job_id,
                    "source_type": "document",
                    "source_id": normalized_file_id,
                    "total_chunks": len(manifest["items"]),
                    "source_fingerprint": manifest["fingerprint"],
                    "model": normalized_model,
                },
            ).fetchone()
            for item in manifest["items"]:
                inserted = conn.execute(
                    self._insert_document_kg_job_item_sql(),
                    {
                        "id": self._new_kg_job_item_id(),
                        "job_id": job_id,
                        "chunk_id": item["chunk_id"],
                        "chunk_order": item["chunk_order"],
                        "source_fingerprint": item["source_fingerprint"],
                        "section_path": json.dumps(
                            item["section_path"],
                            ensure_ascii=False,
                        ),
                        "page_start": item["page_start"],
                        "page_end": item["page_end"],
                    },
                ).fetchone()
                if inserted is None:
                    raise RuntimeError("document KG job item insert returned no row")
        return self._validate_kg_job_public_row(row)

    def get_kg_extraction_job(self, job_id: str) -> dict[str, Any] | None:
        """按 job ID 返回唯一公开 DTO，不读取 staging 或 lease 字段。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        with self.connect() as conn:
            row = conn.execute(
                self._get_kg_extraction_job_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
        if row is None:
            return None
        return self._validate_kg_job_public_row(row)

    def get_latest_kg_extraction_job(
        self,
        *,
        source_type: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        """按 source 与 created_at/id 倒序读取最近 job 的公开 DTO。"""
        if source_type not in {"faq", "document"}:
            raise ValueError("source_type must be faq or document")
        normalized_source_id = self._required_kg_job_text(source_id, "source_id")
        with self.connect() as conn:
            row = conn.execute(
                self._latest_kg_extraction_job_sql(),
                {"source_type": source_type, "source_id": normalized_source_id},
            ).fetchone()
        if row is None:
            return None
        return self._validate_kg_job_public_row(row)

    def claim_kg_extraction_job(self, *, lease_seconds: int) -> dict[str, Any] | None:
        """原子领取一个可推进 job；queued 在领取事务内进入 mapping。"""
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise TypeError("lease_seconds must be an integer")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        params = {
            "lease_token": uuid.uuid4().hex,
            "lease_seconds": lease_seconds,
        }
        with self.connect() as conn:
            return conn.execute(self._claim_kg_extraction_job_sql(), params).fetchone()

    def load_document_kg_map_item(
        self,
        job_id: str,
        *,
        lease_token: str,
    ) -> dict[str, Any] | None:
        """取得首个未 mapped item 和当前来源文本，并校验 frozen locator。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        normalized_token = self._required_kg_job_text(lease_token, "lease_token")
        with self.connect() as conn:
            preview_job = conn.execute(
                self._get_kg_extraction_job_for_fence_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                preview_job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"mapping"},
                source_type="document",
            )
            preview_item = conn.execute(
                self._get_document_kg_map_item_for_fence_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            if preview_item is None:
                return None
            source_id = preview_job["source_id"]
            source_params = {
                "source_id": source_id,
                "source_chunk_id": preview_item["chunk_id"],
            }
            import_file = conn.execute(
                self._lock_kg_document_file_source_sql(),
                source_params,
            ).fetchone()
            chunk = conn.execute(
                self._lock_kg_document_chunk_source_sql(),
                source_params,
            ).fetchone()
            locked_job = conn.execute(
                self._lock_kg_extraction_job_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                locked_job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"mapping"},
                source_type="document",
            )
            locked_item = conn.execute(
                self._lock_document_kg_job_item_sql(),
                {"job_id": normalized_job_id, "item_id": preview_item["id"]},
            ).fetchone()
            if locked_item is None or locked_item.get("phase") not in {"queued", "mapping"}:
                raise ValueError("document KG Map item changed before load")
            self._validate_document_kg_item_source(
                import_file=import_file,
                chunk=chunk,
                job=locked_job,
                item=locked_item,
            )
            item = conn.execute(
                self._mark_document_kg_job_item_mapping_sql(),
                {"job_id": normalized_job_id, "item_id": locked_item["id"]},
            ).fetchone()
            if item is None:
                raise ValueError("document KG Map item changed before mapping")
        return {
            "id": item["id"],
            "job_id": normalized_job_id,
            "chunk_id": item["chunk_id"],
            "chunk_order": item["chunk_order"],
            "source_fingerprint": item["source_fingerprint"],
            "source_text": chunk["source_text"],
            "source": {
                "source_type": "document",
                "source_id": import_file["id"],
                "source_chunk_id": chunk["id"],
                "source_title": import_file["original_name"],
                "section_path": list(chunk["section_path"]),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
            },
        }

    def complete_document_kg_map_item(
        self,
        job_id: str,
        item_id: str,
        *,
        lease_token: str,
        map_result: dict[str, Any],
    ) -> dict[str, Any]:
        """复核 job/item/source 后写隐藏 staging、推进计数并释放 lease。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        normalized_item_id = self._required_kg_job_text(item_id, "item_id")
        normalized_token = self._required_kg_job_text(lease_token, "lease_token")
        if not isinstance(map_result, dict):
            raise TypeError("map_result must be a JSON object")
        premerge_document_kg_map_results([map_result])
        with self.connect() as conn:
            preview_job = conn.execute(
                self._get_kg_extraction_job_for_fence_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                preview_job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"mapping"},
                source_type="document",
            )
            preview_item = conn.execute(
                self._get_document_kg_job_item_by_id_for_fence_sql(),
                {"job_id": normalized_job_id, "item_id": normalized_item_id},
            ).fetchone()
            if preview_item is None:
                raise KeyError(f"document KG Map item not found: {normalized_item_id}")
            if (
                map_result.get("chunk_id") != preview_item.get("chunk_id")
                or map_result.get("chunk_order") != preview_item.get("chunk_order")
            ):
                raise ValueError("Map result does not match manifest item")
            source_params = {
                "source_id": preview_job["source_id"],
                "source_chunk_id": preview_item["chunk_id"],
            }
            import_file = conn.execute(
                self._lock_kg_document_file_source_sql(),
                source_params,
            ).fetchone()
            chunk = conn.execute(
                self._lock_kg_document_chunk_source_sql(),
                source_params,
            ).fetchone()
            locked_job = conn.execute(
                self._lock_kg_extraction_job_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                locked_job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"mapping"},
                source_type="document",
            )
            locked_item = conn.execute(
                self._lock_document_kg_job_item_sql(),
                {"job_id": normalized_job_id, "item_id": normalized_item_id},
            ).fetchone()
            if locked_item is None or locked_item.get("phase") != "mapping":
                raise ValueError("document KG Map item must be mapping before completion")
            self._validate_document_kg_item_source(
                import_file=import_file,
                chunk=chunk,
                job=locked_job,
                item=locked_item,
            )
            staged = conn.execute(
                self._complete_document_kg_map_item_sql(),
                {
                    "job_id": normalized_job_id,
                    "item_id": normalized_item_id,
                    "map_result": json.dumps(map_result, ensure_ascii=False),
                },
            ).fetchone()
            if staged is None:
                raise ValueError("document KG Map item changed before completion")
            row = conn.execute(
                self._advance_document_kg_map_progress_sql(),
                {
                    "job_id": normalized_job_id,
                    "lease_token": normalized_token,
                },
            ).fetchone()
            if row is None:
                raise ValueError("document KG job lease expired before Map completion")
        return self._validate_kg_job_public_row(row)

    def load_document_kg_map_results(
        self,
        job_id: str,
        *,
        lease_token: str,
    ) -> list[dict[str, Any]]:
        """只供 resolving/reducing worker 读取全部 mapped JSON，公开 API 不调用。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        normalized_token = self._required_kg_job_text(lease_token, "lease_token")
        with self.connect() as conn:
            job = conn.execute(
                self._get_kg_extraction_job_for_fence_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"resolving", "reducing"},
                source_type="document",
            )
            rows = conn.execute(
                self._load_document_kg_map_results_sql(),
                {"job_id": normalized_job_id},
            ).fetchall()
        if len(rows) != job["total_chunks"]:
            raise ValueError("document KG job does not have every mapped item")
        results: list[dict[str, Any]] = []
        for row in rows:
            if row.get("phase") != "mapped" or not isinstance(row.get("map_result"), dict):
                raise ValueError("document KG job item is not fully mapped")
            results.append(dict(row["map_result"]))
        return results

    def save_document_kg_resolution(
        self,
        job_id: str,
        *,
        lease_token: str,
        resolution_result: dict[str, Any],
    ) -> dict[str, Any]:
        """持久化 validated resolution 并原子进入 reducing，供崩溃后恢复。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        normalized_token = self._required_kg_job_text(lease_token, "lease_token")
        if not isinstance(resolution_result, dict):
            raise TypeError("resolution_result must be a JSON object")
        with self.connect() as conn:
            row = conn.execute(
                self._save_document_kg_resolution_sql(),
                {
                    "job_id": normalized_job_id,
                    "lease_token": normalized_token,
                    "resolution_result": json.dumps(
                        resolution_result,
                        ensure_ascii=False,
                    ),
                },
            ).fetchone()
        if row is None:
            raise ValueError("document KG job lease expired before resolution save")
        return self._validate_kg_job_public_row(row)

    def fail_kg_extraction_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        error: str,
    ) -> dict[str, Any]:
        """仅当前 lease 可把 active job 置 failed，并清除 Map/resolution staging。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        normalized_token = self._required_kg_job_text(lease_token, "lease_token")
        bounded_error = str(error or "")[:1000]
        with self.connect() as conn:
            job = conn.execute(
                self._lock_kg_extraction_job_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"mapping", "resolving", "reducing"},
            )
            conn.execute(
                self._clear_failed_kg_job_items_sql(),
                {
                    "job_id": normalized_job_id,
                    "lease_token": normalized_token,
                    "error": bounded_error,
                },
            )
            row = conn.execute(
                self._fail_kg_extraction_job_sql(),
                {
                    "job_id": normalized_job_id,
                    "lease_token": normalized_token,
                    "error": bounded_error,
                },
            ).fetchone()
            if row is None:
                raise ValueError("KG extraction job lease expired before failure")
        return self._validate_kg_job_public_row(row)

    def _ensure_no_active_kg_job_in_conn(
        self,
        conn: Any,
        *,
        source_type: str,
        source_id: str,
    ) -> None:
        """在来源锁内拒绝同源 active generation，避免复用或覆盖旧 job。"""
        active = conn.execute(
            self._find_active_kg_extraction_job_sql(),
            {"source_type": source_type, "source_id": source_id},
        ).fetchone()
        if active is not None:
            raise ValueError(f"active KG extraction job already exists: {active['id']}")

    @staticmethod
    def _validate_kg_job_public_row(row: dict[str, Any] | None) -> dict[str, Any]:
        """校验公开 job DTO 不含 fingerprint、staging、lease 或 attempt 字段。"""
        if row is None:
            raise RuntimeError("KG extraction job write returned no row")
        if set(row) != set(_KG_JOB_PUBLIC_FIELDS):
            raise ValueError("KG extraction job public row has invalid fields")
        return dict(row)

    @staticmethod
    def _required_kg_job_text(value: Any, field: str) -> str:
        """读取 KG job 必填文本，拒绝隐式数字或空白 locator。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        return value.strip()

    @staticmethod
    def _validate_kg_job_lease_row(
        row: dict[str, Any] | None,
        *,
        job_id: str,
        lease_token: str,
        phases: set[str],
        source_type: str | None = None,
    ) -> None:
        """校验内部 job 的 active phase 与当前未过期 lease，所有写入必须二次复核。"""
        if row is None:
            raise KeyError(f"KG extraction job not found: {job_id}")
        if row.get("phase") not in phases:
            raise ValueError(f"KG extraction job has invalid phase for operation: {job_id}")
        if source_type is not None and row.get("source_type") != source_type:
            raise ValueError(f"KG extraction job source_type must be {source_type}")
        if row.get("lease_token") != lease_token or not row.get("lease_is_current"):
            raise ValueError("KG extraction job lease expired or changed")

    @staticmethod
    def _validate_document_kg_item_source(
        *,
        import_file: dict[str, Any] | None,
        chunk: dict[str, Any] | None,
        job: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        """复核 frozen item locator 与当前文件/切片，模型调用前后使用同一 fingerprint。"""
        if import_file is None or import_file.get("id") != job.get("source_id"):
            raise ValueError("document KG source file changed or is missing")
        if import_file.get("is_disabled"):
            raise ValueError("document KG source file is disabled")
        if import_file.get("status") != "needs_review":
            raise ValueError("document KG source file left parsed review state")
        if chunk is None or chunk.get("id") != item.get("chunk_id"):
            raise ValueError("document KG source chunk changed or is missing")
        if chunk.get("file_id") != import_file.get("id") or chunk.get("is_disabled"):
            raise ValueError("document KG source chunk is unavailable")
        source_text = chunk.get("source_text")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError("document KG source_text is required")
        current_fingerprint = kg_source_fingerprint(
            source_text=source_text,
            source={
                "source_type": "document",
                "source_id": import_file["id"],
                "source_chunk_id": chunk["id"],
                "source_title": import_file["original_name"],
                "section_path": list(chunk["section_path"]),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
            },
        )
        if (
            item.get("source_fingerprint") != current_fingerprint
            or list(item.get("section_path") or []) != list(chunk["section_path"])
            or item.get("page_start") != chunk.get("page_start")
            or item.get("page_end") != chunk.get("page_end")
        ):
            raise ValueError("document KG Map item manifest changed")

    def complete_faq_kg_extraction_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        extraction: dict[str, Any],
    ) -> dict[str, Any]:
        """复核 FAQ fingerprint 后原子替换 FAQ snapshot 并完成父任务。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        normalized_token = self._required_kg_job_text(lease_token, "lease_token")
        self._require_kg_extraction_object(extraction)
        with self.connect() as conn:
            preview = conn.execute(
                self._get_kg_extraction_job_for_fence_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                preview,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"mapping"},
                source_type="faq",
            )
            faq = conn.execute(
                self._lock_kg_faq_source_sql(),
                {"source_id": preview["source_id"]},
            ).fetchone()
            locked_job = conn.execute(
                self._lock_kg_extraction_job_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                locked_job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"mapping"},
                source_type="faq",
            )
            if faq is None or faq.get("id") != locked_job.get("source_id"):
                raise ValueError("FAQ KG source changed or is missing")
            if faq.get("status") != "usable":
                raise ValueError("FAQ KG source must remain usable")
            source_text = build_faq_kg_source_text(faq)
            source = {
                "source_type": "faq",
                "source_id": faq["id"],
                "source_chunk_id": None,
                "source_title": faq.get("question"),
                "section_path": [],
                "page_start": None,
                "page_end": None,
            }
            source_fingerprint = kg_source_fingerprint(
                source_text=source_text,
                source=source,
            )
            if locked_job.get("source_fingerprint") != source_fingerprint:
                raise ValueError("FAQ KG source fingerprint changed")
            self._validate_kg_extraction_evidence(
                extraction,
                source_text_by_chunk={None: source_text},
                source_by_chunk={None: source},
            )
            counts = self._replace_kg_source_snapshot_in_conn(
                conn,
                source_type="faq",
                source_ids=[faq["id"]],
                source_chunk_ids=None,
                extraction=extraction,
            )
            row = conn.execute(
                self._complete_faq_kg_extraction_job_sql(),
                {
                    "job_id": normalized_job_id,
                    "lease_token": normalized_token,
                    "source_fingerprint": source_fingerprint,
                    **counts,
                },
            ).fetchone()
            if row is None:
                raise ValueError("FAQ KG job lease expired before completion")
        return self._validate_kg_job_public_row(row)

    def complete_document_kg_extraction_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        extraction: dict[str, Any],
    ) -> dict[str, Any]:
        """复核完整 manifest/evidence 后原子替换整篇文档 snapshot 并完成父任务。"""
        normalized_job_id = self._required_kg_job_text(job_id, "job_id")
        normalized_token = self._required_kg_job_text(lease_token, "lease_token")
        self._require_kg_extraction_object(extraction)
        with self.connect() as conn:
            preview = conn.execute(
                self._get_kg_extraction_job_for_fence_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                preview,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"reducing"},
                source_type="document",
            )
            source_params = {"source_id": preview["source_id"]}
            import_file = conn.execute(
                self._lock_kg_document_file_source_sql(),
                source_params,
            ).fetchone()
            chunks = conn.execute(
                self._lock_kg_document_chunks_source_sql(),
                source_params,
            ).fetchall()
            locked_job = conn.execute(
                self._lock_kg_extraction_job_sql(),
                {"job_id": normalized_job_id},
            ).fetchone()
            self._validate_kg_job_lease_row(
                locked_job,
                job_id=normalized_job_id,
                lease_token=normalized_token,
                phases={"reducing"},
                source_type="document",
            )
            items = conn.execute(
                self._lock_document_kg_job_items_sql(),
                {"job_id": normalized_job_id},
            ).fetchall()
            manifest = self._validate_document_kg_completion_manifest(
                import_file=import_file,
                chunks=chunks,
                job=locked_job,
                items=items,
            )
            chunk_by_id = {chunk["id"]: chunk for chunk in chunks}
            source_text_by_chunk: dict[str | None, str] = {}
            source_by_chunk: dict[str | None, dict[str, Any]] = {}
            for item in manifest["items"]:
                chunk = chunk_by_id[item["chunk_id"]]
                source_text_by_chunk[chunk["id"]] = chunk["source_text"]
                source_by_chunk[chunk["id"]] = {
                    "source_type": "document",
                    "source_id": import_file["id"],
                    "source_chunk_id": chunk["id"],
                    "source_title": import_file["original_name"],
                    "section_path": list(chunk["section_path"]),
                    "page_start": chunk.get("page_start"),
                    "page_end": chunk.get("page_end"),
                }
            self._validate_kg_extraction_evidence(
                extraction,
                source_text_by_chunk=source_text_by_chunk,
                source_by_chunk=source_by_chunk,
            )
            counts = self._replace_kg_source_snapshot_in_conn(
                conn,
                source_type="document",
                source_ids=[import_file["id"]],
                source_chunk_ids=None,
                extraction=extraction,
            )
            row = conn.execute(
                self._complete_document_kg_extraction_job_sql(),
                {
                    "job_id": normalized_job_id,
                    "lease_token": normalized_token,
                    "source_fingerprint": manifest["fingerprint"],
                    **counts,
                },
            ).fetchone()
            if row is None:
                raise ValueError("document KG job lease expired before completion")
            conn.execute(
                self._clear_completed_document_kg_staging_sql(),
                {"job_id": normalized_job_id},
            )
        return self._validate_kg_job_public_row(row)

    @staticmethod
    def _require_kg_extraction_object(extraction: dict[str, Any]) -> None:
        """校验 final extraction 顶层唯一 shape，空 snapshot 仍是合法完整结果。"""
        if not isinstance(extraction, dict):
            raise TypeError("extraction must be a JSON object")
        if set(extraction) != {"entities", "relations"}:
            raise ValueError("extraction must contain only entities and relations")
        if not isinstance(extraction["entities"], list) or not isinstance(
            extraction["relations"], list
        ):
            raise TypeError("extraction entities and relations must be arrays")

    @staticmethod
    def _validate_document_kg_completion_manifest(
        *,
        import_file: dict[str, Any] | None,
        chunks: list[dict[str, Any]],
        job: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """复核 file/chunks/job/items 的完整 immutable manifest，任何差异都阻止 owner 写入。"""
        if import_file is None or import_file.get("id") != job.get("source_id"):
            raise ValueError("document KG source file changed or is missing")
        if import_file.get("is_disabled"):
            raise ValueError("document KG source file is disabled")
        if import_file.get("status") != "needs_review":
            raise ValueError("document KG source file left parsed review state")
        manifest = build_document_kg_manifest(import_file, chunks)
        if job.get("source_fingerprint") != manifest["fingerprint"]:
            raise ValueError("document KG manifest fingerprint changed")
        if (
            job.get("processed_chunks") != job.get("total_chunks")
            or job.get("total_chunks") != len(manifest["items"])
            or not isinstance(job.get("resolution_result"), dict)
        ):
            raise ValueError("document KG job is not ready for final snapshot")
        ordered_items = sorted(
            items,
            key=lambda item: (item.get("chunk_order", -1), item.get("id", "")),
        )
        if len(ordered_items) != len(manifest["items"]):
            raise ValueError("document KG manifest item set changed")
        for stored, expected in zip(ordered_items, manifest["items"], strict=True):
            if (
                stored.get("job_id") != job.get("id")
                or stored.get("chunk_id") != expected["chunk_id"]
                or stored.get("chunk_order") != expected["chunk_order"]
                or stored.get("source_fingerprint") != expected["source_fingerprint"]
                or list(stored.get("section_path") or []) != expected["section_path"]
                or stored.get("page_start") != expected["page_start"]
                or stored.get("page_end") != expected["page_end"]
                or stored.get("phase") != "mapped"
                or not isinstance(stored.get("map_result"), dict)
            ):
                raise ValueError("document KG manifest item changed")
        return manifest

    @staticmethod
    def _validate_kg_extraction_evidence(
        extraction: dict[str, Any],
        *,
        source_text_by_chunk: dict[str | None, str],
        source_by_chunk: dict[str | None, dict[str, Any]],
    ) -> None:
        """验证 final evidence 的精确 locator 与 code-point substring，不接受默认或模糊 offset。"""
        expected_fields = {
            "source_type",
            "source_id",
            "source_chunk_id",
            "source_title",
            "section_path",
            "page_start",
            "page_end",
            "excerpt",
            "char_start",
            "char_end",
        }
        candidates = [*extraction["entities"], *extraction["relations"]]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise TypeError("KG extraction candidates must be objects")
            evidence_items = candidate.get("evidence")
            if not isinstance(evidence_items, list) or not evidence_items:
                raise ValueError("KG extraction candidate requires evidence")
            for evidence in evidence_items:
                if not isinstance(evidence, dict) or set(evidence) != expected_fields:
                    raise ValueError("KG extraction evidence has invalid fields")
                source_chunk_id = evidence["source_chunk_id"]
                if source_chunk_id not in source_text_by_chunk:
                    raise ValueError("KG extraction evidence references another source chunk")
                expected_source = source_by_chunk[source_chunk_id]
                actual_source = {
                    field: evidence[field]
                    for field in (
                        "source_type",
                        "source_id",
                        "source_chunk_id",
                        "source_title",
                        "section_path",
                        "page_start",
                        "page_end",
                    )
                }
                if actual_source != expected_source:
                    raise ValueError("KG extraction evidence source locator changed")
                excerpt = evidence["excerpt"]
                char_start = evidence["char_start"]
                char_end = evidence["char_end"]
                source_text = source_text_by_chunk[source_chunk_id]
                if (
                    not isinstance(excerpt, str)
                    or not excerpt
                    or isinstance(char_start, bool)
                    or not isinstance(char_start, int)
                    or isinstance(char_end, bool)
                    or not isinstance(char_end, int)
                    or char_start < 0
                    or char_end <= char_start
                    or char_end > len(source_text)
                    or source_text[char_start:char_end] != excerpt
                ):
                    raise ValueError("KG extraction evidence offsets do not match source text")

    def _replace_kg_source_snapshot_in_conn(
        self,
        conn: Any,
        *,
        source_type: str,
        source_ids: list[str],
        source_chunk_ids: list[str] | None,
        extraction: dict[str, Any],
    ) -> dict[str, int]:
        """按 Unit 0 entity→fresh relation 两阶段锁序替换已验证的精确来源范围。"""
        if source_type not in {"faq", "document"} or not source_ids:
            raise ValueError("KG snapshot source scope is invalid")
        entities = sorted(extraction.get("entities", []), key=lambda item: item["id"])
        relations = sorted(extraction.get("relations", []), key=lambda item: item["id"])
        entity_by_id = {entity["id"]: entity for entity in entities}
        relation_by_id = {relation["id"]: relation for relation in relations}
        if len(entity_by_id) != len(entities) or len(relation_by_id) != len(relations):
            raise ValueError("KG extraction candidates must have unique ids")
        for relation in relations:
            if (
                relation["head_entity_id"] not in entity_by_id
                or relation["tail_entity_id"] not in entity_by_id
            ):
                raise ValueError("KG relation endpoints must be present in extraction entities")
        params = self._kg_source_invalidation_params(
            source_type=source_type,
            source_ids=source_ids,
            source_chunk_ids=source_chunk_ids,
        )
        targets = self._collect_kg_source_lock_targets_in_conn(
            conn,
            params,
            candidate_entity_ids=list(entity_by_id),
            candidate_relation_ids=list(relation_by_id),
        )
        self._lock_or_upsert_kg_entities_in_conn(
            conn,
            targets["entity_lock_ids"],
            entity_by_id,
        )
        relation_lock_ids = self._refresh_kg_relation_lock_ids_in_conn(
            conn,
            initial_relation_ids=targets["relation_lock_ids"],
            incident_entity_ids=targets["incident_entity_ids"],
            candidate_relation_ids=list(relation_by_id),
        )
        non_candidate_relation_ids = self._lock_or_upsert_kg_relations_in_conn(
            conn,
            relation_lock_ids,
            relation_by_id,
        )
        conn.execute(self._delete_kg_source_evidence_sql(), params)

        evidence_payloads: list[dict[str, Any]] = []
        for entity in entities:
            for evidence in entity.get("evidence", []):
                evidence_payloads.append(
                    self._kg_evidence_params(evidence, entity_id=entity["id"])
                )
        for relation in relations:
            for evidence in relation.get("evidence", []):
                evidence_payloads.append(
                    self._kg_evidence_params(evidence, relation_id=relation["id"])
                )

        for evidence_payload in sorted(evidence_payloads, key=lambda item: item["id"]):
            conn.execute(self._insert_kg_evidence_sql(), evidence_payload)
        self._reconcile_kg_owners_after_evidence_change_in_conn(
            conn,
            entity_ids=sorted(set(targets["source_entity_ids"]).union(entity_by_id)),
            relation_ids=relation_lock_ids,
            entity_revision_ids=sorted(
                set(targets["source_entity_ids"]).difference(entity_by_id)
            ),
            relation_revision_ids=non_candidate_relation_ids,
        )
        return {
            "entity_count": len(entities),
            "relation_count": len(relations),
            "evidence_count": len(evidence_payloads),
        }

    def _lock_or_upsert_kg_entities_in_conn(
        self,
        conn: Any,
        entity_ids: list[str],
        candidate_by_id: dict[str, dict[str, Any]],
    ) -> None:
        """按实体 ID 逐个取得真实行/唯一键；候选不存在时也必须在关系阶段前仲裁。"""
        for entity_id in sorted(set(entity_ids)):
            candidate = candidate_by_id.get(entity_id)
            if candidate is not None:
                conn.execute(self._upsert_kg_entity_sql(), self._kg_entity_params(candidate))
                continue
            conn.execute(
                self._lock_kg_entity_review_sql(),
                {"id": entity_id},
            ).fetchone()

    def _lock_or_upsert_kg_relations_in_conn(
        self,
        conn: Any,
        relation_ids: list[str],
        candidate_by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        """按关系 ID 逐个锁定或 upsert；返回需要回退且仍存在的非候选关系。"""
        invalidated_ids: list[str] = []
        for relation_id in sorted(set(relation_ids)):
            candidate = candidate_by_id.get(relation_id)
            if candidate is not None:
                conn.execute(
                    self._upsert_kg_relation_sql(),
                    self._kg_relation_params(candidate),
                )
                continue
            locked = conn.execute(
                self._lock_kg_relation_review_sql(),
                {"id": relation_id},
            ).fetchone()
            if locked is not None:
                invalidated_ids.append(relation_id)
        return invalidated_ids

    def list_kg_entities(
        self,
        *,
        status: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """列出 KG 实体候选，关键约束是返回证据摘要供人工审核。"""
        clauses = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            clauses.append("ent.status = %(status)s")
            params["status"] = status
        if entity_type:
            clauses.append("ent.entity_type = %(entity_type)s")
            params["entity_type"] = entity_type
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(self._list_kg_entities_sql(where=where), params).fetchall()
            total = conn.execute(self._count_kg_entities_sql(where=where), params).fetchone()["total"]
        return {"items": rows, "total": total}

    def list_kg_relations(
        self,
        *,
        status: str | None = None,
        relation_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """列出 KG 关系候选，关键约束是返回头尾实体和证据摘要供人工审核。"""
        clauses = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            clauses.append("rel.status = %(status)s")
            params["status"] = status
        if relation_type:
            clauses.append("rel.relation_type = %(relation_type)s")
            params["relation_type"] = relation_type
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(self._list_kg_relations_sql(where=where), params).fetchall()
            total = conn.execute(self._count_kg_relations_sql(where=where), params).fetchone()["total"]
        return {"items": rows, "total": total}

    def confirm_kg_entity(
        self,
        entity_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """确认 KG 实体并投影；关键约束是调用方显式提交所审核的 revision。"""
        self._validate_expected_kg_review_revision(expected_revision)
        with self.connect() as conn:
            locked = conn.execute(
                self._lock_kg_entity_review_sql(), {"id": entity_id}
            ).fetchone()
            if locked is None:
                raise KeyError(f"KG entity not found: {entity_id}")
            self._require_current_kg_review_revision(
                locked,
                expected_revision=expected_revision,
                candidate_label="KG entity",
            )
            context = conn.execute(
                self._kg_entity_review_context_sql(), {"id": entity_id}
            ).fetchone()
            self._validate_kg_entity_review_context(context, entity_id)
            entity = conn.execute(self._confirm_kg_entity_sql(), {"id": entity_id}).fetchone()
            if entity is None:
                raise KeyError(f"KG entity not found: {entity_id}")
            evidence = conn.execute(
                self._list_valid_kg_entity_evidence_sql(), {"entity_id": entity_id}
            ).fetchall()
            chunk = build_kg_entity_knowledge_chunk_row(entity, [dict(item) for item in evidence])
            conn.execute(self._insert_knowledge_chunk_sql(), self._knowledge_chunk_payload(chunk))
            return entity

    def confirm_kg_relation(
        self,
        relation_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """确认 KG 关系并投影；按固定锁序复核端点和调用方审核的 revision。"""
        self._validate_expected_kg_review_revision(expected_revision)
        with self.connect() as conn:
            locator = conn.execute(
                self._get_kg_relation_endpoint_locator_sql(), {"id": relation_id}
            ).fetchone()
            if locator is None:
                raise KeyError(f"KG relation not found: {relation_id}")
            entity_ids = sorted(
                {
                    locator["head_entity_id"],
                    locator["tail_entity_id"],
                }
            )
            locked_entities = conn.execute(
                self._lock_kg_relation_entities_sql(),
                {"entity_ids": entity_ids},
            ).fetchall()
            if [row["id"] for row in locked_entities] != entity_ids:
                raise ValueError("KG relation endpoint entities changed before confirmation")
            locked_relation = conn.execute(
                self._lock_kg_relation_review_sql(), {"id": relation_id}
            ).fetchone()
            if locked_relation is None:
                raise KeyError(f"KG relation not found: {relation_id}")
            self._require_current_kg_review_revision(
                locked_relation,
                expected_revision=expected_revision,
                candidate_label="KG relation",
            )
            context = conn.execute(
                self._kg_relation_review_context_sql(), {"id": relation_id}
            ).fetchone()
            self._validate_kg_relation_review_context(
                context,
                relation_id,
                endpoint_locator=locator,
            )
            row = conn.execute(self._confirm_kg_relation_sql(), {"id": relation_id}).fetchone()
            if row is None:
                raise KeyError(f"KG relation not found: {relation_id}")
            evidence = conn.execute(
                self._list_valid_kg_relation_evidence_sql(), {"relation_id": relation_id}
            ).fetchall()
            relation = {
                "id": row["id"],
                "head_entity_id": row["head_entity_id"],
                "relation_type": row["relation_type"],
                "tail_entity_id": row["tail_entity_id"],
                "description": row.get("description"),
                "confidence": row.get("confidence"),
                "status": row.get("status"),
            }
            head = {
                "id": row["head_entity_id"],
                "name": row["head_entity_name"],
                "entity_type": row["head_entity_type"],
            }
            tail = {
                "id": row["tail_entity_id"],
                "name": row["tail_entity_name"],
                "entity_type": row["tail_entity_type"],
            }
            chunk = build_kg_relation_knowledge_chunk_row(
                relation, head, tail, [dict(item) for item in evidence]
            )
            conn.execute(self._insert_knowledge_chunk_sql(), self._knowledge_chunk_payload(chunk))
            return row

    def set_kg_entity_status(self, entity_id: str, status: str) -> dict[str, Any]:
        """退回或停用实体；先锁实体及有序关系，再同步实体、关系和投影。"""
        if status not in {"needs_review", "disabled"}:
            raise ValueError("KG entity usable status requires confirm_kg_entity")
        params = {"id": entity_id, "status": status}
        proj_params = {**params, "source_type": "kg_entity"}
        with self.connect() as conn:
            locked_entity = conn.execute(
                self._lock_kg_entity_review_sql(), {"id": entity_id}
            ).fetchone()
            if locked_entity is None:
                raise KeyError(f"KG entity not found: {entity_id}")
            entity_ids = [entity_id]
            self._lock_kg_entity_relations_in_conn(conn, entity_ids)
            row = conn.execute(self._set_kg_entity_status_sql(), params).fetchone()
            if row is None:
                raise KeyError(f"KG entity not found: {entity_id}")
            conn.execute(self._set_kg_projection_status_sql(), proj_params)
            self._invalidate_kg_entity_relations_in_conn(conn, entity_ids)
        return row

    def set_kg_relation_status(self, relation_id: str, status: str) -> dict[str, Any]:
        """退回或停用关系；关键约束是进入 usable 只能调用 confirm_kg_relation。"""
        if status not in {"needs_review", "disabled"}:
            raise ValueError("KG relation usable status requires confirm_kg_relation")
        params = {"id": relation_id, "status": status}
        proj_params = {**params, "source_type": "kg_relation"}
        with self.connect() as conn:
            row = conn.execute(self._set_kg_relation_status_sql(), params).fetchone()
            if row is None:
                raise KeyError(f"KG relation not found: {relation_id}")
            conn.execute(self._set_kg_projection_status_sql(), proj_params)
        return row

    def _reconcile_kg_source_change_in_conn(
        self,
        conn: Any,
        *,
        source_type: str,
        source_ids: list[str],
        source_chunk_ids: list[str] | None = None,
        delete_evidence: bool = False,
    ) -> None:
        """按来源变化重算 KG；先完成全局锁序和证据变更，再读取实时终态。"""
        normalized_ids = list(dict.fromkeys(item for item in source_ids if item))
        if not normalized_ids:
            return
        params = self._kg_source_invalidation_params(
            source_type=source_type,
            source_ids=normalized_ids,
            source_chunk_ids=source_chunk_ids,
        )
        targets = self._collect_kg_source_lock_targets_in_conn(
            conn,
            params,
            candidate_entity_ids=[],
            candidate_relation_ids=[],
        )
        self._lock_or_upsert_kg_entities_in_conn(
            conn,
            targets["entity_lock_ids"],
            {},
        )
        relation_lock_ids = self._refresh_kg_relation_lock_ids_in_conn(
            conn,
            initial_relation_ids=targets["relation_lock_ids"],
            incident_entity_ids=targets["incident_entity_ids"],
            candidate_relation_ids=[],
        )
        affected_relation_ids = self._lock_or_upsert_kg_relations_in_conn(
            conn,
            relation_lock_ids,
            {},
        )
        if delete_evidence:
            conn.execute(self._delete_kg_source_evidence_sql(), params)
        self._reconcile_kg_owners_after_evidence_change_in_conn(
            conn,
            entity_ids=targets["source_entity_ids"],
            relation_ids=affected_relation_ids,
            entity_revision_ids=targets["source_entity_ids"],
            relation_revision_ids=affected_relation_ids,
        )

    @staticmethod
    def _kg_source_invalidation_params(
        *,
        source_type: str,
        source_ids: list[str],
        source_chunk_ids: list[str] | None,
    ) -> dict[str, Any]:
        """整理来源 scope 参数；ID 去重但不推断 source type 或 chunk locator。"""
        return {
            "source_type": source_type,
            "source_ids": list(dict.fromkeys(item for item in source_ids if item)),
            "source_chunk_ids": list(
                dict.fromkeys(item for item in (source_chunk_ids or []) if item)
            ),
        }

    def _collect_kg_source_lock_targets_in_conn(
        self,
        conn: Any,
        params: dict[str, Any],
        *,
        candidate_entity_ids: list[str],
        candidate_relation_ids: list[str],
    ) -> dict[str, list[str]]:
        """区分受影响与纯锁定 ID；直接关系端点只参与实体锁，不被降级。"""
        source_entity_ids = sorted(
            row["id"]
            for row in conn.execute(
                self._list_kg_source_entity_ids_sql(),
                params,
            ).fetchall()
        )
        direct_relations = conn.execute(
            self._list_kg_source_relations_sql(),
            params,
        ).fetchall()
        direct_relation_ids = sorted(row["id"] for row in direct_relations)
        direct_endpoint_ids = {
            endpoint_id
            for row in direct_relations
            for endpoint_id in (row["head_entity_id"], row["tail_entity_id"])
        }
        normalized_candidate_entities = sorted(set(candidate_entity_ids))
        incident_entity_ids = sorted(
            set(source_entity_ids).union(normalized_candidate_entities)
        )
        entity_lock_ids = sorted(
            set(incident_entity_ids).union(direct_endpoint_ids)
        )
        return {
            "source_entity_ids": source_entity_ids,
            "incident_entity_ids": incident_entity_ids,
            "entity_lock_ids": entity_lock_ids,
            "relation_lock_ids": sorted(
                set(direct_relation_ids).union(candidate_relation_ids)
            ),
        }

    def _refresh_kg_relation_lock_ids_in_conn(
        self,
        conn: Any,
        *,
        initial_relation_ids: list[str],
        incident_entity_ids: list[str],
        candidate_relation_ids: list[str],
    ) -> list[str]:
        """实体阶段后重读关联关系；关键约束是之后不再获取任何实体锁。"""
        incident_relation_ids: list[str] = []
        if incident_entity_ids:
            incident_relation_ids = [
                row["id"]
                for row in conn.execute(
                    self._list_kg_incident_relation_ids_sql(),
                    {"entity_ids": incident_entity_ids},
                ).fetchall()
            ]
        return sorted(
            set(initial_relation_ids)
            .union(incident_relation_ids)
            .union(candidate_relation_ids)
        )

    def _reconcile_kg_owners_after_evidence_change_in_conn(
        self,
        conn: Any,
        *,
        entity_ids: list[str],
        relation_ids: list[str],
        entity_revision_ids: list[str],
        relation_revision_ids: list[str],
    ) -> None:
        """在 owner 已按全局顺序锁定后，精确重算 canonical 状态并同步投影。"""
        normalized_entity_ids = sorted(set(entity_ids))
        normalized_relation_ids = sorted(set(relation_ids))
        entity_rows: list[dict[str, Any]] = []
        relation_rows: list[dict[str, Any]] = []
        if normalized_entity_ids:
            entity_rows = conn.execute(
                self._reconcile_kg_entities_after_evidence_change_sql(),
                {
                    "entity_ids": normalized_entity_ids,
                    "entity_revision_ids": sorted(set(entity_revision_ids)),
                },
            ).fetchall()
        if normalized_relation_ids:
            relation_rows = conn.execute(
                self._reconcile_kg_relations_after_evidence_change_sql(),
                {
                    "relation_ids": normalized_relation_ids,
                    "relation_revision_ids": sorted(set(relation_revision_ids)),
                },
            ).fetchall()
        self._sync_kg_owner_projection_statuses_in_conn(
            conn,
            source_type="kg_entity",
            rows=entity_rows,
        )
        self._sync_kg_owner_projection_statuses_in_conn(
            conn,
            source_type="kg_relation",
            rows=relation_rows,
        )

    @staticmethod
    def _sync_kg_owner_projection_statuses_in_conn(
        conn: Any,
        *,
        source_type: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """逐行同步 owner 返回的 canonical 状态，不从来源事件推测统一终态。"""
        for row in sorted(rows, key=lambda item: item["id"]):
            conn.execute(
                KnowledgeGraphMixin._set_kg_projection_status_sql(),
                {
                    "id": row["id"],
                    "status": row["status"],
                    "source_type": source_type,
                },
            )

    def _lock_kg_document_file_and_chunks_in_conn(
        self,
        conn: Any,
        source_id: str,
    ) -> None:
        """按 file→chunk 固定顺序锁文档来源，供删除和重解析在 KG 失效前调用。"""
        params = {"source_id": source_id}
        conn.execute(self._lock_kg_document_file_source_sql(), params).fetchone()
        conn.execute(self._lock_kg_document_chunks_source_sql(), params).fetchall()

    def _lock_kg_entity_relations_in_conn(
        self,
        conn: Any,
        entity_ids: list[str],
    ) -> None:
        """按实体集合锁定关联关系，关键约束是实体 ID 和关系 ID 均确定性排序。"""
        normalized_ids = sorted(set(entity_ids))
        if not normalized_ids:
            return
        conn.execute(
            self._lock_kg_entity_relations_sql(),
            {"entity_ids": normalized_ids},
        ).fetchall()

    def _invalidate_kg_entity_relations_in_conn(
        self,
        conn: Any,
        entity_ids: list[str],
    ) -> None:
        """按已锁实体集合回退关联关系，关系状态和投影在同一 SQL 内同步。"""
        normalized_ids = sorted(set(entity_ids))
        if not normalized_ids:
            return
        conn.execute(
            self._invalidate_kg_entity_relations_sql(),
            {"entity_ids": normalized_ids},
        )

    @staticmethod
    def _validate_kg_entity_review_context(
        context: dict[str, Any] | None, entity_id: str
    ) -> None:
        """校验实体可确认条件，缺失候选与缺失有效证据分别返回明确异常。"""
        if context is None:
            raise KeyError(f"KG entity not found: {entity_id}")
        if not context.get("has_valid_evidence"):
            raise ValueError("KG entity requires at least one valid evidence")

    @staticmethod
    def _validate_expected_kg_review_revision(expected_revision: int) -> None:
        """校验确认请求 revision；布尔值和非正整数都不是合法审核版本。"""
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")

    @staticmethod
    def _require_current_kg_review_revision(
        locked: dict[str, Any],
        *,
        expected_revision: int,
        candidate_label: str,
    ) -> None:
        """比对锁内 revision；不一致说明审核者看到的候选快照已被替换。"""
        if locked.get("review_revision") != expected_revision:
            raise KgReviewConflictError(
                f"{candidate_label} review revision changed; refresh before confirmation"
            )

    @staticmethod
    def _validate_kg_relation_review_context(
        context: dict[str, Any] | None,
        relation_id: str,
        *,
        endpoint_locator: dict[str, Any],
    ) -> None:
        """校验关系 locator、证据和端点状态，避免并发改向或悬空关系进入投影。"""
        if context is None:
            raise KeyError(f"KG relation not found: {relation_id}")
        if (
            context.get("head_entity_id"),
            context.get("tail_entity_id"),
        ) != (
            endpoint_locator.get("head_entity_id"),
            endpoint_locator.get("tail_entity_id"),
        ):
            raise ValueError("KG relation endpoints changed during confirmation")
        if not context.get("has_valid_evidence"):
            raise ValueError("KG relation requires at least one valid evidence")
        if (
            context.get("head_entity_status") != "usable"
            or context.get("tail_entity_status") != "usable"
        ):
            raise ValueError("KG relation endpoint entities must both be usable")

    def search_kg_knowledge_text(
        self,
        query_text: str,
        *,
        top_k: int,
        query_terms: list[str] | None = None,
    ) -> list[KgFactHit]:
        """检索 usable KG fact，关键约束是结果只作证据展开而非最终候选。"""
        normalized = str(query_text or "").strip()
        if not normalized:
            return []
        terms = [str(item).strip() for item in (query_terms or [normalized]) if str(item).strip()]
        params = {
            "query_like": f"%{normalized}%",
            "query_terms": terms,
            "top_k": top_k,
        }
        with self.connect() as conn:
            rows = conn.execute(self._search_kg_knowledge_text_sql(), params).fetchall()
        return [
            KgFactHit(
                fact_chunk_id=row["id"],
                fact_id=row["source_id"],
                fact_type=row["source_type"],
                fact_rank=rank,
                fact_score=float(row["score"]),
            )
            for rank, row in enumerate(rows, start=1)
        ]

    def expand_kg_fact_hits(
        self,
        fact_hits: list[KgFactHit],
    ) -> list[KgExpandedCandidate]:
        """把 KG fact 展开为实时原始证据，关键约束是按原始知识行 ID 去重。"""
        if not fact_hits:
            return []
        if any(not isinstance(hit, KgFactHit) for hit in fact_hits):
            raise TypeError("KG fact hits must be KgFactHit")
        params = {
            "fact_chunk_ids": [hit.fact_chunk_id for hit in fact_hits],
            "fact_ranks": [hit.fact_rank for hit in fact_hits],
            "fact_scores": [hit.fact_score for hit in fact_hits],
        }
        with self.connect() as conn:
            rows = conn.execute(self._expand_kg_fact_hits_sql(), params).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            match = KgFactHit(
                fact_chunk_id=row["fact_chunk_id"],
                fact_id=row["fact_id"],
                fact_type=row["fact_type"],
                fact_rank=int(row["fact_rank"]),
                fact_score=float(row["fact_score"]),
            )
            item = grouped.setdefault(
                row["id"],
                {
                    "document": self._row_to_retrieved_chunk(row),
                    "best_rank": match.fact_rank,
                    "matches": {},
                },
            )
            if match.fact_rank < item["best_rank"]:
                item["document"] = self._row_to_retrieved_chunk(row)
                item["best_rank"] = match.fact_rank
            existing_match = item["matches"].get(match.fact_chunk_id)
            if existing_match is None or match.fact_rank < existing_match.fact_rank:
                item["matches"][match.fact_chunk_id] = match

        candidates = [
            KgExpandedCandidate(
                document=item["document"],
                kg_matches=tuple(
                    sorted(
                        item["matches"].values(),
                        key=lambda match: (match.fact_rank, match.fact_chunk_id),
                    )
                ),
            )
            for item in grouped.values()
        ]
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate.kg_matches[0].fact_rank,
                candidate.document.id,
            ),
        )

    def get_kg_subgraph(
        self,
        *,
        center_entity_id: str,
        hops: int = 1,
        entity_types: list[str] | None = None,
        relation_types: list[str] | None = None,
        limit: int = 80,
    ) -> dict[str, Any] | None:
        """读取 usable 局部子图，明确区分中心不存在、孤立和已连接三种结果。"""
        params = {
            "center_entity_id": center_entity_id,
            "hops": max(1, min(int(hops or 1), 2)),
            "entity_types": entity_types or [],
            "relation_types": relation_types or [],
            "limit": max(1, min(int(limit or 80), 200)),
        }
        with self.connect() as conn:
            rows = conn.execute(self._kg_subgraph_sql(), params).fetchall()
        if not rows:
            return None
        center = self._kg_center_node_payload(rows[0])
        nodes: dict[str, dict[str, Any]] = {center["id"]: center}
        edges: list[dict[str, Any]] = []
        for row in rows:
            if row.get("relation_id") is None:
                continue
            head = self._kg_node_payload(row, "head")
            tail = self._kg_node_payload(row, "tail")
            nodes[head["id"]] = head
            nodes[tail["id"]] = tail
            edges.append(
                {
                    "id": row["relation_id"],
                    "source": row["head_entity_id"],
                    "target": row["tail_entity_id"],
                    "relation_type": row["relation_type"],
                    "description": row.get("relation_description"),
                    "confidence": row.get("relation_confidence"),
                    "status": row.get("relation_status"),
                    "evidence_count": row["evidence_count"],
                }
            )
        return {
            "state": "connected" if edges else "isolated",
            "center": center,
            "nodes": list(nodes.values()),
            "edges": edges,
        }

    @staticmethod
    def _kg_center_node_payload(row: dict[str, Any]) -> dict[str, Any]:
        """从子图首行整理中心节点，保证 isolated 响应也有完整实体。"""
        return {
            "id": row["center_id"],
            "name": row["center_name"],
            "entity_type": row["center_entity_type"],
            "description": row.get("center_description"),
            "status": row.get("center_status"),
            "confidence": row.get("center_confidence"),
        }

    @staticmethod
    def _kg_node_payload(row: dict[str, Any], prefix: str) -> dict[str, Any]:
        """从子图 SQL 行整理节点，关键约束是字段名稳定供 2D/3D 复用。"""
        return {
            "id": row[f"{prefix}_entity_id"],
            "name": row[f"{prefix}_entity_name"],
            "entity_type": row[f"{prefix}_entity_type"],
            "description": row.get(f"{prefix}_entity_description"),
            "status": row.get(f"{prefix}_entity_status"),
            "confidence": row.get(f"{prefix}_entity_confidence"),
        }

    @staticmethod
    def _new_kg_job_id() -> str:
        """生成 KG 抽取任务 ID，关键约束是短 ID 便于本地 UI 展示。"""
        return f"kg_job_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _new_kg_job_item_id() -> str:
        """生成隐藏 Map item ID，关键约束是只在父任务 staging 内使用。"""
        return f"kg_item_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _insert_kg_extraction_job_sql() -> str:
        """创建当前父任务，并只 RETURNING 公开 wire 字段。"""
        return f"""
        INSERT INTO kg_extraction_jobs (
            id, source_type, source_id, phase, processed_chunks, total_chunks,
            source_fingerprint, resolution_result,
            entity_count, relation_count, evidence_count, model, error
        )
        VALUES (
            %(id)s, %(source_type)s, %(source_id)s, 'queued', 0, %(total_chunks)s,
            %(source_fingerprint)s, NULL,
            0, 0, 0, %(model)s, NULL
        )
        RETURNING {_KG_JOB_PUBLIC_RETURNING_SQL}
        """

    @staticmethod
    def _insert_document_kg_job_item_sql() -> str:
        """写入不可变 manifest item，chunk_id 故意不建立来源 FK。"""
        return """
        INSERT INTO kg_extraction_job_items (
            id, job_id, chunk_id, chunk_order, source_fingerprint,
            section_path, page_start, page_end, phase, map_result, error
        )
        VALUES (
            %(id)s, %(job_id)s, %(chunk_id)s, %(chunk_order)s,
            %(source_fingerprint)s, %(section_path)s::jsonb,
            %(page_start)s, %(page_end)s, 'queued', NULL, NULL
        )
        RETURNING id, job_id, chunk_id, chunk_order, source_fingerprint,
                  section_path, page_start, page_end, phase, map_result, error
        """

    @staticmethod
    def _find_active_kg_extraction_job_sql() -> str:
        """在来源锁内读取 active generation，提供明确冲突而非复用旧 job。"""
        return """
        SELECT job.id
        FROM kg_extraction_jobs AS job
        WHERE job.source_type = %(source_type)s
          AND job.source_id = %(source_id)s
          AND job.phase IN ('queued', 'mapping', 'resolving', 'reducing')
        ORDER BY job.created_at ASC, job.id ASC
        LIMIT 1
        """

    @staticmethod
    def _get_kg_extraction_job_sql() -> str:
        """按稳定 ID 读取公开 DTO，禁止 SELECT staging 或 lease。"""
        return f"""
        SELECT {_KG_JOB_PUBLIC_SELECT_SQL}
        FROM kg_extraction_jobs AS job
        WHERE job.id = %(job_id)s
        """

    @staticmethod
    def _latest_kg_extraction_job_sql() -> str:
        """按 owner 读取最近 generation 的公开 DTO。"""
        return f"""
        SELECT {_KG_JOB_PUBLIC_SELECT_SQL}
        FROM kg_extraction_jobs AS job
        WHERE job.source_type = %(source_type)s
          AND job.source_id = %(source_id)s
        ORDER BY job.created_at DESC, job.id DESC
        LIMIT 1
        """

    @staticmethod
    def _get_kg_extraction_job_for_fence_sql() -> str:
        """无锁快检内部 job；来源锁后仍必须再次锁 job 复核。"""
        return """
        SELECT job.*,
               (job.lease_token IS NOT NULL AND job.lease_expires_at > now())
                   AS lease_is_current
        FROM kg_extraction_jobs AS job
        WHERE job.id = %(job_id)s
        """

    @staticmethod
    def _lock_kg_extraction_job_sql() -> str:
        """在来源层级之后锁父任务，并返回可二次校验的当前 lease。"""
        return """
        SELECT job.*,
               (job.lease_token IS NOT NULL AND job.lease_expires_at > now())
                   AS lease_is_current
        FROM kg_extraction_jobs AS job
        WHERE job.id = %(job_id)s
        FOR UPDATE OF job
        """

    @staticmethod
    def _claim_kg_extraction_job_sql() -> str:
        """用 SKIP LOCKED 领取 due/过期 active job，queued 原子进入 mapping。"""
        return """
        WITH due_job AS (
            SELECT job.id
            FROM kg_extraction_jobs AS job
            WHERE job.phase IN ('queued', 'mapping', 'resolving', 'reducing')
              AND job.next_attempt_at <= now()
              AND (job.lease_token IS NULL OR job.lease_expires_at < now())
            ORDER BY job.next_attempt_at ASC, job.created_at ASC, job.id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE kg_extraction_jobs AS job
        SET phase = CASE WHEN job.phase = 'queued' THEN 'mapping' ELSE job.phase END,
            lease_token = %(lease_token)s,
            lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
            attempt_count = job.attempt_count + 1,
            error = NULL,
            updated_at = now()
        FROM due_job
        WHERE job.id = due_job.id
        RETURNING job.*
        """

    @staticmethod
    def _fail_kg_extraction_job_sql() -> str:
        """仅当前未过期 lease 可写 failed，并清除 parent staging。"""
        return f"""
        UPDATE kg_extraction_jobs AS job
        SET phase = 'failed',
            resolution_result = NULL,
            error = %(error)s,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = now(),
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.phase IN ('mapping', 'resolving', 'reducing')
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
        RETURNING {_KG_JOB_PUBLIC_RETURNING_SQL}
        """

    @staticmethod
    def _clear_failed_kg_job_items_sql() -> str:
        """清除失败 generation 的全部 Map JSON，并标记正在执行的 item。"""
        return """
        UPDATE kg_extraction_job_items AS item
        SET phase = CASE WHEN item.phase = 'mapping' THEN 'failed' ELSE item.phase END,
            map_result = NULL,
            error = CASE WHEN item.phase = 'mapping' THEN %(error)s ELSE item.error END,
            updated_at = now()
        WHERE item.job_id = %(job_id)s
          AND EXISTS (
              SELECT 1
              FROM kg_extraction_jobs AS job
              WHERE job.id = item.job_id
                AND job.phase IN ('mapping', 'resolving', 'reducing')
                AND job.lease_token = %(lease_token)s
                AND job.lease_expires_at > now()
          )
        """

    @staticmethod
    def _get_document_kg_map_item_for_fence_sql() -> str:
        """无锁选取首个未完成 Map item，来源锁后再锁行复核。"""
        return """
        SELECT item.*
        FROM kg_extraction_job_items AS item
        WHERE item.job_id = %(job_id)s
          AND item.phase IN ('queued', 'mapping')
        ORDER BY item.chunk_order ASC, item.id ASC
        LIMIT 1
        """

    @staticmethod
    def _get_document_kg_job_item_by_id_for_fence_sql() -> str:
        """无锁读取指定 Map item，供完成路径确定来源锁目标。"""
        return """
        SELECT item.*
        FROM kg_extraction_job_items AS item
        WHERE item.job_id = %(job_id)s
          AND item.id = %(item_id)s
        """

    @staticmethod
    def _lock_document_kg_job_item_sql() -> str:
        """在 parent job 后锁定 item，固定 parent-before-item 顺序。"""
        return """
        SELECT item.*
        FROM kg_extraction_job_items AS item
        WHERE item.job_id = %(job_id)s
          AND item.id = %(item_id)s
        FOR UPDATE OF item
        """

    @staticmethod
    def _mark_document_kg_job_item_mapping_sql() -> str:
        """把 queued/reclaimed item 标为 mapping，不改父任务计数。"""
        return """
        UPDATE kg_extraction_job_items AS item
        SET phase = 'mapping', error = NULL, updated_at = now()
        WHERE item.job_id = %(job_id)s
          AND item.id = %(item_id)s
          AND item.phase IN ('queued', 'mapping')
        RETURNING item.*
        """

    @staticmethod
    def _complete_document_kg_map_item_sql() -> str:
        """把一个 mapping item 写成 mapped；父任务计数由下一条 SQL 原子推进。"""
        return """
        UPDATE kg_extraction_job_items AS item
        SET phase = 'mapped',
            map_result = %(map_result)s::jsonb,
            error = NULL,
            updated_at = now()
        WHERE item.job_id = %(job_id)s
          AND item.id = %(item_id)s
          AND item.phase = 'mapping'
        RETURNING item.id
        """

    @staticmethod
    def _advance_document_kg_map_progress_sql() -> str:
        """仅当前 lease 增加一次 processed_chunks，最后一片进入 resolving。"""
        return f"""
        UPDATE kg_extraction_jobs AS job
        SET processed_chunks = job.processed_chunks + 1,
            phase = CASE
                WHEN job.processed_chunks + 1 = job.total_chunks THEN 'resolving'
                ELSE 'mapping'
            END,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = now(),
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.source_type = 'document'
          AND job.phase = 'mapping'
          AND job.processed_chunks < job.total_chunks
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
        RETURNING {_KG_JOB_PUBLIC_RETURNING_SQL}
        """

    @staticmethod
    def _load_document_kg_map_results_sql() -> str:
        """按 immutable chunk_order 读取全部隐藏 Map JSON。"""
        return """
        SELECT item.phase, item.map_result
        FROM kg_extraction_job_items AS item
        WHERE item.job_id = %(job_id)s
        ORDER BY item.chunk_order ASC, item.id ASC
        """

    @staticmethod
    def _save_document_kg_resolution_sql() -> str:
        """仅 resolving lease 可保存 object staging 并进入 reducing。"""
        return f"""
        UPDATE kg_extraction_jobs AS job
        SET resolution_result = %(resolution_result)s::jsonb,
            phase = 'reducing',
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = now(),
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.source_type = 'document'
          AND job.phase = 'resolving'
          AND job.processed_chunks = job.total_chunks
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
        RETURNING {_KG_JOB_PUBLIC_RETURNING_SQL}
        """

    @staticmethod
    def _lock_document_kg_job_items_sql() -> str:
        """在父任务锁后按稳定 ID 锁全部 manifest items，避免并发清理 staging。"""
        return """
        SELECT item.*
        FROM kg_extraction_job_items AS item
        WHERE item.job_id = %(job_id)s
        ORDER BY item.id ASC
        FOR UPDATE OF item
        """

    @staticmethod
    def _complete_faq_kg_extraction_job_sql() -> str:
        """仅当前 FAQ mapping lease 可在 fingerprint 不变时写 completed 终态。"""
        return f"""
        UPDATE kg_extraction_jobs AS job
        SET phase = 'completed',
            processed_chunks = 1,
            entity_count = %(entity_count)s,
            relation_count = %(relation_count)s,
            evidence_count = %(evidence_count)s,
            resolution_result = NULL,
            error = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = now(),
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.source_type = 'faq'
          AND job.phase = 'mapping'
          AND job.processed_chunks = 0
          AND job.total_chunks = 1
          AND job.source_fingerprint = %(source_fingerprint)s
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
        RETURNING {_KG_JOB_PUBLIC_RETURNING_SQL}
        """

    @staticmethod
    def _complete_document_kg_extraction_job_sql() -> str:
        """仅完整 Reduce generation 可在同一事务写 counts、completed 并清 lease。"""
        return f"""
        UPDATE kg_extraction_jobs AS job
        SET phase = 'completed',
            entity_count = %(entity_count)s,
            relation_count = %(relation_count)s,
            evidence_count = %(evidence_count)s,
            resolution_result = NULL,
            error = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_attempt_at = now(),
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.source_type = 'document'
          AND job.phase = 'reducing'
          AND job.processed_chunks = job.total_chunks
          AND job.resolution_result IS NOT NULL
          AND job.source_fingerprint = %(source_fingerprint)s
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
        RETURNING {_KG_JOB_PUBLIC_RETURNING_SQL}
        """

    @staticmethod
    def _clear_completed_document_kg_staging_sql() -> str:
        """完成事务内清空文档 Map JSON；保留 item locator 与 phase 供任务审计。"""
        return """
        UPDATE kg_extraction_job_items AS item
        SET map_result = NULL,
            error = NULL,
            updated_at = now()
        WHERE item.job_id = %(job_id)s
        """

    @staticmethod
    def _lock_kg_faq_source_sql() -> str:
        """锁定 FAQ 抽取来源，防止版本校验后正文在候选提交前变化。"""
        return """
        SELECT id, question, answer, category, tags, status
        FROM faq_documents faq
        WHERE faq.id = %(source_id)s
        FOR UPDATE OF faq
        """

    @staticmethod
    def _lock_kg_document_file_source_sql() -> str:
        """先锁定文档文件，统一候选保存、删除和重解析的第一层锁顺序。"""
        return """
        SELECT imp.id, imp.original_name, imp.status, imp.is_disabled
        FROM import_files imp
        WHERE imp.id = %(source_id)s
        FOR UPDATE OF imp
        """

    @staticmethod
    def _lock_kg_document_chunk_source_sql() -> str:
        """在文件锁后锁定精确切片，供迟到抽取结果做来源指纹复核。"""
        return """
        SELECT chunk.id, chunk.file_id, chunk.source_text,
               chunk.section_path, chunk.page_start, chunk.page_end,
               chunk.is_disabled
        FROM import_chunks chunk
        WHERE chunk.id = %(source_chunk_id)s
          AND chunk.file_id = %(source_id)s
        FOR UPDATE OF chunk
        """

    @staticmethod
    def _lock_kg_document_chunks_source_sql() -> str:
        """在文件锁后按 ID 锁全部切片，并返回 manifest 所需 canonical locator。"""
        return """
        SELECT chunk.id, chunk.file_id, chunk.chunk_index, chunk.source_text,
               chunk.section_path, chunk.page_start, chunk.page_end,
               chunk.is_disabled
        FROM import_chunks chunk
        WHERE chunk.file_id = %(source_id)s
        ORDER BY chunk.id ASC
        FOR UPDATE OF chunk
        """

    @staticmethod
    def _list_kg_entities_sql(*, where: str = "") -> str:
        """KG 实体审核列表 SQL，保留历史证据并逐条标记实时有效性。"""
        return f"""
        SELECT
            ent.*,
            {_kg_valid_evidence_exists_sql("valid_ev.entity_id = ent.id")} AS has_valid_evidence,
            COALESCE(
                jsonb_agg(
                    jsonb_build_object(
                        'id', ev.id,
                        'source_type', ev.source_type,
                        'source_id', ev.source_id,
                        'source_chunk_id', ev.source_chunk_id,
                        'source_title', ev.source_title,
                        'section_path', ev.section_path,
                        'page_start', ev.page_start,
                        'page_end', ev.page_end,
                        'excerpt', ev.excerpt,
                        'char_start', ev.char_start,
                        'char_end', ev.char_end,
                        'is_valid', {_kg_valid_evidence_exists_sql("valid_ev.id = ev.id")}
                    )
                    ORDER BY ev.created_at ASC, ev.id ASC
                ) FILTER (WHERE ev.id IS NOT NULL),
                '[]'::jsonb
            ) AS evidence
        FROM kg_entities ent
        LEFT JOIN kg_evidence ev ON ev.entity_id = ent.id
        {where}
        GROUP BY ent.id
        ORDER BY ent.updated_at DESC, ent.id ASC
        LIMIT %(limit)s OFFSET %(offset)s
        """

    @staticmethod
    def _count_kg_entities_sql(*, where: str = "") -> str:
        """KG 实体审核列表计数 SQL，关键约束是和列表筛选口径一致。"""
        return f"""
        SELECT count(*) AS total
        FROM kg_entities ent
        {where}
        """

    @staticmethod
    def _list_kg_relations_sql(*, where: str = "") -> str:
        """KG 关系审核列表 SQL，返回端点、历史证据有效性和实时证据数。"""
        return f"""
        SELECT
            rel.*,
            head.name AS head_entity_name,
            head.entity_type AS head_entity_type,
            head.status AS head_entity_status,
            tail.name AS tail_entity_name,
            tail.entity_type AS tail_entity_type,
            tail.status AS tail_entity_status,
            {_kg_valid_evidence_exists_sql("valid_ev.relation_id = rel.id")} AS has_valid_evidence,
            (
                {_kg_valid_evidence_count_sql("valid_ev.relation_id = rel.id")}
            ) AS evidence_count,
            COALESCE(
                jsonb_agg(
                    jsonb_build_object(
                        'id', ev.id,
                        'source_type', ev.source_type,
                        'source_id', ev.source_id,
                        'source_chunk_id', ev.source_chunk_id,
                        'source_title', ev.source_title,
                        'section_path', ev.section_path,
                        'page_start', ev.page_start,
                        'page_end', ev.page_end,
                        'excerpt', ev.excerpt,
                        'char_start', ev.char_start,
                        'char_end', ev.char_end,
                        'is_valid', {_kg_valid_evidence_exists_sql("valid_ev.id = ev.id")}
                    )
                    ORDER BY ev.created_at ASC, ev.id ASC
                ) FILTER (WHERE ev.id IS NOT NULL),
                '[]'::jsonb
            ) AS evidence
        FROM kg_relations rel
        JOIN kg_entities head ON head.id = rel.head_entity_id
        JOIN kg_entities tail ON tail.id = rel.tail_entity_id
        LEFT JOIN kg_evidence ev ON ev.relation_id = rel.id
        {where}
        GROUP BY rel.id, head.id, tail.id
        ORDER BY rel.updated_at DESC, rel.id ASC
        LIMIT %(limit)s OFFSET %(offset)s
        """

    @staticmethod
    def _count_kg_relations_sql(*, where: str = "") -> str:
        """KG 关系审核列表计数 SQL，关键约束是和列表筛选口径一致。"""
        return f"""
        SELECT count(*) AS total
        FROM kg_relations rel
        {where}
        """

    @staticmethod
    def _kg_entity_params(entity: dict[str, Any]) -> dict[str, Any]:
        """整理实体写入参数，关键约束是 AI 候选一律进入 needs_review。"""
        return {
            "id": entity["id"],
            "name": entity["name"],
            "entity_type": entity["entity_type"],
            "aliases": json.dumps(entity.get("aliases", []), ensure_ascii=False),
            "description": entity.get("description"),
            "status": "needs_review",
            "confidence": entity.get("confidence"),
        }

    @staticmethod
    def _kg_relation_params(relation: dict[str, Any]) -> dict[str, Any]:
        """整理关系写入参数，关键约束是 AI 候选一律进入 needs_review。"""
        return {
            "id": relation["id"],
            "head_entity_id": relation["head_entity_id"],
            "relation_type": relation["relation_type"],
            "tail_entity_id": relation["tail_entity_id"],
            "description": relation.get("description"),
            "status": "needs_review",
            "confidence": relation.get("confidence"),
        }

    @classmethod
    def _kg_evidence_params(
        cls,
        evidence: dict[str, Any],
        *,
        entity_id: str | None = None,
        relation_id: str | None = None,
    ) -> dict[str, Any]:
        """整理证据写入参数，关键约束是一条证据只绑定实体或关系之一。"""
        return {
            "id": cls._kg_evidence_id(evidence, entity_id=entity_id, relation_id=relation_id),
            "entity_id": entity_id,
            "relation_id": relation_id,
            "source_type": evidence["source_type"],
            "source_id": evidence["source_id"],
            "source_chunk_id": evidence.get("source_chunk_id"),
            "source_title": evidence.get("source_title"),
            "section_path": json.dumps(evidence.get("section_path", []), ensure_ascii=False),
            "page_start": evidence.get("page_start"),
            "page_end": evidence.get("page_end"),
            "excerpt": evidence["excerpt"],
            "char_start": evidence["char_start"],
            "char_end": evidence["char_end"],
        }

    @staticmethod
    def _kg_evidence_id(
        evidence: dict[str, Any],
        *,
        entity_id: str | None,
        relation_id: str | None,
    ) -> str:
        """生成稳定证据 ID，关键约束是重复抽取同一证据可幂等去重。"""
        payload = {
            "entity_id": entity_id,
            "relation_id": relation_id,
            "source_type": evidence.get("source_type"),
            "source_id": evidence.get("source_id"),
            "source_chunk_id": evidence.get("source_chunk_id"),
            "excerpt": evidence.get("excerpt"),
            "char_start": evidence.get("char_start"),
            "char_end": evidence.get("char_end"),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "kg_ev_" + hashlib.sha256(encoded).hexdigest()[:16]

    @staticmethod
    def _upsert_kg_entity_sql() -> str:
        """实体候选 upsert SQL，关键约束是重复抽取后强制重新人工审核。"""
        return """
        INSERT INTO kg_entities (
            id, name, entity_type, aliases, description, status, confidence
        )
        VALUES (
            %(id)s, %(name)s, %(entity_type)s, %(aliases)s::jsonb, %(description)s,
            %(status)s, %(confidence)s
        )
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            entity_type = EXCLUDED.entity_type,
            aliases = EXCLUDED.aliases,
            description = EXCLUDED.description,
            status = EXCLUDED.status,
            review_revision = kg_entities.review_revision + 1,
            confidence = EXCLUDED.confidence,
            updated_at = now()
        """

    @staticmethod
    def _upsert_kg_relation_sql() -> str:
        """关系候选 upsert SQL，关键约束是重复抽取保持 ID 但撤销旧审核状态。"""
        return """
        INSERT INTO kg_relations (
            id, head_entity_id, relation_type, tail_entity_id, description, status, confidence
        )
        VALUES (
            %(id)s, %(head_entity_id)s, %(relation_type)s, %(tail_entity_id)s,
            %(description)s, %(status)s, %(confidence)s
        )
        ON CONFLICT (id) DO UPDATE SET
            description = EXCLUDED.description,
            status = EXCLUDED.status,
            review_revision = kg_relations.review_revision + 1,
            confidence = EXCLUDED.confidence,
            updated_at = now()
        """

    @staticmethod
    def _insert_kg_evidence_sql() -> str:
        """证据写入 SQL，关键约束是同一证据 ID 重复写入时保持幂等。"""
        return """
        INSERT INTO kg_evidence (
            id, entity_id, relation_id, source_type, source_id, source_chunk_id,
            source_title, section_path, page_start, page_end, excerpt,
            char_start, char_end
        )
        VALUES (
            %(id)s, %(entity_id)s, %(relation_id)s, %(source_type)s, %(source_id)s,
            %(source_chunk_id)s, %(source_title)s, %(section_path)s::jsonb,
            %(page_start)s, %(page_end)s, %(excerpt)s,
            %(char_start)s, %(char_end)s
        )
        ON CONFLICT (id) DO NOTHING
        """

    @staticmethod
    def _lock_kg_entity_review_sql() -> str:
        """先锁定待审核实体；证据必须在获锁后的下一条语句中重新读取。"""
        return """
        SELECT ent.id, ent.review_revision
        FROM kg_entities ent
        WHERE ent.id = %(id)s
        FOR UPDATE OF ent
        """

    @staticmethod
    def _kg_entity_review_context_sql() -> str:
        """获锁后读取实体审核上下文，关键约束是使用新的语句快照校验证据。"""
        return f"""
        SELECT
            ent.*,
            {_kg_valid_evidence_exists_sql("valid_ev.entity_id = ent.id")} AS has_valid_evidence
        FROM kg_entities ent
        WHERE ent.id = %(id)s
        """

    @staticmethod
    def _get_kg_relation_endpoint_locator_sql() -> str:
        """读取关系端点 locator，供事务按端点 ID 升序建立统一锁序。"""
        return """
        SELECT rel.head_entity_id, rel.tail_entity_id
        FROM kg_relations rel
        WHERE rel.id = %(id)s
        """

    @staticmethod
    def _lock_kg_relation_entities_sql() -> str:
        """按实体 ID 升序锁定关系端点，避免模型返回顺序影响并发等待图。"""
        return """
        SELECT ent.id
        FROM kg_entities ent
        WHERE ent.id = ANY(%(entity_ids)s::text[])
        ORDER BY ent.id ASC
        FOR UPDATE OF ent
        """

    @staticmethod
    def _lock_kg_relation_review_sql() -> str:
        """在端点实体之后锁定关系；证据和端点状态须在下一条语句重新读取。"""
        return """
        SELECT rel.id, rel.review_revision
        FROM kg_relations rel
        WHERE rel.id = %(id)s
        FOR UPDATE OF rel
        """

    @staticmethod
    def _lock_kg_entity_relations_sql() -> str:
        """锁定实体集合的关联关系并按关系 ID 升序，统一降级与抽取锁序。"""
        return """
        SELECT rel.id
        FROM kg_relations rel
        WHERE rel.head_entity_id = ANY(%(entity_ids)s::text[])
           OR rel.tail_entity_id = ANY(%(entity_ids)s::text[])
        ORDER BY rel.id ASC
        FOR UPDATE OF rel
        """

    @staticmethod
    def _kg_relation_review_context_sql() -> str:
        """获锁后读取关系上下文，关键约束是用新快照校验证据和两端状态。"""
        return f"""
        SELECT
            rel.*,
            head.name AS head_entity_name,
            head.entity_type AS head_entity_type,
            head.status AS head_entity_status,
            tail.name AS tail_entity_name,
            tail.entity_type AS tail_entity_type,
            tail.status AS tail_entity_status,
            {_kg_valid_evidence_exists_sql("valid_ev.relation_id = rel.id")} AS has_valid_evidence
        FROM kg_relations rel
        JOIN kg_entities head ON head.id = rel.head_entity_id
        JOIN kg_entities tail ON tail.id = rel.tail_entity_id
        WHERE rel.id = %(id)s
        """

    @staticmethod
    def _confirm_kg_entity_sql() -> str:
        """确认实体 SQL，关键约束是只把待审核/禁用实体显式切到 usable。"""
        return """
        UPDATE kg_entities
        SET status = 'usable', updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """

    @staticmethod
    def _confirm_kg_relation_sql() -> str:
        """确认关系 SQL，关键约束是同时取回头尾实体名称和类型用于投影。"""
        return """
        WITH updated AS (
            UPDATE kg_relations
            SET status = 'usable', updated_at = now()
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT
            updated.*,
            head.name AS head_entity_name,
            head.entity_type AS head_entity_type,
            tail.name AS tail_entity_name,
            tail.entity_type AS tail_entity_type
        FROM updated
        JOIN kg_entities head ON head.id = updated.head_entity_id
        JOIN kg_entities tail ON tail.id = updated.tail_entity_id
        """

    @staticmethod
    def _set_kg_entity_status_sql() -> str:
        """KG 实体状态更新 SQL，关键约束是只改审核状态不改证据。"""
        return """
        UPDATE kg_entities
        SET status = %(status)s,
            review_revision = review_revision + 1,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """

    @staticmethod
    def _set_kg_relation_status_sql() -> str:
        """KG 关系状态更新 SQL，关键约束是返回头尾实体信息供按需投影。"""
        return """
        WITH updated AS (
            UPDATE kg_relations
            SET status = %(status)s,
                review_revision = review_revision + 1,
                updated_at = now()
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT
            updated.*,
            head.name AS head_entity_name,
            head.entity_type AS head_entity_type,
            tail.name AS tail_entity_name,
            tail.entity_type AS tail_entity_type
        FROM updated
        JOIN kg_entities head ON head.id = updated.head_entity_id
        JOIN kg_entities tail ON tail.id = updated.tail_entity_id
        """

    @staticmethod
    def _set_kg_projection_status_sql() -> str:
        """KG 投影状态同步 SQL，关键约束是禁用后检索立即不可见。"""
        return """
        UPDATE knowledge_chunks
        SET status = %(status)s, updated_at = now()
        WHERE source_type = %(source_type)s
          AND source_id = %(id)s
        """

    @staticmethod
    def _invalidate_kg_entity_relations_sql() -> str:
        """按实体集合级联回退可用关系及投影，调用方必须先按关系 ID 加锁。"""
        return """
        WITH invalidated_relations AS (
            UPDATE kg_relations
            SET status = 'needs_review',
                review_revision = review_revision + 1,
                updated_at = now()
            WHERE status = 'usable'
              AND (
                  head_entity_id = ANY(%(entity_ids)s::text[])
                  OR tail_entity_id = ANY(%(entity_ids)s::text[])
              )
            RETURNING id
        )
        UPDATE knowledge_chunks
        SET status = 'needs_review', updated_at = now()
        WHERE source_type = 'kg_relation'
          AND source_id IN (SELECT id FROM invalidated_relations)
        """

    @staticmethod
    def _list_kg_source_entity_ids_sql() -> str:
        """读取来源直接影响的实体 ID；这里只定语义集合，不在查询阶段取行锁。"""
        return """
        SELECT DISTINCT evidence.entity_id AS id
        FROM kg_evidence AS evidence
        WHERE evidence.source_type = %(source_type)s
          AND evidence.source_id = ANY(%(source_ids)s::text[])
          AND (
              cardinality(%(source_chunk_ids)s::text[]) = 0
              OR evidence.source_chunk_id = ANY(%(source_chunk_ids)s::text[])
          )
          AND evidence.entity_id IS NOT NULL
        ORDER BY evidence.entity_id ASC
        """

    @staticmethod
    def _list_kg_source_relations_sql() -> str:
        """读取来源直接影响的关系及端点；端点仅参与全局实体锁，不等同受影响实体。"""
        return """
        SELECT DISTINCT rel.id, rel.head_entity_id, rel.tail_entity_id
        FROM kg_relations rel
        JOIN kg_evidence AS evidence ON evidence.relation_id = rel.id
        WHERE evidence.source_type = %(source_type)s
          AND evidence.source_id = ANY(%(source_ids)s::text[])
          AND (
              cardinality(%(source_chunk_ids)s::text[]) = 0
              OR evidence.source_chunk_id = ANY(%(source_chunk_ids)s::text[])
          )
        ORDER BY rel.id ASC
        """

    @staticmethod
    def _list_kg_incident_relation_ids_sql() -> str:
        """实体阶段后读取关联关系 ID；新快照保证之后不会再获取实体锁。"""
        return """
        SELECT rel.id
        FROM kg_relations AS rel
        WHERE rel.head_entity_id = ANY(%(entity_ids)s::text[])
           OR rel.tail_entity_id = ANY(%(entity_ids)s::text[])
        ORDER BY rel.id ASC
        """

    @staticmethod
    def _reconcile_kg_entities_after_evidence_change_sql() -> str:
        """按实时去重来源重算实体计数与终态，revision 只递增语义变化集合。"""
        return f"""
        WITH live_counts AS (
            SELECT
                target.id,
                (
                    COUNT(DISTINCT (
                        valid_ev.source_type,
                        valid_ev.source_id,
                        COALESCE(valid_ev.source_chunk_id, '')
                    )) FILTER (WHERE {_kg_live_evidence_predicate_sql()})
                )::integer AS source_count
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
            status = CASE
                WHEN live_counts.source_count = 0 THEN 'disabled'
                ELSE 'needs_review'
            END,
            review_revision = entity.review_revision + CASE
                WHEN entity.id = ANY(%(entity_revision_ids)s::text[]) THEN 1
                ELSE 0
            END,
            updated_at = now()
        FROM live_counts
        WHERE entity.id = live_counts.id
        RETURNING entity.id, entity.status, entity.source_count, entity.review_revision
        """

    @staticmethod
    def _reconcile_kg_relations_after_evidence_change_sql() -> str:
        """按实时证据数重算关系终态；端点变化只撤销审核，不制造关系证据。"""
        return f"""
        WITH live_counts AS (
            SELECT
                target.id,
                (
                    COUNT(valid_ev.id) FILTER (WHERE {_kg_live_evidence_predicate_sql()})
                )::integer AS evidence_count
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
        SET status = CASE
                WHEN live_counts.evidence_count = 0 THEN 'disabled'
                ELSE 'needs_review'
            END,
            review_revision = relation.review_revision + CASE
                WHEN relation.id = ANY(%(relation_revision_ids)s::text[]) THEN 1
                ELSE 0
            END,
            updated_at = now()
        FROM live_counts
        WHERE relation.id = live_counts.id
        RETURNING relation.id, relation.status, relation.review_revision
        """

    @staticmethod
    def _delete_kg_source_evidence_sql() -> str:
        """删除正文已变化或已删除来源的旧证据，避免旧 excerpt 被再次确认。"""
        return """
        DELETE FROM kg_evidence
        WHERE source_type = %(source_type)s
          AND source_id = ANY(%(source_ids)s::text[])
          AND (
              cardinality(%(source_chunk_ids)s::text[]) = 0
              OR source_chunk_id = ANY(%(source_chunk_ids)s::text[])
          )
        """

    @staticmethod
    def _list_valid_kg_entity_evidence_sql() -> str:
        """读取实体当前有效证据，投影不得混入保留的历史 excerpt。"""
        select_sql = _kg_valid_evidence_select_sql(
            "valid_ev.entity_id = %(entity_id)s",
            columns=(
                "valid_ev.source_type, valid_ev.source_id, "
                "valid_ev.source_chunk_id, valid_ev.source_title, "
                "valid_ev.section_path, valid_ev.page_start, "
                "valid_ev.page_end, valid_ev.excerpt, "
                "valid_ev.char_start, valid_ev.char_end"
            ),
        )
        return f"{select_sql} ORDER BY valid_ev.created_at ASC, valid_ev.id ASC"

    @staticmethod
    def _list_valid_kg_relation_evidence_sql() -> str:
        """读取关系当前有效证据，与确认门禁共用来源状态口径。"""
        select_sql = _kg_valid_evidence_select_sql(
            "valid_ev.relation_id = %(relation_id)s",
            columns=(
                "valid_ev.source_type, valid_ev.source_id, "
                "valid_ev.source_chunk_id, valid_ev.source_title, "
                "valid_ev.section_path, valid_ev.page_start, "
                "valid_ev.page_end, valid_ev.excerpt, "
                "valid_ev.char_start, valid_ev.char_end"
            ),
        )
        return f"{select_sql} ORDER BY valid_ev.created_at ASC, valid_ev.id ASC"

    @staticmethod
    def _search_kg_knowledge_text_sql() -> str:
        """KG 投影关键词召回 SQL，关键约束是显式只读 kg_entity/kg_relation。"""
        return """
        SELECT
            kc.id, kc.source_type, kc.source_id, kc.source_chunk_id, kc.parent_chunk_id,
            kc.chunk_level, kc.source_title, kc.section_path, kc.page_start, kc.page_end,
            kc.block_type, kc.source_offsets, kc.content,
            kc.metadata, kc.tags, kc.confidence, kc.status,
            (
                CASE WHEN kc.source_title ILIKE %(query_like)s THEN 0.45 ELSE 0 END
                + CASE WHEN kc.search_text ILIKE %(query_like)s THEN 0.35 ELSE 0 END
                + CASE WHEN kc.content ILIKE %(query_like)s THEN 0.20 ELSE 0 END
                + COALESCE((
                    SELECT sum(
                        CASE WHEN kc.source_title ILIKE ('%%' || term || '%%') THEN 0.18 ELSE 0 END
                        + CASE WHEN kc.search_text ILIKE ('%%' || term || '%%') THEN 0.12 ELSE 0 END
                        + CASE WHEN kc.content ILIKE ('%%' || term || '%%') THEN 0.06 ELSE 0 END
                    )
                    FROM unnest(%(query_terms)s::text[]) AS term
                ), 0)
            ) AS score
        FROM knowledge_chunks kc
        WHERE kc.status = 'usable'
          AND kc.source_type IN ('kg_entity', 'kg_relation')
          AND (
              kc.source_title ILIKE %(query_like)s
              OR kc.content ILIKE %(query_like)s
              OR kc.search_text ILIKE %(query_like)s
              OR EXISTS (
                  SELECT 1
                  FROM unnest(%(query_terms)s::text[]) AS term
                  WHERE kc.source_title ILIKE ('%%' || term || '%%')
                     OR kc.content ILIKE ('%%' || term || '%%')
                     OR kc.search_text ILIKE ('%%' || term || '%%')
              )
          )
        ORDER BY score DESC, kc.updated_at DESC, kc.id ASC
        LIMIT %(top_k)s
        """

    @staticmethod
    def _expand_kg_fact_hits_sql() -> str:
        """按精确证据展开 KG fact，只返回实时可用 FAQ 或文档 direct child。"""
        return """
        WITH fact_hits AS (
            SELECT
                hit.fact_chunk_id,
                hit.fact_rank,
                hit.fact_score
            FROM unnest(
                %(fact_chunk_ids)s::text[],
                %(fact_ranks)s::integer[],
                %(fact_scores)s::double precision[]
            ) AS hit(fact_chunk_id, fact_rank, fact_score)
        ),
        entity_facts AS (
            SELECT
                hit.fact_chunk_id,
                fact.source_id AS fact_id,
                fact.source_type AS fact_type,
                hit.fact_rank,
                hit.fact_score
            FROM fact_hits hit
            JOIN knowledge_chunks fact
              ON fact.id = hit.fact_chunk_id
             AND fact.source_type = 'kg_entity'
             AND fact.status = 'usable'
            JOIN kg_entities entity
              ON entity.id = fact.source_id
             AND entity.status = 'usable'
        ),
        relation_facts AS (
            SELECT
                hit.fact_chunk_id,
                fact.source_id AS fact_id,
                fact.source_type AS fact_type,
                hit.fact_rank,
                hit.fact_score
            FROM fact_hits hit
            JOIN knowledge_chunks fact
              ON fact.id = hit.fact_chunk_id
             AND fact.source_type = 'kg_relation'
             AND fact.status = 'usable'
            JOIN kg_relations relation
              ON relation.id = fact.source_id
             AND relation.status = 'usable'
            JOIN kg_entities head
              ON head.id = relation.head_entity_id
             AND head.status = 'usable'
            JOIN kg_entities tail
              ON tail.id = relation.tail_entity_id
             AND tail.status = 'usable'
        ),
        evidence AS (
            SELECT
                fact.fact_chunk_id,
                fact.fact_id,
                fact.fact_type,
                fact.fact_rank,
                fact.fact_score,
                evidence_row.source_type,
                evidence_row.source_id,
                evidence_row.source_chunk_id
            FROM entity_facts fact
            JOIN kg_evidence evidence_row ON evidence_row.entity_id = fact.fact_id
            UNION ALL
            SELECT
                fact.fact_chunk_id,
                fact.fact_id,
                fact.fact_type,
                fact.fact_rank,
                fact.fact_score,
                evidence_row.source_type,
                evidence_row.source_id,
                evidence_row.source_chunk_id
            FROM relation_facts fact
            JOIN kg_evidence evidence_row ON evidence_row.relation_id = fact.fact_id
        ),
        candidate_links AS (
            SELECT DISTINCT
                original.id AS original_id,
                evidence.fact_chunk_id,
                evidence.fact_id,
                evidence.fact_type,
                evidence.fact_rank,
                evidence.fact_score
            FROM evidence
            JOIN faq_documents faq ON faq.id = evidence.source_id
            JOIN knowledge_chunks original
              ON original.source_type = 'faq'
             AND original.source_id = evidence.source_id
             AND original.source_chunk_id IS NULL
             AND original.chunk_level = 'chunk'
             AND original.chunk_index = 0
            WHERE evidence.source_type = 'faq'
              AND evidence.source_chunk_id IS NULL
              AND faq.status = 'usable'
              AND faq.embedding_status = 'ready'
              AND original.status = 'usable'
              AND original.embedding_status = 'ready'
            UNION ALL
            SELECT DISTINCT
                original.id AS original_id,
                evidence.fact_chunk_id,
                evidence.fact_id,
                evidence.fact_type,
                evidence.fact_rank,
                evidence.fact_score
            FROM evidence
            JOIN import_files import_file
              ON import_file.id = evidence.source_id
             AND import_file.is_disabled = false
            JOIN import_chunks import_chunk
              ON import_chunk.id = evidence.source_chunk_id
             AND import_chunk.file_id = evidence.source_id
             AND import_chunk.is_disabled = false
            JOIN knowledge_chunks original
              ON original.source_type = 'document'
             AND original.source_id = evidence.source_id
             AND original.source_chunk_id = evidence.source_chunk_id
             AND original.chunk_level = 'child'
            WHERE evidence.source_type = 'document'
              AND original.status = 'usable'
              AND original.embedding_status = 'ready'
        )
        SELECT
            original.id,
            original.source_type,
            original.source_id,
            original.source_chunk_id,
            original.parent_chunk_id,
            original.chunk_level,
            original.source_title,
            original.section_path,
            original.page_start,
            original.page_end,
            original.block_type,
            original.source_offsets,
            original.content,
            original.metadata,
            original.tags,
            original.confidence,
            original.status,
            candidate_links.fact_score AS score,
            candidate_links.fact_chunk_id,
            candidate_links.fact_id,
            candidate_links.fact_type,
            candidate_links.fact_rank,
            candidate_links.fact_score
        FROM candidate_links
        JOIN knowledge_chunks original ON original.id = candidate_links.original_id
        ORDER BY candidate_links.fact_rank ASC, original.id ASC
        """

    @staticmethod
    def _kg_subgraph_sql() -> str:
        """usable-only 子图 SQL，保留孤立中心且边只统计实时有效证据。"""
        return f"""
        WITH RECURSIVE center AS (
            SELECT
                center.id,
                center.name,
                center.entity_type,
                center.description,
                center.status,
                center.confidence
            FROM kg_entities center
            WHERE center.id = %(center_entity_id)s
              AND center.status = 'usable'
        ),
        reachable AS (
            SELECT center.id AS entity_id, 0 AS depth
            FROM center
            UNION
            SELECT
                CASE WHEN relation.head_entity_id = reachable.entity_id
                     THEN relation.tail_entity_id
                     ELSE relation.head_entity_id
                END AS entity_id,
                reachable.depth + 1 AS depth
            FROM reachable
            JOIN kg_relations relation
              ON (
                    relation.head_entity_id = reachable.entity_id
                    OR relation.tail_entity_id = reachable.entity_id
                 )
             AND relation.status = 'usable'
            JOIN kg_entities neighbor ON neighbor.id = (
                CASE WHEN relation.head_entity_id = reachable.entity_id
                     THEN relation.tail_entity_id
                     ELSE relation.head_entity_id
                END
            )
            WHERE reachable.depth < %(hops)s
              AND neighbor.status = 'usable'
              AND (
                    cardinality(%(relation_types)s::text[]) = 0
                    OR relation.relation_type = ANY(%(relation_types)s::text[])
                  )
        ),
        graph_edges AS (
            SELECT DISTINCT
            relation.id AS relation_id,
            relation.relation_type,
            relation.description AS relation_description,
            relation.confidence AS relation_confidence,
            relation.status AS relation_status,
            head.id AS head_entity_id,
            head.name AS head_entity_name,
            head.entity_type AS head_entity_type,
            head.description AS head_entity_description,
            head.status AS head_entity_status,
            head.confidence AS head_entity_confidence,
            tail.id AS tail_entity_id,
            tail.name AS tail_entity_name,
            tail.entity_type AS tail_entity_type,
            tail.description AS tail_entity_description,
            tail.status AS tail_entity_status,
            tail.confidence AS tail_entity_confidence,
            (
                {_kg_valid_evidence_count_sql("valid_ev.relation_id = relation.id")}
            ) AS evidence_count,
            relation.updated_at AS relation_updated_at
            FROM reachable
            JOIN kg_relations relation
              ON (
                    relation.head_entity_id = reachable.entity_id
                    OR relation.tail_entity_id = reachable.entity_id
                 )
             AND relation.status = 'usable'
            JOIN kg_entities head
              ON head.id = relation.head_entity_id
             AND head.status = 'usable'
            JOIN kg_entities tail
              ON tail.id = relation.tail_entity_id
             AND tail.status = 'usable'
            WHERE reachable.depth < %(hops)s
              AND (
                    cardinality(%(entity_types)s::text[]) = 0
                    OR head.entity_type = ANY(%(entity_types)s::text[])
                    OR tail.entity_type = ANY(%(entity_types)s::text[])
                  )
              AND (
                    cardinality(%(relation_types)s::text[]) = 0
                    OR relation.relation_type = ANY(%(relation_types)s::text[])
                  )
        ),
        limited_edges AS (
            SELECT *
            FROM graph_edges
            ORDER BY relation_updated_at DESC, relation_id ASC
            LIMIT %(limit)s
        )
        SELECT
            center.id AS center_id,
            center.name AS center_name,
            center.entity_type AS center_entity_type,
            center.description AS center_description,
            center.status AS center_status,
            center.confidence AS center_confidence,
            limited_edges.relation_id,
            limited_edges.relation_type,
            limited_edges.relation_description,
            limited_edges.relation_confidence,
            limited_edges.relation_status,
            limited_edges.head_entity_id,
            limited_edges.head_entity_name,
            limited_edges.head_entity_type,
            limited_edges.head_entity_description,
            limited_edges.head_entity_status,
            limited_edges.head_entity_confidence,
            limited_edges.tail_entity_id,
            limited_edges.tail_entity_name,
            limited_edges.tail_entity_type,
            limited_edges.tail_entity_description,
            limited_edges.tail_entity_status,
            limited_edges.tail_entity_confidence,
            limited_edges.evidence_count
        FROM center
        LEFT JOIN limited_edges ON true
        ORDER BY limited_edges.relation_updated_at DESC NULLS LAST,
                 limited_edges.relation_id ASC
        """
