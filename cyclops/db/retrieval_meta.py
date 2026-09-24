from __future__ import annotations

import json
import uuid
from typing import Any

from cyclops.db.builders import clean_list


RETRIEVAL_EVAL_CONTRACT_VERSION = 2
RETRIEVAL_EVAL_BASELINE_STRATEGY = "retrieval_hybrid_v1"
RETRIEVAL_EVAL_KG_DEBUG_STRATEGY = "retrieval_hybrid_v1_kg_debug"
RETRIEVAL_EVAL_STRATEGIES = (
    RETRIEVAL_EVAL_BASELINE_STRATEGY,
    RETRIEVAL_EVAL_KG_DEBUG_STRATEGY,
)


class RetrievalMetaMixin:
    """检索评测用例 / 评测运行 / 别名词典。"""

    def create_retrieval_eval_case(self, row: dict[str, Any]) -> dict[str, Any]:
        """创建或更新检索评测用例，关键约束是期望命中字段统一保存为 JSONB。"""
        payload = {
            "id": row["id"],
            "question": row["question"],
            "intent": row.get("intent"),
            "expected_source_ids": json.dumps(
                clean_list(row.get("expected_source_ids")),
                ensure_ascii=False,
            ),
            "expected_chunk_ids": json.dumps(
                clean_list(row.get("expected_chunk_ids")),
                ensure_ascii=False,
            ),
            "tags": json.dumps(clean_list(row.get("tags")), ensure_ascii=False),
            "note": row.get("note"),
            "status": row.get("status", "active"),
        }
        sql = """
        INSERT INTO retrieval_eval_cases (
            id, question, intent, expected_source_ids, expected_chunk_ids,
            tags, note, status
        )
        VALUES (
            %(id)s, %(question)s, %(intent)s, %(expected_source_ids)s::jsonb,
            %(expected_chunk_ids)s::jsonb, %(tags)s::jsonb, %(note)s, %(status)s
        )
        ON CONFLICT (id) DO UPDATE SET
            question = EXCLUDED.question,
            intent = EXCLUDED.intent,
            expected_source_ids = EXCLUDED.expected_source_ids,
            expected_chunk_ids = EXCLUDED.expected_chunk_ids,
            tags = EXCLUDED.tags,
            note = EXCLUDED.note,
            status = EXCLUDED.status,
            updated_at = now()
        RETURNING *
        """
        with self.connect() as conn:
            return conn.execute(sql, payload).fetchone()

    def list_retrieval_eval_cases(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """列出评测用例，并按固定顺序返回两种 current strategy 的最新运行。"""
        clauses = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
            clauses.append("c.status = %(status)s")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows_sql = f"""
        SELECT
            c.*,
            latest.latest_runs
        FROM retrieval_eval_cases c
        LEFT JOIN LATERAL (
            SELECT COALESCE(
                jsonb_agg(
                    jsonb_build_object(
                        'id', run.id,
                        'case_id', run.case_id,
                        'strategy', run.strategy,
                        'retrieved_items', run.retrieved_items,
                        'metrics', run.metrics,
                        'analysis', run.analysis,
                        'created_at', run.created_at
                    )
                    ORDER BY CASE run.strategy
                        WHEN 'retrieval_hybrid_v1' THEN 1
                        WHEN 'retrieval_hybrid_v1_kg_debug' THEN 2
                    END
                ),
                '[]'::jsonb
            ) AS latest_runs
            FROM (
                SELECT DISTINCT ON (run.strategy) run.*
                FROM retrieval_eval_runs run
                WHERE run.case_id = c.id
                  AND run.strategy IN (
                      'retrieval_hybrid_v1',
                      'retrieval_hybrid_v1_kg_debug'
                  )
                  AND run.analysis->'contract_version' = '2'::jsonb
                ORDER BY run.strategy, run.created_at DESC, run.id DESC
            ) AS run
        ) latest ON true
        {where}
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        count_sql = f"SELECT count(*) AS total FROM retrieval_eval_cases c {where}"
        with self.connect() as conn:
            rows = conn.execute(rows_sql, params).fetchall()
            total = conn.execute(count_sql, params).fetchone()["total"]
        return {"items": rows, "total": total}

    def record_retrieval_eval_run(self, row: dict[str, Any]) -> dict[str, Any]:
        """保存 current 评测运行，版本只接受精确整数 2。"""
        strategy = row.get("strategy")
        if strategy not in RETRIEVAL_EVAL_STRATEGIES:
            raise ValueError("retrieval eval strategy must be an explicit current strategy")
        analysis = row.get("analysis")
        contract_version = analysis.get("contract_version") if isinstance(analysis, dict) else None
        if (
            type(contract_version) is not int
            or contract_version != RETRIEVAL_EVAL_CONTRACT_VERSION
        ):
            raise ValueError("retrieval eval analysis contract_version must be 2")
        payload = {
            "id": row.get("id") or f"eval_run_{uuid.uuid4().hex[:12]}",
            "case_id": row["case_id"],
            "strategy": strategy,
            "retrieved_items": json.dumps(row["retrieved_items"], ensure_ascii=False),
            "metrics": json.dumps(row["metrics"], ensure_ascii=False),
            "analysis": json.dumps(analysis, ensure_ascii=False),
        }
        sql = """
        INSERT INTO retrieval_eval_runs (
            id, case_id, strategy, retrieved_items, metrics, analysis
        )
        VALUES (
            %(id)s, %(case_id)s, %(strategy)s, %(retrieved_items)s::jsonb,
            %(metrics)s::jsonb, %(analysis)s::jsonb
        )
        RETURNING *
        """
        with self.connect() as conn:
            return conn.execute(sql, payload).fetchone()

    def get_retrieval_eval_case(self, case_id: str) -> dict[str, Any] | None:
        """按 id 读取检索评测用例，供单条评测运行使用。"""
        sql = "SELECT * FROM retrieval_eval_cases WHERE id = %(id)s"
        with self.connect() as conn:
            return conn.execute(sql, {"id": case_id}).fetchone()

    def list_retrieval_aliases(self, status: str = "active") -> list[dict[str, Any]]:
        """列出检索别名词典，第一版只读取启用词条用于关键词扩展。"""
        sql = """
        SELECT *
        FROM retrieval_aliases
        WHERE status = %(status)s
        ORDER BY updated_at DESC, id DESC
        """
        with self.connect() as conn:
            return conn.execute(sql, {"status": status}).fetchall()

    def upsert_retrieval_alias(self, row: dict[str, Any]) -> dict[str, Any]:
        """写入检索别名词典，关键约束是标准词和别名都由人工维护。"""
        payload = {
            "id": row.get("id") or f"alias_{uuid.uuid4().hex[:12]}",
            "canonical": str(row.get("canonical", "")).strip(),
            "aliases": json.dumps(clean_list(row.get("aliases")), ensure_ascii=False),
            "tags": json.dumps(clean_list(row.get("tags")), ensure_ascii=False),
            "status": row.get("status", "active"),
        }
        sql = """
        INSERT INTO retrieval_aliases (id, canonical, aliases, tags, status)
        VALUES (
            %(id)s, %(canonical)s, %(aliases)s::jsonb, %(tags)s::jsonb, %(status)s
        )
        ON CONFLICT (id) DO UPDATE SET
            canonical = EXCLUDED.canonical,
            aliases = EXCLUDED.aliases,
            tags = EXCLUDED.tags,
            status = EXCLUDED.status,
            updated_at = now()
        RETURNING *
        """
        with self.connect() as conn:
            return conn.execute(sql, payload).fetchone()
