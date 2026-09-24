"""cyclops.db 包：组合数据库业务 mixin，并导出 canonical 数据模型与构建器。"""

from __future__ import annotations

from cyclops.db.analytics import AnalyticsMixin
from cyclops.db.base import BaseDatabase
from cyclops.db.builders import (
    build_document_knowledge_chunk_row,
    build_embedding_text,
    build_faq_knowledge_chunk_row,
    build_import_candidate_faq_row,
    compute_content_hash,
    compute_knowledge_chunk_hash,
    document_embedding_source_fingerprint,
    empty_import_file_embedding_summary,
    next_embedding_status,
)
from cyclops.db.faq import FaqMixin
from cyclops.db.imports import ImportMixin
from cyclops.db.kg import KgReviewConflictError, KnowledgeGraphMixin
from cyclops.db.knowledge import KnowledgeMixin
from cyclops.db.models import (
    KgExpandedCandidate,
    KgFactHit,
    RetrievedKnowledgeChunk,
    format_vector,
    score_to_distance,
)
from cyclops.db.retrieval_meta import (
    RETRIEVAL_EVAL_BASELINE_STRATEGY,
    RETRIEVAL_EVAL_CONTRACT_VERSION,
    RETRIEVAL_EVAL_KG_DEBUG_STRATEGY,
    RETRIEVAL_EVAL_STRATEGIES,
    RetrievalMetaMixin,
)


class Database(
    FaqMixin,
    KnowledgeMixin,
    KnowledgeGraphMixin,
    ImportMixin,
    RetrievalMetaMixin,
    AnalyticsMixin,
    BaseDatabase,
):
    """统一数据库入口：多个业务 mixin 共享 BaseDatabase 的连接管理。"""

    pass


__all__ = [
    "Database",
    "KgExpandedCandidate",
    "KgFactHit",
    "KgReviewConflictError",
    "RetrievedKnowledgeChunk",
    "RETRIEVAL_EVAL_BASELINE_STRATEGY",
    "RETRIEVAL_EVAL_CONTRACT_VERSION",
    "RETRIEVAL_EVAL_KG_DEBUG_STRATEGY",
    "RETRIEVAL_EVAL_STRATEGIES",
    "format_vector",
    "score_to_distance",
    "build_embedding_text",
    "build_faq_knowledge_chunk_row",
    "build_document_knowledge_chunk_row",
    "build_import_candidate_faq_row",
    "compute_content_hash",
    "compute_knowledge_chunk_hash",
    "document_embedding_source_fingerprint",
    "next_embedding_status",
    "empty_import_file_embedding_summary",
]
