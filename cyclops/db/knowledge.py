from __future__ import annotations

import json
from typing import Any

from cyclops.db.builders import (
    child_knowledge_chunk_index,
    clean_dict,
    clean_int,
    clean_list,
    compute_knowledge_chunk_hash,
    document_embedding_source_fingerprint,
    expected_document_knowledge_count,
    join_search_text,
)
from cyclops.db.models import (
    RetrievedKnowledgeChunk,
    format_vector,
    score_to_distance,
)


class KnowledgeMixin:
    """统一知识单元写入 + 向量/关键词检索 + 父块上下文回填。"""

    def replace_document_chunk_embeddings(
        self,
        *,
        file_id: str,
        chunk_id: str,
        source_fingerprint: str,
        items: list[tuple[dict[str, Any], list[float]]],
        embedding_model: str,
        embedding_dimensions: int,
    ) -> list[dict[str, Any]]:
        """原子替换一个来源切片的全部向量行，迟到任务必须通过实时来源指纹门禁。"""
        if not file_id or not chunk_id or not source_fingerprint:
            raise ValueError("document embedding source guard is required")
        if not items:
            raise ValueError("document embedding batch requires parent and child rows")
        rows = [row for row, _embedding in items]
        parent_rows = [row for row in rows if row.get("chunk_level") == "parent"]
        child_rows = [row for row in rows if row.get("chunk_level") == "child"]
        if len(parent_rows) != 1 or not child_rows:
            raise ValueError("document embedding batch requires one parent and at least one child")
        parent_id = parent_rows[0].get("id")
        for row in rows:
            if (
                row.get("source_type") != "document"
                or row.get("source_id") != file_id
                or row.get("source_chunk_id") != chunk_id
            ):
                raise ValueError("document embedding row does not match source guard")
            if row.get("chunk_level") == "child" and row.get("parent_chunk_id") != parent_id:
                raise ValueError("document embedding child does not match batch parent")

        with self.connect() as conn:
            import_file = conn.execute(
                """
                SELECT imp.*
                FROM import_files imp
                WHERE imp.id = %(file_id)s
                FOR UPDATE OF imp
                """,
                {"file_id": file_id},
            ).fetchone()
            chunk = conn.execute(
                """
                SELECT chunk.*
                FROM import_chunks chunk
                WHERE chunk.id = %(chunk_id)s
                  AND chunk.file_id = %(file_id)s
                FOR UPDATE OF chunk
                """,
                {"file_id": file_id, "chunk_id": chunk_id},
            ).fetchone()
            source_available = bool(
                import_file
                and chunk
                and import_file.get("status") in {"needs_review", "completed"}
                and not import_file.get("is_disabled")
                and not chunk.get("is_disabled")
            )
            current_fingerprint = (
                document_embedding_source_fingerprint(import_file, chunk)
                if source_available
                else None
            )
            if not source_available or current_fingerprint != source_fingerprint:
                raise ValueError("document embedding source changed or became unavailable")
            self._validate_document_embedding_source_rows(rows, chunk)
            conn.execute(
                """
                DELETE FROM knowledge_chunks
                WHERE source_type = 'document'
                  AND source_id = %(file_id)s
                  AND source_chunk_id = %(chunk_id)s
                """,
                {"file_id": file_id, "chunk_id": chunk_id},
            )
            inserted: list[dict[str, Any]] = []
            for row, embedding in items:
                payload = self._knowledge_chunk_payload(
                    row,
                    embedding=embedding,
                    embedding_model=embedding_model,
                    embedding_dimensions=embedding_dimensions,
                )
                saved = conn.execute(self._insert_knowledge_chunk_sql(), payload).fetchone()
                inserted.append(saved)
            return inserted

    @staticmethod
    def _validate_document_embedding_source_rows(
        rows: list[dict[str, Any]],
        chunk: dict[str, Any],
    ) -> None:
        """按锁内实时切片校验完整确定性行，防止残缺批次覆盖 parent/children。"""
        chunk_id = str(chunk["id"])
        parent_id = f"kc_document_{chunk_id}"
        parent_index = int(chunk.get("chunk_index", 0))
        expected_count = expected_document_knowledge_count(chunk)
        expected_shape = [(parent_id, "parent", None, parent_index)]
        expected_shape.extend(
            (
                f"{parent_id}_child_{child_index}",
                "child",
                parent_id,
                child_knowledge_chunk_index(parent_index, child_index),
            )
            for child_index in range(1, expected_count)
        )
        actual_shape = [
            (
                row.get("id"),
                row.get("chunk_level"),
                row.get("parent_chunk_id"),
                row.get("chunk_index"),
            )
            for row in rows
        ]
        if actual_shape != expected_shape:
            raise ValueError("document embedding batch does not match deterministic source rows")

    @staticmethod
    def _knowledge_chunk_payload(
        row: dict[str, Any],
        embedding: list[float] | None = None,
        *,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
    ) -> dict[str, Any]:
        """构造统一知识单元写入参数，关键约束是只供 FAQ、文档和 KG 的受控事务复用。"""
        embedding_text = str(row.get("embedding_text") or row["content"]).strip()
        return {
            "id": row["id"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "source_chunk_id": row.get("source_chunk_id"),
            "parent_chunk_id": row.get("parent_chunk_id"),
            "chunk_level": row.get("chunk_level", "chunk"),
            "source_title": row.get("source_title"),
            "chunk_index": int(row.get("chunk_index", 0)),
            "section_path": json.dumps(clean_list(row.get("section_path")), ensure_ascii=False),
            "page_start": clean_int(row.get("page_start")),
            "page_end": clean_int(row.get("page_end")),
            "block_type": row.get("block_type"),
            "source_offsets": json.dumps(clean_dict(row.get("source_offsets")), ensure_ascii=False),
            "content": row["content"],
            "embedding_text": embedding_text,
            "search_text": row.get("search_text") or join_search_text(
                [row.get("source_title"), row.get("tags", []), row["content"]]
            ),
            "metadata": json.dumps(row.get("metadata", {}), ensure_ascii=False),
            "tags": json.dumps(clean_list(row.get("tags")), ensure_ascii=False),
            "confidence": row.get("confidence"),
            "status": row.get("status", "needs_review"),
            "embedding": format_vector(embedding) if embedding is not None else None,
            "embedding_status": "ready" if embedding is not None else row.get("embedding_status", "pending"),
            "embedding_model": embedding_model,
            "embedding_dimensions": embedding_dimensions,
            "embedding_error": None if embedding is not None else row.get("embedding_error"),
            "content_hash": row.get("content_hash")
            or compute_knowledge_chunk_hash({**row, "embedding_text": embedding_text}),
        }

    def search_knowledge(
        self,
        query_embedding: list[float],
        *,
        top_k: int,
        min_score: float,
        status: str = "usable",
    ) -> list[RetrievedKnowledgeChunk]:
        """从统一知识单元表检索内容，关键约束是不过滤 confidence 以允许文档切片命中。"""
        params = {
            "embedding": format_vector(query_embedding),
            "status": status,
            "max_distance": score_to_distance(min_score),
            "top_k": top_k,
        }
        with self.connect() as conn:
            rows = conn.execute(self._search_knowledge_sql(), params).fetchall()
        return [self._row_to_retrieved_chunk(row) for row in rows]

    def search_knowledge_text(
        self,
        query_text: str,
        *,
        top_k: int,
        query_terms: list[str] | None = None,
        status: str = "usable",
    ) -> list[RetrievedKnowledgeChunk]:
        """从统一知识单元表做关键词召回，关键约束是只返回正式可检索内容。"""
        normalized = str(query_text or "").strip()
        if not normalized:
            return []
        terms = clean_list(query_terms) or [normalized]
        params = {
            "query_like": f"%{normalized}%",
            "query_terms": terms,
            "status": status,
            "top_k": top_k,
        }
        with self.connect() as conn:
            rows = conn.execute(self._search_knowledge_text_sql(), params).fetchall()
        return [self._row_to_retrieved_chunk(row) for row in rows]

    def get_parent_context_chunks(
        self,
        child_ids: list[str],
        *,
        status: str = "usable",
    ) -> list[RetrievedKnowledgeChunk]:
        """按 child 命中回填 parent 上下文，关键约束是只读取同来源可用父块。"""
        unique_ids = list(dict.fromkeys(str(item).strip() for item in child_ids if str(item).strip()))
        if not unique_ids:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                self._get_parent_context_chunks_sql(),
                {"child_ids": unique_ids, "status": status},
            ).fetchall()
        return [self._row_to_retrieved_chunk(row) for row in rows]

    @staticmethod
    def _row_to_retrieved_chunk(row: dict[str, Any]) -> RetrievedKnowledgeChunk:
        return RetrievedKnowledgeChunk(
            id=row["id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            source_chunk_id=row["source_chunk_id"],
            parent_chunk_id=row["parent_chunk_id"],
            chunk_level=row["chunk_level"],
            source_title=row["source_title"],
            section_path=row["section_path"] or [],
            page_start=row["page_start"],
            page_end=row["page_end"],
            block_type=row["block_type"],
            source_offsets=row["source_offsets"] or {},
            content=row["content"],
            metadata=row["metadata"] or {},
            tags=row["tags"] or [],
            confidence=row["confidence"],
            status=row["status"],
            score=float(row["score"]),
        )

    @staticmethod
    def _insert_knowledge_chunk_sql() -> str:
        """集中维护统一知识单元 upsert SQL，避免多来源写入字段漂移。"""
        return """
        INSERT INTO knowledge_chunks (
            id, source_type, source_id, source_chunk_id, parent_chunk_id,
            chunk_level, source_title, chunk_index, section_path, page_start,
            page_end, block_type, source_offsets,
            content, embedding_text, search_text, metadata, tags, confidence, status,
            embedding, embedding_status, embedding_model, embedding_dimensions,
            embedding_updated_at, embedding_error, content_hash
        )
        VALUES (
            %(id)s, %(source_type)s, %(source_id)s, %(source_chunk_id)s,
            %(parent_chunk_id)s, %(chunk_level)s, %(source_title)s,
            %(chunk_index)s, %(section_path)s::jsonb, %(page_start)s,
            %(page_end)s, %(block_type)s, %(source_offsets)s::jsonb,
            %(content)s, %(embedding_text)s, %(search_text)s,
            %(metadata)s::jsonb, %(tags)s::jsonb, %(confidence)s, %(status)s,
            %(embedding)s::vector, %(embedding_status)s, %(embedding_model)s,
            %(embedding_dimensions)s,
            CASE WHEN %(embedding)s IS NULL THEN NULL ELSE now() END,
            %(embedding_error)s, %(content_hash)s
        )
        ON CONFLICT (source_type, source_id, chunk_index) DO UPDATE SET
            id = EXCLUDED.id,
            source_chunk_id = EXCLUDED.source_chunk_id,
            parent_chunk_id = EXCLUDED.parent_chunk_id,
            chunk_level = EXCLUDED.chunk_level,
            source_title = EXCLUDED.source_title,
            content = EXCLUDED.content,
            section_path = EXCLUDED.section_path,
            page_start = EXCLUDED.page_start,
            page_end = EXCLUDED.page_end,
            block_type = EXCLUDED.block_type,
            source_offsets = EXCLUDED.source_offsets,
            embedding_text = EXCLUDED.embedding_text,
            search_text = EXCLUDED.search_text,
            metadata = EXCLUDED.metadata,
            tags = EXCLUDED.tags,
            confidence = EXCLUDED.confidence,
            status = EXCLUDED.status,
            embedding = EXCLUDED.embedding,
            embedding_status = EXCLUDED.embedding_status,
            embedding_model = EXCLUDED.embedding_model,
            embedding_dimensions = EXCLUDED.embedding_dimensions,
            embedding_updated_at = EXCLUDED.embedding_updated_at,
            embedding_error = EXCLUDED.embedding_error,
            content_hash = EXCLUDED.content_hash,
            updated_at = now()
        RETURNING *
        """

    @staticmethod
    def _search_knowledge_sql() -> str:
        """集中维护统一知识单元向量检索 SQL，后续混合检索会复用同一候选表。

        LEFT JOIN import_files / import_chunks 是为了让"文档级 / 切片级禁用"立即在检索层生效，
        不需要重新生成 embedding；FAQ 必须同时满足实时审核状态和实时向量状态，不能读取旧投影。
        """
        return """
        SELECT
            kc.id, kc.source_type, kc.source_id, kc.source_chunk_id, kc.parent_chunk_id,
            kc.chunk_level, kc.source_title, kc.section_path, kc.page_start, kc.page_end,
            kc.block_type, kc.source_offsets, kc.content,
            kc.metadata, kc.tags, kc.confidence, kc.status,
            1 - (kc.embedding <=> %(embedding)s::vector) AS score
        FROM knowledge_chunks kc
        LEFT JOIN import_files imp
            ON kc.source_type = 'document' AND imp.id = kc.source_id
        LEFT JOIN import_chunks ic
            ON kc.source_type = 'document'
           AND ic.id = kc.source_chunk_id
           AND ic.file_id = kc.source_id
        LEFT JOIN faq_documents fq
            ON kc.source_type = 'faq' AND fq.id = kc.source_id
        WHERE kc.source_type IN ('faq', 'document')
          AND (
                (
                    kc.source_type = 'faq'
                    AND fq.status = %(status)s
                    AND fq.embedding_status = 'ready'
                )
                OR (kc.source_type = 'document' AND kc.status = %(status)s)
              )
          AND kc.embedding_status = 'ready'
          AND kc.embedding IS NOT NULL
          AND (kc.embedding <=> %(embedding)s::vector) <= %(max_distance)s
          AND (
              kc.source_type <> 'document'
              OR (
                  imp.id IS NOT NULL
                  AND ic.id IS NOT NULL
                  AND imp.is_disabled = false
                  AND ic.is_disabled = false
              )
          )
          AND (kc.source_type <> 'document' OR kc.chunk_level = 'child')
        ORDER BY kc.embedding <=> %(embedding)s::vector
        LIMIT %(top_k)s
        """

    @staticmethod
    def _search_knowledge_text_sql() -> str:
        """集中维护统一知识单元关键词检索 SQL，作为混合召回的第二路候选。

        与向量检索使用同一 ready/status 门禁，文档和切片仍按 is_disabled 过滤。
        """
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
        LEFT JOIN import_files imp
            ON kc.source_type = 'document' AND imp.id = kc.source_id
        LEFT JOIN import_chunks ic
            ON kc.source_type = 'document'
           AND ic.id = kc.source_chunk_id
           AND ic.file_id = kc.source_id
        LEFT JOIN faq_documents fq
            ON kc.source_type = 'faq' AND fq.id = kc.source_id
        WHERE kc.source_type IN ('faq', 'document')
          AND (
                (
                    kc.source_type = 'faq'
                    AND fq.status = %(status)s
                    AND fq.embedding_status = 'ready'
                )
                OR (kc.source_type = 'document' AND kc.status = %(status)s)
              )
          AND kc.embedding_status = 'ready'
          AND (
              kc.source_type <> 'document'
              OR (
                  imp.id IS NOT NULL
                  AND ic.id IS NOT NULL
                  AND imp.is_disabled = false
                  AND ic.is_disabled = false
              )
          )
          AND (kc.source_type <> 'document' OR kc.chunk_level = 'child')
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
    def _get_parent_context_chunks_sql() -> str:
        """集中维护 child 命中后的 parent 上下文回填 SQL。"""
        return """
        SELECT DISTINCT
            parent.id,
            parent.source_type,
            parent.source_id,
            parent.source_chunk_id,
            parent.parent_chunk_id,
            parent.chunk_level,
            parent.source_title,
            parent.section_path,
            parent.page_start,
            parent.page_end,
            parent.block_type,
            parent.source_offsets,
            parent.content,
            parent.metadata,
            parent.tags,
            parent.confidence,
            parent.status,
            1.0::double precision AS score
        FROM knowledge_chunks child
        JOIN knowledge_chunks parent
          ON parent.id = child.parent_chunk_id
         AND parent.source_type = child.source_type
         AND parent.source_id = child.source_id
         AND parent.source_chunk_id = child.source_chunk_id
        LEFT JOIN import_files imp
            ON parent.source_type = 'document' AND imp.id = parent.source_id
        LEFT JOIN import_chunks ic
            ON parent.source_type = 'document'
           AND ic.id = parent.source_chunk_id
           AND ic.file_id = parent.source_id
        WHERE child.id = ANY(%(child_ids)s::text[])
          AND parent.chunk_level = 'parent'
          AND parent.status = %(status)s
          AND parent.embedding_status = 'ready'
          AND (
              parent.source_type <> 'document'
              OR (
                  imp.id IS NOT NULL
                  AND ic.id IS NOT NULL
                  AND imp.is_disabled = false
                  AND ic.is_disabled = false
              )
          )
        ORDER BY parent.source_type, parent.source_id, parent.source_chunk_id, parent.id
        """
