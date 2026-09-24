from __future__ import annotations

import json
from typing import Any

from cyclops.db.builders import (
    build_embedding_text,
    build_faq_knowledge_chunk_row,
    clean_list,
    compute_content_hash,
    next_embedding_status,
)
from cyclops.db.models import format_vector


class FaqMixin:
    """FAQ 文档表读写：upsert / get / save_text / update_embedding / list / 状态批改。"""

    def upsert_faq(
        self,
        row: dict[str, Any],
        embedding: list[float],
        *,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> None:
        """写入带向量 FAQ；正文或 usable 门禁变化时精确重算来源 KG。"""
        embedding_text = row.get("embedding_text") or build_embedding_text(row)
        content_hash = compute_content_hash({**row, "embedding_text": embedding_text})
        payload = {
            "id": row["id"],
            "doc_type": row.get("doc_type", "faq_qa"),
            "source_file": row.get("source_file"),
            "source_group": row.get("source_group"),
            "source_date": row.get("source_date"),
            "category": row.get("category"),
            "question": row["question"],
            "question_variants": json.dumps(row.get("question_variants", []), ensure_ascii=False),
            "answer": row["answer"],
            "tags": json.dumps(row.get("tags", []), ensure_ascii=False),
            "evidence": json.dumps(row.get("evidence", []), ensure_ascii=False),
            "confidence": row["confidence"],
            "status": row["status"],
            "sensitivity": row.get("sensitivity"),
            "embedding_text": embedding_text,
            "embedding": format_vector(embedding),
            "embedding_status": "ready",
            "embedding_model": embedding_model,
            "embedding_dimensions": embedding_dimensions,
            "embedding_error": None,
            "content_hash": content_hash,
        }
        sql = """
        INSERT INTO faq_documents (
            id, doc_type, source_file, source_group, source_date, category,
            question, question_variants, answer, tags, evidence, confidence,
            status, sensitivity, embedding_text, embedding, embedding_status,
            embedding_model, embedding_dimensions, embedding_updated_at,
            embedding_error, content_hash
        )
        VALUES (
            %(id)s, %(doc_type)s, %(source_file)s, %(source_group)s, %(source_date)s, %(category)s,
            %(question)s, %(question_variants)s::jsonb, %(answer)s, %(tags)s::jsonb,
            %(evidence)s::jsonb, %(confidence)s, %(status)s, %(sensitivity)s,
            %(embedding_text)s, %(embedding)s::vector, %(embedding_status)s,
            %(embedding_model)s, %(embedding_dimensions)s, now(),
            %(embedding_error)s, %(content_hash)s
        )
        ON CONFLICT (id) DO UPDATE SET
            doc_type = EXCLUDED.doc_type,
            source_file = EXCLUDED.source_file,
            source_group = EXCLUDED.source_group,
            source_date = EXCLUDED.source_date,
            category = EXCLUDED.category,
            question = EXCLUDED.question,
            question_variants = EXCLUDED.question_variants,
            answer = EXCLUDED.answer,
            tags = EXCLUDED.tags,
            evidence = EXCLUDED.evidence,
            confidence = EXCLUDED.confidence,
            status = EXCLUDED.status,
            sensitivity = EXCLUDED.sensitivity,
            embedding_text = EXCLUDED.embedding_text,
            embedding = EXCLUDED.embedding,
            embedding_status = EXCLUDED.embedding_status,
            embedding_model = EXCLUDED.embedding_model,
            embedding_dimensions = EXCLUDED.embedding_dimensions,
            embedding_updated_at = EXCLUDED.embedding_updated_at,
            embedding_error = EXCLUDED.embedding_error,
            content_hash = EXCLUDED.content_hash,
            updated_at = now()
        """
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM faq_documents WHERE id = %(id)s FOR UPDATE",
                {"id": row["id"]},
            ).fetchone()
            conn.execute(sql, payload)
            content_changed = self._faq_content_changed(
                existing,
                content_hash=content_hash,
            )
            live_gate_changed = existing is not None and (
                (existing.get("status") == "usable")
                != (payload["status"] == "usable")
            )
            if content_changed:
                self._mark_faq_knowledge_stale_in_conn(conn, row["id"])
            if content_changed or live_gate_changed:
                self._reconcile_kg_source_change_in_conn(
                    conn,
                    source_type="faq",
                    source_ids=[row["id"]],
                    delete_evidence=content_changed,
                )
            self._upsert_ready_faq_projection_in_conn(
                conn,
                {
                    **row,
                    "embedding_text": embedding_text,
                    "content_hash": content_hash,
                },
                embedding,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
            )

    def get_faq(self, faq_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM faq_documents WHERE id = %(id)s"
        with self.connect() as conn:
            return conn.execute(sql, {"id": faq_id}).fetchone()

    def prepare_faq_embedding(self, faq_id: str) -> dict[str, Any]:
        """锁定 FAQ 当前正文并建立本次向量指纹，NULL hash 不复用任何旧向量。"""
        lock_sql = """
        SELECT id, embedding_text, embedding_status, content_hash
        FROM faq_documents faq
        WHERE faq.id = %(id)s
        FOR UPDATE OF faq
        """
        update_sql = """
        UPDATE faq_documents
        SET content_hash = %(content_hash)s,
            embedding_status = CASE
                WHEN embedding_status = 'ready' THEN 'stale'
                ELSE embedding_status
            END,
            embedding_error = NULL,
            updated_at = now()
        WHERE id = %(id)s
        RETURNING *
        """
        with self.connect() as conn:
            row = conn.execute(lock_sql, {"id": faq_id}).fetchone()
            if row is None:
                raise KeyError(f"FAQ not found: {faq_id}")
            content_hash = compute_content_hash(row)
            if row.get("content_hash") == content_hash:
                return row
            prepared = conn.execute(
                update_sql,
                {"id": faq_id, "content_hash": content_hash},
            ).fetchone()
            if prepared is None:
                raise KeyError(f"FAQ not found: {faq_id}")
            return prepared

    def save_faq_text(self, row: dict[str, Any]) -> dict[str, Any]:
        """保存 FAQ 正文；读取旧值、写入新值和 KG 来源回退必须处于同一事务。"""
        sql = """
        INSERT INTO faq_documents (
            id, doc_type, source_file, source_group, source_date, category,
            question, question_variants, answer, tags, evidence, confidence,
            status, sensitivity, embedding_text, embedding_status,
            embedding_error, content_hash
        )
        VALUES (
            %(id)s, %(doc_type)s, %(source_file)s, %(source_group)s, %(source_date)s, %(category)s,
            %(question)s, %(question_variants)s::jsonb, %(answer)s, %(tags)s::jsonb,
            %(evidence)s::jsonb, %(confidence)s, %(status)s, %(sensitivity)s,
            %(embedding_text)s, %(embedding_status)s, %(embedding_error)s, %(content_hash)s
        )
        ON CONFLICT (id) DO UPDATE SET
            doc_type = EXCLUDED.doc_type,
            source_file = EXCLUDED.source_file,
            source_group = EXCLUDED.source_group,
            source_date = EXCLUDED.source_date,
            category = EXCLUDED.category,
            question = EXCLUDED.question,
            question_variants = EXCLUDED.question_variants,
            answer = EXCLUDED.answer,
            tags = EXCLUDED.tags,
            evidence = EXCLUDED.evidence,
            confidence = EXCLUDED.confidence,
            status = EXCLUDED.status,
            sensitivity = EXCLUDED.sensitivity,
            embedding_text = EXCLUDED.embedding_text,
            embedding_status = EXCLUDED.embedding_status,
            embedding_error = EXCLUDED.embedding_error,
            content_hash = EXCLUDED.content_hash,
            updated_at = now()
        RETURNING *
        """
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM faq_documents WHERE id = %(id)s FOR UPDATE",
                {"id": row["id"]},
            ).fetchone()
            embedding_text = row.get("embedding_text") or build_embedding_text(row)
            new_hash = compute_content_hash({**row, "embedding_text": embedding_text})
            previous_hash = existing["content_hash"] if existing else None
            embedding_status = next_embedding_status(
                existing["embedding_status"] if existing else None,
                previous_hash,
                new_hash,
            )
            payload = {
                "id": row["id"],
                "doc_type": row.get("doc_type", "faq_qa"),
                "source_file": row.get("source_file"),
                "source_group": row.get("source_group"),
                "source_date": row.get("source_date"),
                "category": row.get("category"),
                "question": row["question"],
                "question_variants": json.dumps(
                    clean_list(row.get("question_variants")), ensure_ascii=False
                ),
                "answer": row["answer"],
                "tags": json.dumps(clean_list(row.get("tags")), ensure_ascii=False),
                "evidence": json.dumps(row.get("evidence", []), ensure_ascii=False),
                "confidence": row.get("confidence", "high"),
                "status": row.get("status", "usable"),
                "sensitivity": row.get("sensitivity"),
                "embedding_text": embedding_text,
                "embedding_status": embedding_status,
                "embedding_error": (
                    None
                    if embedding_status in {"pending", "stale"}
                    else row.get("embedding_error")
                ),
                "content_hash": new_hash,
            }
            saved = conn.execute(sql, payload).fetchone()
            content_changed = self._faq_content_changed(
                existing,
                content_hash=new_hash,
            )
            if content_changed:
                self._mark_faq_knowledge_stale_in_conn(conn, row["id"])
                self._set_faq_knowledge_status_in_conn(
                    conn,
                    [row["id"]],
                    payload["status"],
                )
            else:
                self._sync_faq_knowledge_fields_in_conn(
                    conn,
                    {
                        **row,
                        "embedding_text": embedding_text,
                        "content_hash": new_hash,
                        "confidence": payload["confidence"],
                        "status": payload["status"],
                    },
                )
            live_gate_changed = existing is not None and (
                (existing.get("status") == "usable")
                != (payload["status"] == "usable")
            )
            if content_changed or live_gate_changed:
                self._reconcile_kg_source_change_in_conn(
                    conn,
                    source_type="faq",
                    source_ids=[row["id"]],
                    delete_evidence=content_changed,
                )
            return saved

    def update_faq_embedding(
        self,
        faq_id: str,
        embedding: list[float],
        *,
        embedding_model: str,
        embedding_dimensions: int,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        """按内容指纹提交 FAQ 向量，并在同一事务刷新统一知识投影。"""
        if not isinstance(expected_content_hash, str) or not expected_content_hash:
            raise ValueError("FAQ embedding requires expected_content_hash")
        sql = """
        UPDATE faq_documents
        SET embedding = %(embedding)s::vector,
            embedding_status = 'ready',
            embedding_model = %(embedding_model)s,
            embedding_dimensions = %(embedding_dimensions)s,
            embedding_updated_at = now(),
            embedding_error = NULL,
            updated_at = now()
        WHERE id = %(id)s
          AND content_hash = %(expected_content_hash)s
        RETURNING *
        """
        params = {
            "id": faq_id,
            "embedding": format_vector(embedding),
            "embedding_model": embedding_model,
            "embedding_dimensions": embedding_dimensions,
            "expected_content_hash": expected_content_hash,
        }
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                raise ValueError("FAQ changed or was deleted during embedding")
            self._upsert_ready_faq_projection_in_conn(
                conn,
                row,
                embedding,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
            )
            return row

    def mark_embedding_failed(
        self,
        faq_id: str,
        error: str,
        *,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        """按准备阶段指纹记录失败，正文已变化时拒绝污染当前 FAQ。"""
        sql = """
        UPDATE faq_documents
        SET embedding_status = 'failed',
            embedding_error = %(error)s,
            updated_at = now()
        WHERE id = %(id)s
          AND content_hash = %(expected_content_hash)s
        RETURNING *
        """
        params = {
            "id": faq_id,
            "error": error[:1000],
            "expected_content_hash": expected_content_hash,
        }
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            raise ValueError("FAQ changed or was deleted during embedding")
        return row

    def update_faq_statuses(self, ids: list[str], status: str) -> list[dict[str, Any]]:
        """批量更新 FAQ 状态，仅在 usable 门禁真实变化时重算来源 KG。"""
        lock_sql = """
        SELECT id, status
        FROM faq_documents faq
        WHERE faq.id = ANY(%(ids)s::text[])
        ORDER BY faq.id ASC
        FOR UPDATE OF faq
        """
        sql = """
        UPDATE faq_documents
        SET status = %(status)s,
            updated_at = now()
        WHERE id = ANY(%(ids)s::text[])
        RETURNING id, question, answer, category, tags, confidence, status,
                  embedding_status, embedding_model, embedding_dimensions,
                  embedding_updated_at, embedding_error, updated_at
        """
        with self.connect() as conn:
            previous_rows = conn.execute(lock_sql, {"ids": ids}).fetchall()
            rows = conn.execute(sql, {"ids": ids, "status": status}).fetchall()
            if rows:
                self._set_faq_knowledge_status_in_conn(
                    conn,
                    [item["id"] for item in rows],
                    status,
                )
            changed_source_ids = sorted(
                row["id"]
                for row in previous_rows
                if (row.get("status") == "usable") != (status == "usable")
            )
            if changed_source_ids:
                self._reconcile_kg_source_change_in_conn(
                    conn,
                    source_type="faq",
                    source_ids=changed_source_ids,
                )
            return rows

    @staticmethod
    def _faq_content_changed(
        existing: dict[str, Any] | None,
        *,
        content_hash: str,
    ) -> bool:
        """按当前指纹判断 FAQ 变化，缺指纹就是必须重建的非当前行。"""
        if existing is None:
            return False
        return existing.get("content_hash") != content_hash

    @staticmethod
    def _mark_faq_knowledge_stale_in_conn(conn: Any, source_id: str) -> None:
        """让正文已变化的 FAQ 投影退出 ready，关键约束是与 FAQ 保存共用事务。"""
        conn.execute(
            """
            UPDATE knowledge_chunks
            SET embedding_status = 'stale',
                embedding_error = NULL,
                updated_at = now()
            WHERE source_type = 'faq'
              AND source_id = %(source_id)s
            """,
            {"source_id": source_id},
        )

    @staticmethod
    def _set_faq_knowledge_status_in_conn(
        conn: Any,
        source_ids: list[str],
        status: str,
    ) -> None:
        """在 FAQ 状态事务内同步投影，保证审核、检索和 KG 证据使用同一状态。"""
        conn.execute(
            """
            UPDATE knowledge_chunks
            SET status = %(status)s,
                updated_at = now()
            WHERE source_type = 'faq'
              AND source_id = ANY(%(source_ids)s::text[])
            """,
            {"source_ids": source_ids, "status": status},
        )

    def _sync_faq_knowledge_fields_in_conn(
        self,
        conn: Any,
        row: dict[str, Any],
    ) -> None:
        """原地同步不影响向量的 FAQ 投影字段；关键约束是保留现有 embedding 状态。"""
        knowledge_row = build_faq_knowledge_chunk_row(row)
        payload = self._knowledge_chunk_payload(knowledge_row)
        conn.execute(
            """
            UPDATE knowledge_chunks
            SET source_title = %(source_title)s,
                content = %(content)s,
                embedding_text = %(embedding_text)s,
                search_text = %(search_text)s,
                metadata = %(metadata)s::jsonb,
                tags = %(tags)s::jsonb,
                confidence = %(confidence)s,
                status = %(status)s,
                content_hash = %(content_hash)s,
                updated_at = now()
            WHERE source_type = 'faq'
              AND source_id = %(source_id)s
              AND chunk_index = 0
            """,
            payload,
        )

    def _upsert_ready_faq_projection_in_conn(
        self,
        conn: Any,
        row: dict[str, Any],
        embedding: list[float],
        *,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> None:
        """在 FAQ 写事务内同步 ready 投影，避免导入后出现第二条补偿路径。"""
        knowledge_row = build_faq_knowledge_chunk_row(row)
        payload = self._knowledge_chunk_payload(
            knowledge_row,
            embedding=embedding,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
        )
        conn.execute(self._insert_knowledge_chunk_sql(), payload)

    def list_faqs(
        self,
        *,
        query: str = "",
        status: str | None = None,
        embedding_status: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if query:
            params["query"] = f"%{query}%"
            clauses.append("(question ILIKE %(query)s OR answer ILIKE %(query)s OR category ILIKE %(query)s)")
        if status:
            params["status"] = status
            clauses.append("status = %(status)s")
        if embedding_status:
            params["embedding_status"] = embedding_status
            clauses.append("embedding_status = %(embedding_status)s")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows_sql = f"""
        SELECT id, question, answer, category, tags, confidence, status,
               embedding_status, embedding_model, embedding_dimensions,
               embedding_updated_at, embedding_error, updated_at
        FROM faq_documents
        {where}
        ORDER BY updated_at DESC, id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        count_sql = f"SELECT count(*) AS total FROM faq_documents {where}"
        status_sql = "SELECT status, count(*) AS count FROM faq_documents GROUP BY status"
        embedding_sql = """
        SELECT embedding_status, count(*) AS count
        FROM faq_documents
        GROUP BY embedding_status
        """
        with self.connect() as conn:
            rows = conn.execute(rows_sql, params).fetchall()
            total = conn.execute(count_sql, params).fetchone()["total"]
            status_counts = conn.execute(status_sql).fetchall()
            embedding_counts = conn.execute(embedding_sql).fetchall()
        return {
            "items": rows,
            "total": total,
            "status_counts": {row["status"]: row["count"] for row in status_counts},
            "embedding_counts": {
                row["embedding_status"]: row["count"] for row in embedding_counts
            },
        }

    def list_embedding_candidates(self, *, limit: int = 50) -> list[dict[str, Any]]:
        sql = """
        SELECT *
        FROM faq_documents
        WHERE embedding_status IN ('pending', 'stale', 'failed')
        ORDER BY updated_at ASC
        LIMIT %(limit)s
        """
        with self.connect() as conn:
            return conn.execute(sql, {"limit": limit}).fetchall()
