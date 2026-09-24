from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from cyclops.db import RetrievedKnowledgeChunk
from cyclops.rag import EMPTY_RESPONSE_FALLBACK, build_user_prompt
from cyclops.retrieval import HybridRetrievalResult


@dataclass(frozen=True)
class RagToolDocument:
    """表示 agent 可消费的检索来源，同时保留 canonical 来源定位与既有输出键。"""

    id: str
    score: float
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
    content: str
    metadata: dict[str, Any]
    question: str
    answer: str
    category: str | None
    tags: list[str]
    source_date: str | None
    confidence: str | None
    status: str

    @classmethod
    def from_retrieved(cls, doc: RetrievedKnowledgeChunk) -> "RagToolDocument":
        """从 canonical 候选生成 MCP wire 文档，拒绝匿名对象并显式派生问答字段。"""
        if not isinstance(doc, RetrievedKnowledgeChunk):
            raise TypeError("RAG tool document must be RetrievedKnowledgeChunk")
        category = doc.metadata.get("category") or doc.source_type
        source_date = doc.metadata.get("source_date")
        return cls(
            id=doc.id,
            score=doc.score,
            source_type=doc.source_type,
            source_id=doc.source_id,
            source_chunk_id=doc.source_chunk_id,
            parent_chunk_id=doc.parent_chunk_id,
            chunk_level=doc.chunk_level,
            source_title=doc.source_title,
            section_path=doc.section_path,
            page_start=doc.page_start,
            page_end=doc.page_end,
            block_type=doc.block_type,
            content=doc.content,
            metadata=doc.metadata,
            question=doc.source_title or doc.source_id,
            answer=doc.content,
            category=str(category) if category else None,
            tags=doc.tags,
            source_date=str(source_date) if source_date else None,
            confidence=doc.confidence,
            status=doc.status,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化 agent 来源，关键约束是保留原有键并补齐统一来源定位。"""
        return {
            "id": self.id,
            "score": self.score,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_chunk_id": self.source_chunk_id,
            "parent_chunk_id": self.parent_chunk_id,
            "chunk_level": self.chunk_level,
            "source_title": self.source_title,
            "section_path": self.section_path,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "block_type": self.block_type,
            "content": self.content,
            "metadata": self.metadata,
            "question": self.question,
            "answer": self.answer,
            "category": self.category,
            "tags": self.tags,
            "source_date": self.source_date,
            "confidence": self.confidence,
            "status": self.status,
        }


@dataclass(frozen=True)
class RagToolSearchResult:
    """表示 agent 搜索结果，关键约束是保留真实 rerank 诊断。"""

    question: str
    documents: list[RagToolDocument]
    top_k: int
    min_score: float
    rerank_used: bool

    def to_dict(self) -> dict[str, Any]:
        """序列化搜索结果，rerank_used 必须与统一检索结果一致。"""
        return {
            "tool": "faq_rag",
            "mode": "search",
            "question": self.question,
            "has_context": bool(self.documents),
            "top_score": self.documents[0].score if self.documents else None,
            "top_k": self.top_k,
            "min_score": self.min_score,
            "rerank_used": self.rerank_used,
            "documents": [doc.to_dict() for doc in self.documents],
        }


@dataclass(frozen=True)
class RagToolAnswerResult:
    """表示 agent 回答草稿，关键约束是搜索诊断不在生成层丢失。"""

    question: str
    answer_draft: str
    documents: list[RagToolDocument]
    top_k: int
    min_score: float
    rerank_used: bool

    def to_dict(self) -> dict[str, Any]:
        """序列化草稿结果，直接透传 rerank 使用状态。"""
        return {
            "tool": "faq_rag",
            "mode": "answer_draft",
            "question": self.question,
            "answer_draft": self.answer_draft,
            "has_context": bool(self.documents),
            "top_score": self.documents[0].score if self.documents else None,
            "top_k": self.top_k,
            "min_score": self.min_score,
            "rerank_used": self.rerank_used,
            "documents": [doc.to_dict() for doc in self.documents],
        }


class RagTool:
    """向 agent 暴露检索与草稿生成，所有模式都复用唯一混合检索服务。"""

    def __init__(
        self,
        retrieval: Any,
        chat: Any,
        system_prompt: str,
    ) -> None:
        """保存工具依赖；top_k 和 min_score 只从统一检索服务读取。"""
        self.retrieval = retrieval
        self.chat = chat
        self.system_prompt = system_prompt
        self.top_k = retrieval.top_k
        self.min_score = retrieval.min_score

    def search(self, question: str) -> RagToolSearchResult:
        """返回 direct 检索候选，不把 parent 上下文计入搜索结果。"""
        result = self._retrieve(question, include_parent_context=False)
        docs = [candidate.document for candidate in result.candidates]
        return RagToolSearchResult(
            question=question,
            documents=[RagToolDocument.from_retrieved(doc) for doc in docs],
            top_k=self.top_k,
            min_score=self.min_score,
            rerank_used=result.rerank_used,
        )

    def answer(self, question: str) -> RagToolAnswerResult:
        """用 direct 候选与 parent 上下文生成草稿，来源列表只返回 direct 候选。"""
        result = self._retrieve(question, include_parent_context=True)
        docs = [candidate.document for candidate in result.candidates]
        prompt = build_user_prompt(question, [*docs, *result.parent_documents])
        answer_draft = self.chat.complete(self.system_prompt, prompt).strip()
        if not answer_draft:
            answer_draft = EMPTY_RESPONSE_FALLBACK
        return RagToolAnswerResult(
            question=question,
            answer_draft=answer_draft,
            documents=[RagToolDocument.from_retrieved(doc) for doc in docs],
            top_k=self.top_k,
            min_score=self.min_score,
            rerank_used=result.rerank_used,
        )

    def _retrieve(
        self,
        question: str,
        *,
        include_parent_context: bool,
    ) -> HybridRetrievalResult:
        """调用唯一检索服务，关键约束是正式工具永远关闭 KG debug。"""
        return self.retrieval.retrieve(
            question,
            include_parent_context=include_parent_context,
            use_kg=False,
        )

    def stream_answer(self, question: str) -> Iterator[dict[str, Any]]:
        """流式回答生成器；先 yield 多个 delta 事件，最后 yield 一个 final 事件。

        关键约束：每个 delta 事件形如 {"type": "delta", "text": "..."}，
        final 事件含完整 answer_draft + documents + top_score + hit_count，
        供上游 MCP / SSE 等流式 transport 直接转发。空回复时给占位文案。
        """
        result = self._retrieve(question, include_parent_context=True)
        docs = [candidate.document for candidate in result.candidates]
        prompt = build_user_prompt(question, [*docs, *result.parent_documents])
        parts: list[str] = []
        for delta in self.chat.stream_complete(self.system_prompt, prompt):
            if not delta:
                continue
            parts.append(delta)
            yield {"type": "delta", "text": delta}
        answer_draft = "".join(parts).strip()
        if not answer_draft:
            answer_draft = EMPTY_RESPONSE_FALLBACK
        documents = [RagToolDocument.from_retrieved(doc) for doc in docs]
        yield {
            "type": "final",
            "answer_draft": answer_draft,
            "documents": [doc.to_dict() for doc in documents],
            "top_score": documents[0].score if documents else None,
            "hit_count": len(documents),
            "top_k": self.top_k,
            "min_score": self.min_score,
            "rerank_used": result.rerank_used,
            "has_context": bool(documents),
        }
