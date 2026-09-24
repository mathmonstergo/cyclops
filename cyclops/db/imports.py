from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime
from typing import Any

from cyclops.config import DOCUMENT_CHUNKER_TYPES
from cyclops.db.builders import (
    clean_block_list,
    clean_dict,
    clean_int,
    clean_list,
    compute_knowledge_chunk_hash,
    count_job_item_statuses,
    empty_import_file_embedding_summary,
    expected_document_knowledge_count,
)


def _document_chunk_embedding_status(
    row: dict[str, Any],
    expected_knowledge_count: int,
) -> str:
    """按真实行计数与唯一预期数判定切片向量状态。"""
    knowledge_count = int(row.get("knowledge_count") or 0)
    ready_count = int(row.get("ready_count") or 0)
    stale_count = int(row.get("stale_count") or 0)
    failed_count = int(row.get("failed_count") or 0)
    if knowledge_count == 0:
        return "pending"
    if stale_count > 0:
        return "stale"
    if failed_count > 0 and ready_count == 0:
        return "failed"
    if knowledge_count == expected_knowledge_count and ready_count == knowledge_count:
        return "ready"
    if ready_count == 0:
        return "pending"
    return "partial"


def _finalize_import_file_embedding_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """完成文件级向量摘要，关键约束是缺失 child 计入 pending。"""
    total = int(summary["total_chunks"])
    ready = int(summary["ready_count"])
    stale = int(summary["stale_count"])
    failed = int(summary["failed_count"])
    if total == 0:
        status = "none"
    elif stale > 0:
        status = "stale"
    elif failed > 0 and ready == 0:
        status = "failed"
    elif ready >= total:
        status = "ready"
    elif ready == 0:
        status = "pending"
    else:
        status = "partial"
    return {**summary, "status": status}


