from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = ("id", "question", "answer", "confidence", "status", "embedding_text")


def validate_faq_row(row: dict[str, Any]) -> list[str]:
    return [field for field in REQUIRED_FIELDS if field not in row]


def load_faq_rows(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} invalid JSON: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no} expected JSON object")
            missing = validate_faq_row(row)
            if missing:
                joined = ", ".join(missing)
                raise ValueError(f"{path}:{line_no} missing required fields: {joined}")
            rows.append(row)
    return rows


def import_faqs(path: str | Path, db: Any, embeddings: Any) -> int:
    """导入 FAQ 并原子写入 ready 投影，关键约束是显式记录当前 embedding 模型和维度。"""
    rows = load_faq_rows(path)
    for row in rows:
        vector = embeddings.embed(row["embedding_text"])
        db.upsert_faq(
            row,
            vector,
            embedding_model=embeddings.model,
            embedding_dimensions=embeddings.dimensions,
        )
    return len(rows)
