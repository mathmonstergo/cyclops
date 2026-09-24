from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


def format_vector(values: Iterable[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def score_to_distance(score: float) -> float:
    return 1.0 - score


@dataclass(frozen=True)
class RetrievedKnowledgeChunk:
    """统一知识单元检索结果，字段直接对应 canonical FAQ/文档来源契约。"""

    id: str
    source_type: str
    source_id: str
    source_chunk_id: str | None
    parent_chunk_id: str | None
    chunk_level: str
    source_title: str | None
    section_path: list[str]
    page_start: int | None
    page_end: int | None
    block_type: str | None
    source_offsets: dict[str, Any]
    content: str
    metadata: dict[str, Any]
    tags: list[str]
    confidence: str | None
    status: str
    score: float

@dataclass(frozen=True)
class KgFactHit:
    """表示显式 KG debug 命中的合成 fact，只用于后续证据展开与诊断。"""

    fact_chunk_id: str
    fact_id: str
    fact_type: str
    fact_rank: int
    fact_score: float


@dataclass(frozen=True)
class KgExpandedCandidate:
    """表示 KG fact 展开的原始知识候选，并保留贡献该候选的 fact 明细。"""

    document: RetrievedKnowledgeChunk
    kg_matches: tuple[KgFactHit, ...]