class ImportMixin:
    """维护导入文件、持久解析任务、切片、候选与生成任务。"""

    def create_import_file(self, row: dict[str, Any]) -> dict[str, Any]:
        """创建导入文件记录，保存原件路径和格式识别结果。"""
        sql = """
        INSERT INTO import_files (
            id, original_name, stored_path, file_type, parser, chunker_type, status,
            message_count, chunk_count, candidate_count, error
        )
        VALUES (
            %(id)s, %(original_name)s, %(stored_path)s, %(file_type)s, %(parser)s,
            %(chunker_type)s,
            %(status)s, %(message_count)s, %(chunk_count)s, %(candidate_count)s, %(error)s
        )
        RETURNING *
        """
        payload = {
            "chunker_type": "naive",
            "message_count": 0,
            "chunk_count": 0,
            "candidate_count": 0,
            "error": None,
            **row,
        }
        with self.connect() as conn:
            return conn.execute(sql, payload).fetchone()

    def create_import_parse_job(
        self,
        file_id: str,
        *,
        chunker_type: str,
        input_fingerprint: str,
    ) -> dict[str, Any]:
        """锁定文件并创建唯一 queued 任务，任何活跃任务都明确冲突。"""
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("file_id is required")
        if not isinstance(chunker_type, str) or chunker_type not in DOCUMENT_CHUNKER_TYPES:
            allowed = ", ".join(sorted(DOCUMENT_CHUNKER_TYPES))
            raise ValueError(f"chunker_type must be one of: {allowed}")
        if not isinstance(input_fingerprint, str) or not input_fingerprint.strip():
            raise ValueError("input_fingerprint is required")

        job_id = f"parse_job_{uuid.uuid4().hex[:12]}"
        lock_file_sql = """
        SELECT imp.*
        FROM import_files imp
        WHERE imp.id = %(file_id)s
        FOR UPDATE OF imp
        """
        active_job_sql = """
        SELECT job.id
        FROM import_parse_jobs AS job
        WHERE job.file_id = %(file_id)s
          AND job.status IN ('queued', 'submitting', 'polling', 'finalizing')
        ORDER BY job.created_at ASC, job.id ASC
        LIMIT 1
        """
        insert_job_sql = """
        INSERT INTO import_parse_jobs (
            id, file_id, status, chunker_type, input_fingerprint,
            provider_batch_id, provider_file_name, progress, error,
            lease_token, lease_expires_at, next_poll_at
        )
        VALUES (
            %(id)s, %(file_id)s, %(status)s, %(chunker_type)s, %(input_fingerprint)s,
            NULL, NULL, %(progress)s::jsonb, NULL,
            NULL, NULL, now()
        )
        RETURNING *
        """
        update_file_sql = """
        UPDATE import_files
        SET status = %(status)s,
            chunker_type = %(chunker_type)s,
            error = NULL,
            updated_at = now()
        WHERE id = %(file_id)s
        RETURNING *
        """
        with self.connect() as conn:
            import_file = conn.execute(
                lock_file_sql,
                {"file_id": file_id},
            ).fetchone()
            if import_file is None:
                raise KeyError(f"Import file not found: {file_id}")
            active_job = conn.execute(
                active_job_sql,
                {"file_id": file_id},
            ).fetchone()
            if active_job is not None:
                raise ValueError(f"active import parse job already exists: {active_job['id']}")
            job = conn.execute(
                insert_job_sql,
                {
                    "id": job_id,
                    "file_id": file_id,
                    "status": "queued",
                    "chunker_type": chunker_type,
                    "input_fingerprint": input_fingerprint,
                    "progress": json.dumps({}, ensure_ascii=False),
                },
            ).fetchone()
            if job is None:
                raise RuntimeError("import parse job insert returned no row")
            updated_file = conn.execute(
                update_file_sql,
                {
                    "file_id": file_id,
                    "status": "processing",
                    "chunker_type": chunker_type,
                },
            ).fetchone()
            if updated_file is None:
                raise KeyError(f"Import file not found after lock: {file_id}")
        return self._validate_import_parse_job_row(job)

    def get_import_parse_job(self, job_id: str) -> dict[str, Any] | None:
        """按 current 任务 ID 读取持久解析状态，不补齐缺失 progress。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id is required")
        sql = """
        SELECT *
        FROM import_parse_jobs
        WHERE id = %(job_id)s
        """
        with self.connect() as conn:
            row = conn.execute(sql, {"job_id": job_id}).fetchone()
        if row is None:
            return None
        return self._validate_import_parse_job_row(row)

    def get_latest_import_parse_job_for_file(
        self,
        file_id: str,
    ) -> dict[str, Any] | None:
        """按稳定时间顺序读取文件最新解析任务，供列表与 Drawer 共用。"""
        if not isinstance(file_id, str) or not file_id.strip():
            raise ValueError("file_id is required")
        sql = """
        SELECT *
        FROM import_parse_jobs
        WHERE file_id = %(file_id)s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
        with self.connect() as conn:
            row = conn.execute(sql, {"file_id": file_id}).fetchone()
        if row is None:
            return None
        return self._validate_import_parse_job_row(row)

    def list_latest_import_parse_jobs_for_files(
        self,
        file_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """批量读取每个文件最新解析任务，避免列表为每行单独查询。"""
        unique_ids = list(dict.fromkeys(file_id for file_id in file_ids if file_id))
        if not unique_ids:
            return {}
        sql = """
        SELECT DISTINCT ON (file_id) *
        FROM import_parse_jobs
        WHERE file_id = ANY(%(file_ids)s::text[])
        ORDER BY file_id ASC, created_at DESC, id DESC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, {"file_ids": unique_ids}).fetchall()
        return {
            row["file_id"]: self._validate_import_parse_job_row(row)
            for row in rows
        }

    def claim_import_parse_job(
        self,
        *,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        """原子领取到期任务；首次领取转 submitting，恢复领取保持原阶段。"""
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        lease_token = secrets.token_urlsafe(32)
        with self.connect() as conn:
            row = conn.execute(
                self._claim_import_parse_job_sql(),
                {
                    "lease_token": lease_token,
                    "lease_seconds": lease_seconds,
                },
            ).fetchone()
        if row is None:
            return None
        return self._validate_import_parse_job_row(row)

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
        """持有当前 lease 的 worker 进入或保持 polling，写入后释放 lease。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id is required")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token is required")
        if status != "polling":
            raise ValueError("invalid import parse job progress transition target")
        if not isinstance(progress, dict):
            raise TypeError("progress must be a JSON object")
        if not isinstance(provider_batch_id, str) or not provider_batch_id.strip():
            raise ValueError("provider_batch_id is required for polling")
        if not isinstance(provider_file_name, str) or not provider_file_name.strip():
            raise ValueError("provider_file_name is required for polling")
        if (
            not isinstance(next_poll_at, datetime)
            or next_poll_at.tzinfo is None
            or next_poll_at.utcoffset() is None
        ):
            raise ValueError("next_poll_at must be a timezone-aware datetime")

        update_sql = """
        UPDATE import_parse_jobs AS job
        SET status = %(status)s,
            provider_batch_id = %(provider_batch_id)s,
            provider_file_name = %(provider_file_name)s,
            progress = %(progress)s::jsonb,
            next_poll_at = %(next_poll_at)s,
            lease_token = NULL,
            lease_expires_at = NULL,
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
          AND job.status = %(current_status)s
        RETURNING job.*
        """
        with self.connect() as conn:
            current = conn.execute(
                self._lock_import_parse_job_sql(),
                {"job_id": job_id},
            ).fetchone()
            if current is None:
                raise KeyError(f"Import parse job not found: {job_id}")
            if current["lease_token"] != lease_token:
                raise ValueError("import parse job lease token changed")
            current_status = current["status"]
            if current_status not in {"submitting", "polling"}:
                raise ValueError(
                    "invalid import parse job progress transition: "
                    f"{current_status} -> {status}"
                )
            row = conn.execute(
                update_sql,
                {
                    "job_id": job_id,
                    "lease_token": lease_token,
                    "current_status": current_status,
                    "status": status,
                    "progress": json.dumps(progress, ensure_ascii=False),
                    "provider_batch_id": provider_batch_id,
                    "provider_file_name": provider_file_name,
                    "next_poll_at": next_poll_at,
                },
            ).fetchone()
            if row is None:
                raise ValueError("import parse job lease expired or status changed")
        return self._validate_import_parse_job_row(row)

    def renew_import_parse_job_lease(
        self,
        job_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
    ) -> bool:
        """仅延长当前未过期活跃任务的 lease，不改变业务阶段或进度。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id is required")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token is required")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")

        sql = """
        UPDATE import_parse_jobs AS job
        SET lease_expires_at = now() + make_interval(secs => %(lease_seconds)s)
        WHERE job.id = %(job_id)s
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
          AND job.status IN ('submitting', 'polling', 'finalizing')
        RETURNING job.id
        """
        with self.connect() as conn:
            row = conn.execute(
                sql,
                {
                    "job_id": job_id,
                    "lease_token": lease_token,
                    "lease_seconds": lease_seconds,
                },
            ).fetchone()
        return row is not None

    def begin_import_parse_job_finalization(
        self,
        job_id: str,
        *,
        lease_token: str,
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        """显式进入 finalizing 并保留当前 lease，供下载结果后原子提交。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id is required")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token is required")
        if not isinstance(progress, dict):
            raise TypeError("progress must be a JSON object")

        update_sql = """
        UPDATE import_parse_jobs AS job
        SET status = 'finalizing',
            progress = %(progress)s::jsonb,
            next_poll_at = now(),
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
          AND job.status = %(current_status)s
        RETURNING job.*
        """
        with self.connect() as conn:
            current = conn.execute(
                self._lock_import_parse_job_sql(),
                {"job_id": job_id},
            ).fetchone()
            if current is None:
                raise KeyError(f"Import parse job not found: {job_id}")
            if current["lease_token"] != lease_token:
                raise ValueError("import parse job lease token changed")
            current_status = current["status"]
            if current_status not in {"polling", "finalizing"}:
                raise ValueError(
                    "invalid import parse job finalizing transition: "
                    f"{current_status} -> finalizing"
                )
            row = conn.execute(
                update_sql,
                {
                    "job_id": job_id,
                    "lease_token": lease_token,
                    "current_status": current_status,
                    "progress": json.dumps(progress, ensure_ascii=False),
                },
            ).fetchone()
            if row is None:
                raise ValueError("import parse job lease expired or status changed")
        return self._validate_import_parse_job_row(row)

    def fail_import_parse_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        error: str,
    ) -> dict[str, Any]:
        """校验当前 lease 后，同事务将解析任务与来源文件标记为 failed。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id is required")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token is required")
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error is required")

        lock_file_sql = """
        SELECT imp.*
        FROM import_files AS imp
        WHERE imp.id = %(file_id)s
        FOR UPDATE OF imp
        """
        update_job_sql = """
        UPDATE import_parse_jobs AS job
        SET status = 'failed',
            error = %(error)s,
            lease_token = NULL,
            lease_expires_at = NULL,
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
          AND job.status = %(current_status)s
        RETURNING job.*
        """
        update_file_sql = """
        UPDATE import_files AS imp
        SET status = 'failed',
            error = %(error)s,
            updated_at = now()
        WHERE imp.id = %(file_id)s
        RETURNING imp.*
        """
        with self.connect() as conn:
            preview = conn.execute(
                self._get_import_parse_job_for_fence_sql(),
                {"job_id": job_id},
            ).fetchone()
            if preview is None:
                raise KeyError(f"Import parse job not found: {job_id}")
            self._validate_import_parse_job_lease_row(
                preview,
                lease_token=lease_token,
                allowed_statuses={"submitting", "polling", "finalizing"},
                operation="fail",
            )
            import_file = conn.execute(
                lock_file_sql,
                {"file_id": preview["file_id"]},
            ).fetchone()
            if import_file is None:
                raise KeyError(f"Import parse job or file not found: {job_id}")
            current = conn.execute(
                self._lock_import_parse_job_sql(),
                {"job_id": job_id},
            ).fetchone()
            if current is None:
                raise KeyError(f"Import parse job not found: {job_id}")
            current_status = self._validate_import_parse_job_lease_row(
                current,
                lease_token=lease_token,
                allowed_statuses={"submitting", "polling", "finalizing"},
                operation="fail",
            )
            if current["file_id"] != import_file["id"]:
                raise RuntimeError("import parse job file changed while locked")
            row = conn.execute(
                update_job_sql,
                {
                    "job_id": job_id,
                    "lease_token": lease_token,
                    "current_status": current_status,
                    "error": error,
                },
            ).fetchone()
            if row is None:
                raise ValueError("import parse job lease expired or status changed")
            updated_file = conn.execute(
                update_file_sql,
                {"file_id": current["file_id"], "error": error},
            ).fetchone()
            if updated_file is None:
                raise KeyError(f"Import file not found after lock: {current['file_id']}")
        return self._validate_import_parse_job_row(row)

    def complete_import_parse_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        input_fingerprint: str,
        chunks: list[dict[str, Any]],
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        """原子替换整份切片并完成任务，任何失败都回滚来源与任务终态。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id is required")
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token is required")
        if not isinstance(input_fingerprint, str) or not input_fingerprint.strip():
            raise ValueError("input_fingerprint is required")
        if not isinstance(chunks, list):
            raise TypeError("chunks must be a list")
        if not isinstance(progress, dict):
            raise TypeError("progress must be a JSON object")

        update_file_sql = """
        UPDATE import_files AS imp
        SET status = %(status)s,
            message_count = %(message_count)s,
            chunk_count = %(chunk_count)s,
            candidate_count = %(candidate_count)s,
            error = %(error)s,
            updated_at = now()
        WHERE imp.id = %(file_id)s
        RETURNING imp.*
        """
        complete_job_sql = """
        UPDATE import_parse_jobs AS job
        SET status = 'completed',
            progress = %(progress)s::jsonb,
            error = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            next_poll_at = now(),
            updated_at = now()
        WHERE job.id = %(job_id)s
          AND job.lease_token = %(lease_token)s
          AND job.lease_expires_at > now()
          AND job.status = %(current_status)s
          AND job.input_fingerprint = %(input_fingerprint)s
        RETURNING job.*
        """
        with self.connect() as conn:
            preview = conn.execute(
                self._get_import_parse_job_for_fence_sql(),
                {"job_id": job_id},
            ).fetchone()
            if preview is None:
                raise KeyError(f"Import parse job not found: {job_id}")
            self._validate_import_parse_job_lease_row(
                preview,
                lease_token=lease_token,
                allowed_statuses={"submitting", "finalizing"},
                operation="complete",
                input_fingerprint=input_fingerprint,
            )
            file_id = preview["file_id"]
            if any(chunk.get("file_id") != file_id for chunk in chunks):
                raise ValueError("import parse job chunk file_id changed")

            self._lock_kg_document_file_and_chunks_in_conn(conn, file_id)
            current = conn.execute(
                self._lock_import_parse_job_sql(),
                {"job_id": job_id},
            ).fetchone()
            if current is None:
                raise KeyError(f"Import parse job not found: {job_id}")
            current_status = self._validate_import_parse_job_lease_row(
                current,
                lease_token=lease_token,
                allowed_statuses={"submitting", "finalizing"},
                operation="complete",
                input_fingerprint=input_fingerprint,
            )
            if current["file_id"] != file_id:
                raise RuntimeError("import parse job file changed while locked")
            self._reconcile_kg_source_change_in_conn(
                conn,
                source_type="document",
                source_ids=[file_id],
                delete_evidence=True,
            )
            conn.execute(
                """
                DELETE FROM knowledge_chunks
                WHERE source_type = 'document'
                  AND source_id = %(file_id)s
                """,
                {"file_id": file_id},
            )
            conn.execute(
                "DELETE FROM import_chunks WHERE file_id = %(file_id)s",
                {"file_id": file_id},
            )
            inserted_rows = []
            for chunk in chunks:
                inserted = conn.execute(
                    self._insert_import_chunk_sql(),
                    self._import_chunk_payload(chunk),
                ).fetchone()
                if inserted is None:
                    raise RuntimeError("import chunk insert returned no row")
                inserted_rows.append(inserted)

            updated_file = conn.execute(
                update_file_sql,
                {
                    "file_id": file_id,
                    "status": "needs_review",
                    "message_count": sum(
                        int(chunk.get("message_count") or 0) for chunk in chunks
                    ),
                    "chunk_count": len(inserted_rows),
                    "candidate_count": 0,
                    "error": None,
                },
            ).fetchone()
            if updated_file is None:
                raise KeyError(f"Import file not found after lock: {file_id}")
            row = conn.execute(
                complete_job_sql,
                {
                    "job_id": job_id,
                    "lease_token": lease_token,
                    "current_status": current_status,
                    "input_fingerprint": input_fingerprint,
                    "progress": json.dumps(progress, ensure_ascii=False),
                },
            ).fetchone()
            if row is None:
                raise ValueError("import parse job lease expired or status changed")
        return self._validate_import_parse_job_row(row)

    @staticmethod
    def _validate_import_parse_job_row(row: dict[str, Any]) -> dict[str, Any]:
        """校验数据库 progress 的唯一对象形状，缺字段或标量必须显式失败。"""
        if not isinstance(row["progress"], dict):
            raise TypeError("progress must be a JSON object")
        return row

    @staticmethod
    def _get_import_parse_job_for_fence_sql() -> str:
        """无锁读取任务供快速 fence；任何写入前必须按来源锁序再次锁定复核。"""
        return """
        SELECT job.*
        FROM import_parse_jobs AS job
        WHERE job.id = %(job_id)s
        """

    @staticmethod
    def _validate_import_parse_job_lease_row(
        row: dict[str, Any],
        *,
        lease_token: str,
        allowed_statuses: set[str],
        operation: str,
        input_fingerprint: str | None = None,
    ) -> str:
        """校验已读任务的 token/阶段/指纹；锁前快检与锁后二次校验共用。"""
        if row["lease_token"] != lease_token:
            raise ValueError("import parse job lease token changed")
        status = row["status"]
        if status not in allowed_statuses:
            raise ValueError(f"import parse job cannot {operation} from status: {status}")
        if (
            input_fingerprint is not None
            and row["input_fingerprint"] != input_fingerprint
        ):
            raise ValueError("import parse job input fingerprint changed")
        return status

    @staticmethod
    def _lock_import_parse_job_sql() -> str:
        """锁定单个解析任务，供 lease-fenced writer 校验当前阶段。"""
        return """
        SELECT job.*
        FROM import_parse_jobs AS job
        WHERE job.id = %(job_id)s
        FOR UPDATE OF job
        """

    @staticmethod
    def _claim_import_parse_job_sql() -> str:
        """使用 SKIP LOCKED 领取 due/过期任务，首次领取原子进入 submitting。"""
        return """
        WITH candidate AS (
            SELECT job.id
            FROM import_parse_jobs AS job
            WHERE job.status IN ('queued', 'submitting', 'polling', 'finalizing')
              AND job.next_poll_at <= now()
              AND (
                    (job.lease_token IS NULL AND job.lease_expires_at IS NULL)
                    OR job.lease_expires_at <= now()
                  )
            ORDER BY next_poll_at ASC, created_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE import_parse_jobs AS job
        SET status = CASE
                WHEN job.status = 'queued' THEN 'submitting'
                ELSE job.status
            END,
            lease_token = %(lease_token)s,
            lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
            updated_at = now()
        FROM candidate
        WHERE job.id = candidate.id
        RETURNING job.*
        """

    def update_import_file_summary(self, file_id: str, **fields: Any) -> dict[str, Any]:
        """更新导入文件业务摘要，只允许当前文件字段。"""
        allowed = {
            "status",
            "message_count",
            "chunk_count",
            "candidate_count",
            "error",
            "chunker_type",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_import_file(file_id)
        assignments = ", ".join(f"{key} = %({key})s" for key in updates)
        sql = f"""
        UPDATE import_files
        SET {assignments}, updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            row = conn.execute(sql, {"id": file_id, **updates}).fetchone()
        if row is None:
            raise KeyError(f"Import file not found: {file_id}")
        return row

    def get_import_file(self, file_id: str) -> dict[str, Any] | None:
        """按 id 获取导入文件记录。"""
        sql = "SELECT * FROM import_files WHERE id = %(id)s"
        with self.connect() as conn:
            return conn.execute(sql, {"id": file_id}).fetchone()

    def delete_import_file(self, file_id: str) -> dict[str, Any] | None:
        """删除导入文件记录，依赖外键级联清理切块和候选 FAQ。

        knowledge_chunks 不在外键级联范围内（统一知识表），需手工清除文档来源的向量行，
        避免文件删除后残留 stale embedding 继续被检索命中。

        返回值带 `_deleted_chunk_count` / `_deleted_vector_count`，反映实际触发的 DB 事件量；
        前端据此条件性提示（没切片就不提示切片清理，没向量就不提示向量清理），避免假动作提示。
        """
        count_chunks_sql = (
            "SELECT count(*) AS c FROM import_chunks WHERE file_id = %(id)s"
        )
        lock_parse_jobs_sql = """
        SELECT job.id
        FROM import_parse_jobs AS job
        WHERE job.file_id = %(id)s
        ORDER BY job.id ASC
        FOR UPDATE OF job
        """
        delete_knowledge_sql = (
            "DELETE FROM knowledge_chunks "
            "WHERE source_type = 'document' AND source_id = %(id)s "
            "RETURNING id"
        )
        delete_file_sql = "DELETE FROM import_files WHERE id = %(id)s RETURNING *"
        with self.connect() as conn:
            self._lock_kg_document_file_and_chunks_in_conn(conn, file_id)
            conn.execute(lock_parse_jobs_sql, {"id": file_id}).fetchall()
            self._reconcile_kg_source_change_in_conn(
                conn,
                source_type="document",
                source_ids=[file_id],
                delete_evidence=True,
            )
            chunk_count = int(
                conn.execute(count_chunks_sql, {"id": file_id}).fetchone()["c"]
            )
            deleted_vectors = conn.execute(
                delete_knowledge_sql, {"id": file_id}
            ).fetchall()
            record = conn.execute(delete_file_sql, {"id": file_id}).fetchone()
        if record is None:
            return None
        record["_deleted_chunk_count"] = chunk_count
        record["_deleted_vector_count"] = len(deleted_vectors)
        return record

    def set_import_file_disabled(
        self, file_id: str, is_disabled: bool
    ) -> dict[str, Any] | None:
        """切换文件禁用标记；仅在值真实变化时双向重算该文件来源 KG。"""
        lock_sql = """
        SELECT *
        FROM import_files imp
        WHERE imp.id = %(id)s
        FOR UPDATE OF imp
        """
        sql = """
        UPDATE import_files
        SET is_disabled = %(is_disabled)s,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            requested_disabled = bool(is_disabled)
            existing = conn.execute(lock_sql, {"id": file_id}).fetchone()
            if existing is None:
                return None
            if bool(existing.get("is_disabled")) == requested_disabled:
                return existing
            row = conn.execute(
                sql, {"id": file_id, "is_disabled": requested_disabled}
            ).fetchone()
            if row is None:
                raise KeyError(f"Import file not found after lock: {file_id}")
            self._reconcile_kg_source_change_in_conn(
                conn,
                source_type="document",
                source_ids=[file_id],
            )
            return row

    def set_import_chunk_disabled(
        self, chunk_id: str, is_disabled: bool
    ) -> dict[str, Any] | None:
        """切换切片禁用标记；仅在值真实变化时双向重算精确切片来源 KG。"""
        lock_sql = """
        SELECT *
        FROM import_chunks chunk
        WHERE chunk.id = %(id)s
        FOR UPDATE OF chunk
        """
        sql = """
        UPDATE import_chunks
        SET is_disabled = %(is_disabled)s,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            requested_disabled = bool(is_disabled)
            existing = conn.execute(lock_sql, {"id": chunk_id}).fetchone()
            if existing is None:
                return None
            if bool(existing.get("is_disabled")) == requested_disabled:
                return existing
            row = conn.execute(
                sql, {"id": chunk_id, "is_disabled": requested_disabled}
            ).fetchone()
            if row is None:
                raise KeyError(f"Import chunk not found after lock: {chunk_id}")
            self._reconcile_kg_source_change_in_conn(
                conn,
                source_type="document",
                source_ids=[row["file_id"]],
                source_chunk_ids=[chunk_id],
            )
            return row

    def set_import_chunk_questions(
        self,
        chunk_id: str,
        questions: list[str],
        *,
        model: str | None,
        status: str = "ready",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        """更新假设问题，关键约束是问题正文变化时同事务标记全部知识行 stale。"""
        normalized_questions = list(questions or [])
        payload = {
            "id": chunk_id,
            "questions": json.dumps(normalized_questions, ensure_ascii=False),
            "status": status,
            "model": model,
            "error": error,
        }
        with self.connect() as conn:
            existing = conn.execute(
                self._lock_import_chunk_for_questions_update_sql(),
                {"id": chunk_id},
            ).fetchone()
            if existing is None:
                return None
            row = conn.execute(self._update_import_chunk_questions_sql(), payload).fetchone()
            if row is None:
                return None
            if list(existing.get("questions") or []) != normalized_questions:
                conn.execute(
                    self._mark_document_chunk_questions_stale_sql(),
                    {
                        "source_id": existing["file_id"],
                        "source_chunk_id": chunk_id,
                    },
                )
            return row

    def list_import_files(
        self,
        *,
        query: str = "",
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """列出导入文件，并返回状态计数供侧栏筛选。"""
        clauses = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if query:
            params["query"] = f"%{query}%"
            clauses.append("original_name ILIKE %(query)s")
        if status:
            params["status"] = status
            clauses.append("status = %(status)s")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows_sql = f"""
        SELECT *
        FROM import_files
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        count_sql = f"SELECT count(*) AS total FROM import_files {where}"
        status_sql = "SELECT status, count(*) AS count FROM import_files GROUP BY status"
        with self.connect() as conn:
            rows = conn.execute(rows_sql, params).fetchall()
            total = conn.execute(count_sql, params).fetchone()["total"]
            status_counts = conn.execute(status_sql).fetchall()
        file_ids = [row["id"] for row in rows]
        summaries = self.list_import_file_embedding_summaries(file_ids)
        parse_jobs = self.list_latest_import_parse_jobs_for_files(file_ids)
        return {
            "items": [
                {
                    **row,
                    "embedding_summary": summaries.get(row["id"], empty_import_file_embedding_summary()),
                    "parse_job": parse_jobs.get(row["id"]),
                }
                for row in rows
            ],
            "total": total,
            "status_counts": {row["status"]: row["count"] for row in status_counts},
        }

    def list_import_file_embedding_summaries(self, file_ids: list[str]) -> dict[str, dict[str, Any]]:
        """批量统计文档向量状态，child 预期数与生成链路共用算法。"""
        unique_ids = list(dict.fromkeys(file_ids))
        if not unique_ids:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                self._import_file_embedding_summaries_sql(),
                {"file_ids": unique_ids},
            ).fetchall()
        summaries = {
            file_id: empty_import_file_embedding_summary() for file_id in unique_ids
        }
        for row in rows:
            if row.get("chunk_id") is None:
                continue
            summary = summaries[row["file_id"]]
            expected_count = expected_document_knowledge_count(row)
            knowledge_count = int(row.get("knowledge_count") or 0)
            missing_count = max(expected_count - knowledge_count, 0)
            summary["total_chunks"] += max(expected_count, knowledge_count)
            summary["knowledge_count"] += knowledge_count
            summary["ready_count"] += int(row.get("ready_count") or 0)
            summary["stale_count"] += int(row.get("stale_count") or 0)
            summary["failed_count"] += int(row.get("failed_count") or 0)
            summary["pending_count"] += (
                int(row.get("pending_count") or 0) + missing_count
            )
            summary["missing_count"] += missing_count
        return {
            file_id: _finalize_import_file_embedding_summary(summary)
            for file_id, summary in summaries.items()
        }

    def get_import_file_embedding_summary(self, file_id: str) -> dict[str, Any]:
        """获取单个文档的切片向量摘要，供详情抽屉保存后刷新。"""
        return self.list_import_file_embedding_summaries([file_id]).get(
            file_id,
            empty_import_file_embedding_summary(),
        )

    def replace_import_chunks(
        self,
        file_id: str,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """替换文件切块，关键约束是先回退旧 KG 再清理旧知识单元和切片。"""
        with self.connect() as conn:
            self._lock_kg_document_file_and_chunks_in_conn(conn, file_id)
            self._reconcile_kg_source_change_in_conn(
                conn,
                source_type="document",
                source_ids=[file_id],
                delete_evidence=True,
            )
            conn.execute(
                """
                DELETE FROM knowledge_chunks
                WHERE source_type = 'document'
                  AND source_id = %(file_id)s
                """,
                {"file_id": file_id},
            )
            conn.execute("DELETE FROM import_chunks WHERE file_id = %(file_id)s", {"file_id": file_id})
            rows = []
            for chunk in chunks:
                rows.append(conn.execute(self._insert_import_chunk_sql(), self._import_chunk_payload(chunk)).fetchone())
        return rows

    def list_import_chunks(self, file_id: str) -> list[dict[str, Any]]:
        """按文件列出切片，并联表 knowledge_chunks 派生每片真实 embedding_status。

        import_chunks 本身不存 embedding 状态（向量只落在统一知识表 knowledge_chunks），
        因此切片级状态要按"该片的 parent + child 知识单元"聚合得到，规则与文件级摘要保持一致：
        无向量=未索引(pending)、任一 stale=过期、全失败=失败、全就绪=已索引、其余=部分(partial)。
        """
        with self.connect() as conn:
            rows = conn.execute(
                self._list_import_chunks_sql(), {"file_id": file_id}
            ).fetchall()
        result = []
        for row in rows:
            expected_count = expected_document_knowledge_count(row)
            result.append(
                {
                    **row,
                    "expected_knowledge_count": expected_count,
                    "embedding_status": _document_chunk_embedding_status(
                        row,
                        expected_count,
                    ),
                }
            )
        return result

    @staticmethod
    def _list_import_chunks_sql() -> str:
        """读取切片向量事实计数，不在 SQL 中复制 child 生成算法。"""
        return """
        WITH chunk_knowledge AS (
            SELECT
                ic.id AS chunk_id,
                count(kc.id) AS knowledge_count,
                count(kc.id) FILTER (WHERE kc.embedding_status = 'ready') AS ready_count,
                count(kc.id) FILTER (WHERE kc.embedding_status = 'stale') AS stale_count,
                count(kc.id) FILTER (WHERE kc.embedding_status = 'failed') AS failed_count,
                count(kc.id) FILTER (WHERE kc.embedding_status = 'pending') AS pending_count
            FROM import_chunks ic
            LEFT JOIN knowledge_chunks kc
              ON kc.source_type = 'document'
             AND kc.source_id = ic.file_id
             AND kc.source_chunk_id = ic.id
            WHERE ic.file_id = %(file_id)s
            GROUP BY ic.id
        )
        SELECT
            ic.*,
            COALESCE(ck.knowledge_count, 0)::int AS knowledge_count,
            COALESCE(ck.ready_count, 0)::int AS ready_count,
            COALESCE(ck.stale_count, 0)::int AS stale_count,
            COALESCE(ck.failed_count, 0)::int AS failed_count,
            COALESCE(ck.pending_count, 0)::int AS pending_count
        FROM import_chunks ic
        LEFT JOIN chunk_knowledge ck ON ck.chunk_id = ic.id
        WHERE ic.file_id = %(file_id)s
        ORDER BY ic.chunk_index ASC
        """

    def get_import_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """按 id 获取导入切块。"""
        sql = "SELECT * FROM import_chunks WHERE id = %(id)s"
        with self.connect() as conn:
            return conn.execute(sql, {"id": chunk_id}).fetchone()

    @staticmethod
    def _import_chunk_payload(chunk: dict[str, Any]) -> dict[str, Any]:
        """补齐导入切片结构化字段，统一 Markdown 与 MinerU 当前来源。"""
        return {
            **chunk,
            "section_path": json.dumps(clean_list(chunk.get("section_path")), ensure_ascii=False),
            "page_start": clean_int(chunk.get("page_start")),
            "page_end": clean_int(chunk.get("page_end")),
            "block_type": chunk.get("block_type"),
            "source_offsets": json.dumps(clean_dict(chunk.get("source_offsets")), ensure_ascii=False),
            "source_blocks": json.dumps(clean_block_list(chunk.get("source_blocks")), ensure_ascii=False),
            "children_delimiter": str(chunk.get("children_delimiter") or ""),
        }

    def update_import_chunk_text(self, chunk_id: str, source_text: str) -> dict[str, Any]:
        """保存变化后的切片原文；未变化时保持 KG 证据和向量状态不动。"""
        payload = {
            "id": chunk_id,
            "chunk_id": chunk_id,
            "source_text": source_text,
            "content_hash": compute_knowledge_chunk_hash({"embedding_text": source_text}),
        }
        with self.connect() as conn:
            existing = conn.execute(
                self._lock_import_chunk_for_text_update_sql(), {"id": chunk_id}
            ).fetchone()
            if existing is None:
                raise KeyError(f"Import chunk not found: {chunk_id}")
            if str(existing.get("source_text") or "") == source_text:
                return existing
            payload["source_id"] = existing["file_id"]
            row = conn.execute(self._update_import_chunk_text_sql(), payload).fetchone()
            if row is None:
                raise KeyError(f"Import chunk not found: {chunk_id}")
            self._reconcile_kg_source_change_in_conn(
                conn,
                source_type="document",
                source_ids=[row["file_id"]],
                source_chunk_ids=[chunk_id],
                delete_evidence=True,
            )
            conn.execute(self._delete_document_chunk_child_knowledge_sql(), payload)
            conn.execute(self._mark_document_chunk_knowledge_stale_sql(), payload)
        return row

    def create_import_candidates(
        self,
        chunk: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """保存某个切块的 AI 候选 FAQ，并更新切块候选数。"""
        with self.connect() as conn:
            rows = []
            for candidate in candidates:
                payload = {
                    "duplicate_level": "none",
                    "duplicate_score": 0,
                    "duplicate_target_id": None,
                    "duplicate_reason": None,
                    **candidate,
                }
                rows.append(conn.execute(self._insert_import_candidate_sql(), payload).fetchone())
            conn.execute(
                """
                UPDATE import_chunks
                SET status = 'generated',
                    candidate_count = %(candidate_count)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {"id": chunk["id"], "candidate_count": len(rows)},
            )
            conn.execute(
                """
                UPDATE import_files
                SET candidate_count = candidate_count + %(candidate_count)s,
                    updated_at = now()
                WHERE id = %(file_id)s
                """,
                {"file_id": chunk["file_id"], "candidate_count": len(rows)},
            )
        return rows

    def create_import_generation_job(self, chunk_ids: list[str]) -> dict[str, Any]:
        """创建候选生成任务，已处理或活跃中的切块会被标记为跳过。"""
        unique_chunk_ids = list(dict.fromkeys(chunk_ids))
        if not unique_chunk_ids:
            raise ValueError("chunk_ids is required")
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        with self.connect() as conn:
            chunks = conn.execute(
                """
                SELECT id, candidate_count, status
                FROM import_chunks
                WHERE id = ANY(%(ids)s::text[])
                """,
                {"ids": unique_chunk_ids},
            ).fetchall()
            chunks_by_id = {row["id"]: row for row in chunks}
            active_rows = conn.execute(
                """
                SELECT DISTINCT chunk_id
                FROM import_generation_job_items
                WHERE chunk_id = ANY(%(ids)s::text[])
                  AND status IN ('queued', 'processing')
                """,
                {"ids": unique_chunk_ids},
            ).fetchall()
            active_ids = {row["chunk_id"] for row in active_rows}
            items = []
            for chunk_id in unique_chunk_ids:
                chunk = chunks_by_id.get(chunk_id)
                status = "queued"
                reason = None
                if chunk is None:
                    status = "skipped"
                    reason = "missing_chunk"
                elif chunk["candidate_count"] > 0 or chunk["status"] == "generated":
                    status = "skipped"
                    reason = "already_generated"
                elif chunk_id in active_ids:
                    status = "skipped"
                    reason = "already_queued"
                items.append(
                    {
                        "id": f"job_item_{uuid.uuid4().hex[:12]}",
                        "job_id": job_id,
                        "chunk_id": chunk_id,
                        "status": status,
                        "reason": reason,
                        "candidate_count": 0,
                        "error": None,
                    }
                )
            counts = count_job_item_statuses(items)
            job = conn.execute(
                """
                INSERT INTO import_generation_jobs (
                    id, status, total_count, queued_count, processing_count,
                    generated_count, skipped_count, failed_count
                )
                VALUES (
                    %(id)s, %(status)s, %(total_count)s, %(queued_count)s, %(processing_count)s,
                    %(generated_count)s, %(skipped_count)s, %(failed_count)s
                )
                RETURNING *
                """,
                {
                    "id": job_id,
                    "status": "queued" if counts["queued_count"] else "completed",
                    "total_count": len(items),
                    **counts,
                },
            ).fetchone()
            inserted_items = [
                conn.execute(self._insert_import_generation_job_item_sql(), item).fetchone()
                for item in items
            ]
        return {**job, "items": inserted_items}

    def get_import_generation_job(self, job_id: str) -> dict[str, Any] | None:
        """按 id 获取候选生成任务。"""
        sql = "SELECT * FROM import_generation_jobs WHERE id = %(id)s"
        with self.connect() as conn:
            return conn.execute(sql, {"id": job_id}).fetchone()

    def list_import_generation_job_items(self, job_id: str) -> list[dict[str, Any]]:
        """列出候选生成任务的切块子项。"""
        sql = """
        SELECT *
        FROM import_generation_job_items
        WHERE job_id = %(job_id)s
        ORDER BY created_at ASC, id ASC
        """
        with self.connect() as conn:
            return conn.execute(sql, {"job_id": job_id}).fetchall()

    def update_import_generation_job_item(self, item_id: str, **fields: Any) -> dict[str, Any]:
        """更新候选生成任务子项状态和结果。"""
        allowed = {"status", "reason", "candidate_count", "error"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            raise ValueError("generation job item updates are required")
        assignments = ", ".join(f"{key} = %({key})s" for key in updates)
        sql = f"""
        UPDATE import_generation_job_items
        SET {assignments}, updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            row = conn.execute(sql, {"id": item_id, **updates}).fetchone()
        if row is None:
            raise KeyError(f"Import generation job item not found: {item_id}")
        return row

    def update_import_generation_job_summary(self, job_id: str, status: str) -> dict[str, Any]:
        """重新统计候选生成任务摘要并写入最终状态。"""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, count(*) AS count
                FROM import_generation_job_items
                WHERE job_id = %(job_id)s
                GROUP BY status
                """,
                {"job_id": job_id},
            ).fetchall()
            counts = {row["status"]: row["count"] for row in rows}
            job = conn.execute(
                """
                UPDATE import_generation_jobs
                SET status = %(status)s,
                    queued_count = %(queued_count)s,
                    processing_count = %(processing_count)s,
                    generated_count = %(generated_count)s,
                    skipped_count = %(skipped_count)s,
                    failed_count = %(failed_count)s,
                    updated_at = now()
                WHERE id = %(id)s
                RETURNING *
                """,
                {
                    "id": job_id,
                    "status": status,
                    "queued_count": counts.get("queued", 0),
                    "processing_count": counts.get("processing", 0),
                    "generated_count": counts.get("generated", 0),
                    "skipped_count": counts.get("skipped", 0),
                    "failed_count": counts.get("failed", 0),
                },
            ).fetchone()
        if job is None:
            raise KeyError(f"Import generation job not found: {job_id}")
        return job

    def list_import_candidates(self, chunk_id: str) -> list[dict[str, Any]]:
        """按切块列出候选 FAQ。"""
        sql = """
        SELECT *
        FROM import_candidates
        WHERE chunk_id = %(chunk_id)s
        ORDER BY created_at ASC, id ASC
        """
        with self.connect() as conn:
            return conn.execute(sql, {"chunk_id": chunk_id}).fetchall()

    def list_import_file_candidates(self, file_id: str) -> list[dict[str, Any]]:
        """按文件汇总候选 FAQ，并带上来源切块编号供审核列表定位。"""
        sql = """
        SELECT c.*, ch.chunk_index, ch.start_at, ch.end_at
        FROM import_candidates c
        JOIN import_chunks ch ON ch.id = c.chunk_id
        WHERE c.file_id = %(file_id)s
        ORDER BY c.updated_at DESC, c.created_at DESC, c.id ASC
        """
        with self.connect() as conn:
            return conn.execute(sql, {"file_id": file_id}).fetchall()

    def list_import_dedupe_references(self, chunk_id: str) -> list[dict[str, Any]]:
        """列出候选查重参考，包括正式 FAQ 和其它候选 FAQ。"""
        sql = """
        SELECT id, question, answer
        FROM faq_documents
        UNION ALL
        SELECT id, question, answer
        FROM import_candidates
        WHERE chunk_id <> %(chunk_id)s
          AND status IN ('pending', 'saved')
        """
        with self.connect() as conn:
            return conn.execute(sql, {"chunk_id": chunk_id}).fetchall()

    def get_import_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """获取候选 FAQ，并带上来源文件名用于保存 evidence。"""
        sql = """
        SELECT c.*, f.original_name AS file_name
        FROM import_candidates c
        JOIN import_files f ON f.id = c.file_id
        WHERE c.id = %(id)s
        """
        with self.connect() as conn:
            return conn.execute(sql, {"id": candidate_id}).fetchone()

    def update_import_candidate(self, candidate_id: str, row: dict[str, Any]) -> dict[str, Any]:
        """更新人工编辑后的候选 FAQ 内容。"""
        payload = {
            "id": candidate_id,
            "question": row["question"],
            "answer": row["answer"],
            "similar_questions": json.dumps(clean_list(row.get("similar_questions")), ensure_ascii=False),
            "category": row.get("category"),
            "tags": json.dumps(clean_list(row.get("tags")), ensure_ascii=False),
            "confidence": row.get("confidence", "medium"),
            "internal_note": row.get("internal_note"),
        }
        sql = """
        UPDATE import_candidates
        SET question = %(question)s,
            answer = %(answer)s,
            similar_questions = %(similar_questions)s::jsonb,
            category = %(category)s,
            tags = %(tags)s::jsonb,
            confidence = %(confidence)s,
            internal_note = %(internal_note)s,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            result = conn.execute(sql, payload).fetchone()
        if result is None:
            raise KeyError(f"Import candidate not found: {candidate_id}")
        return result

    def mark_import_candidate_saved(self, candidate_id: str, faq_id: str) -> dict[str, Any]:
        """标记候选 FAQ 已保存到标准问答。"""
        sql = """
        UPDATE import_candidates
        SET status = 'saved',
            saved_faq_id = %(faq_id)s,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            row = conn.execute(sql, {"id": candidate_id, "faq_id": faq_id}).fetchone()
        if row is None:
            raise KeyError(f"Import candidate not found: {candidate_id}")
        return row

    def mark_import_candidate_ignored(self, candidate_id: str) -> dict[str, Any]:
        """标记候选 FAQ 已忽略。"""
        sql = """
        UPDATE import_candidates
        SET status = 'ignored',
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            row = conn.execute(sql, {"id": candidate_id}).fetchone()
        if row is None:
            raise KeyError(f"Import candidate not found: {candidate_id}")
        return row

    @staticmethod
    def _insert_import_chunk_sql() -> str:
        """集中维护切块插入 SQL，避免多处字段漂移。"""
        return """
        INSERT INTO import_chunks (
            id, file_id, chunk_index,
            section_path, page_start, page_end, block_type, source_offsets, source_blocks,
            children_delimiter,
            start_at, end_at, message_count,
            keywords, source_text, status, candidate_count
        )
        VALUES (
            %(id)s, %(file_id)s, %(chunk_index)s,
            %(section_path)s::jsonb, %(page_start)s,
            %(page_end)s, %(block_type)s, %(source_offsets)s::jsonb, %(source_blocks)s::jsonb,
            %(children_delimiter)s,
            %(start_at)s, %(end_at)s,
            %(message_count)s, %(keywords)s::jsonb, %(source_text)s,
            %(status)s, %(candidate_count)s
        )
        RETURNING *
        """

    @staticmethod
    def _import_file_embedding_summaries_sql() -> str:
        """批量读取每个来源切片的向量计数，预期数留给 Python 唯一算法。"""
        return """
        WITH requested AS (
            SELECT unnest(%(file_ids)s::text[]) AS file_id
        ),
        chunk_rows AS (
            SELECT
                id AS chunk_id,
                file_id,
                source_text,
                source_blocks,
                children_delimiter
            FROM import_chunks
            WHERE file_id = ANY(%(file_ids)s::text[])
              AND COALESCE(is_disabled, false) = false
        )
        SELECT
            requested.file_id,
            chunk_rows.chunk_id,
            chunk_rows.source_text,
            chunk_rows.source_blocks,
            chunk_rows.children_delimiter,
            count(knowledge.id)::int AS knowledge_count,
            count(knowledge.id) FILTER (
                WHERE knowledge.embedding_status = 'ready'
            )::int AS ready_count,
            count(knowledge.id) FILTER (
                WHERE knowledge.embedding_status = 'stale'
            )::int AS stale_count,
            count(knowledge.id) FILTER (
                WHERE knowledge.embedding_status = 'failed'
            )::int AS failed_count,
            count(knowledge.id) FILTER (
                WHERE knowledge.embedding_status = 'pending'
            )::int AS pending_count
        FROM requested
        LEFT JOIN chunk_rows ON chunk_rows.file_id = requested.file_id
        LEFT JOIN knowledge_chunks knowledge
          ON knowledge.source_type = 'document'
         AND knowledge.source_id = requested.file_id
         AND knowledge.source_chunk_id = chunk_rows.chunk_id
        GROUP BY
            requested.file_id,
            chunk_rows.chunk_id,
            chunk_rows.source_text,
            chunk_rows.source_blocks,
            chunk_rows.children_delimiter
        ORDER BY requested.file_id, chunk_rows.chunk_id
        """

    @staticmethod
    def _lock_import_chunk_for_text_update_sql() -> str:
        """锁定待编辑切片并读取旧正文，避免并发保存时误判 no-op。"""
        return """
        SELECT *
        FROM import_chunks chunk
        WHERE chunk.id = %(id)s
        FOR UPDATE OF chunk
        """

    @staticmethod
    def _lock_import_chunk_for_questions_update_sql() -> str:
        """锁定假设问题来源切片，避免并发生成时漏掉知识行失效。"""
        return """
        SELECT *
        FROM import_chunks chunk
        WHERE chunk.id = %(id)s
        FOR UPDATE OF chunk
        """

    @staticmethod
    def _update_import_chunk_questions_sql() -> str:
        """集中维护假设问题及生成状态更新，保持单一写入契约。"""
        return """
        UPDATE import_chunks
        SET questions = %(questions)s::jsonb,
            questions_status = %(status)s,
            questions_model = %(model)s,
            questions_updated_at = now(),
            questions_error = %(error)s,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """

    @staticmethod
    def _mark_document_chunk_questions_stale_sql() -> str:
        """让问题正文已变化的 parent/child 失效，不重写旧正文或哈希。"""
        return """
        UPDATE knowledge_chunks
        SET embedding_status = 'stale',
            embedding_error = NULL,
            updated_at = now()
        WHERE source_type = 'document'
          AND source_id = %(source_id)s
          AND source_chunk_id = %(source_chunk_id)s
        """

    @staticmethod
    def _update_import_chunk_text_sql() -> str:
        """集中维护切片正文更新 SQL，手工编辑后清空旧解析块避免 child 过期。"""
        return """
        UPDATE import_chunks
        SET source_text = %(source_text)s,
            source_blocks = '[]'::jsonb,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """

    @staticmethod
    def _mark_document_chunk_knowledge_stale_sql() -> str:
        """集中维护文档 parent 知识单元过期标记 SQL，避免旧向量继续命中。"""
        return """
        UPDATE knowledge_chunks
        SET content = %(source_text)s,
            embedding_text = %(source_text)s,
            search_text = concat_ws(E'\n', source_title, tags::text, %(source_text)s::text),
            embedding_status = 'stale',
            embedding_error = NULL,
            content_hash = %(content_hash)s,
            updated_at = now()
        WHERE source_type = 'document'
          AND source_id = %(source_id)s
          AND source_chunk_id = %(chunk_id)s
        """

    @staticmethod
    def _delete_document_chunk_child_knowledge_sql() -> str:
        """按原始来源 ID 删除旧 child，禁止依赖 synthetic parent ID 推断。"""
        return """
        DELETE FROM knowledge_chunks
        WHERE source_type = 'document'
          AND source_id = %(source_id)s
          AND source_chunk_id = %(chunk_id)s
          AND chunk_level = 'child'
        """

    @staticmethod
    def _insert_import_candidate_sql() -> str:
        """集中维护候选 FAQ 插入 SQL，确保 API 和测试使用同一字段。"""
        return """
        INSERT INTO import_candidates (
            id, file_id, chunk_id, question, answer, similar_questions, category,
            tags, confidence, internal_note, source_excerpt, duplicate_level,
            duplicate_score, duplicate_target_id, duplicate_reason, status
        )
        VALUES (
            %(id)s, %(file_id)s, %(chunk_id)s, %(question)s, %(answer)s,
            %(similar_questions)s::jsonb, %(category)s, %(tags)s::jsonb,
            %(confidence)s, %(internal_note)s, %(source_excerpt)s, %(duplicate_level)s,
            %(duplicate_score)s, %(duplicate_target_id)s, %(duplicate_reason)s, %(status)s
        )
        RETURNING *
        """

    @staticmethod
    def _insert_import_generation_job_item_sql() -> str:
        """集中维护生成任务切块插入 SQL。"""
        return """
        INSERT INTO import_generation_job_items (
            id, job_id, chunk_id, status, reason, candidate_count, error
        )
        VALUES (
            %(id)s, %(job_id)s, %(chunk_id)s, %(status)s, %(reason)s,
            %(candidate_count)s, %(error)s
        )
        RETURNING *
        """
