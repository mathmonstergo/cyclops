from dataclasses import replace
from types import SimpleNamespace

import pytest

from cyclops.rag import (
    RagService,
    build_user_prompt,
    format_document,
    normalize_conversation_context,
)
from cyclops.retrieval import FusedCandidate, HybridRetrievalResult


class FakeRetrieval:
    """返回固定统一检索结果，并记录正式入口保持 KG 关闭。"""

    def __init__(self, result):
        """保存预设统一检索结果，供回答链路测试稳定复用。"""
        self.result = result

    def retrieve(self, query, *, include_parent_context, use_kg):
        """校验正式回答检索参数并返回预设结果，KG 必须保持关闭。"""
        assert include_parent_context is True
        assert use_kg is False
        return self.result


class FakeChat:
    def __init__(self):
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return "Please check whether the assignment was published first."


class WhitespaceChat:
    def complete(self, system_prompt, user_prompt):
        return "   \n"


def _unified_document(*, chunk_id="kc_document_child_1", chunk_level="child", parent_id=None):
    """构造统一知识文档，关键约束是不依赖 RetrievedDocument 旧模型。"""
    from cyclops.db import RetrievedKnowledgeChunk

    return RetrievedKnowledgeChunk(
        id=chunk_id,
        source_type="document",
        source_id="file_1",
        source_chunk_id="chunk_1",
        parent_chunk_id=parent_id,
        chunk_level=chunk_level,
        source_title="平台操作手册",
        section_path=["任务派发"],
        page_start=1,
        page_end=1,
        block_type="text",
        source_offsets={},
        content="请先检查任务是否已经发布。",
        metadata={"category": "support workflow", "source_date": "2025-09"},
        tags=["量表", "任务派发"],
        confidence=None,
        status="usable",
        score=0.82,
    )


def _hybrid_result(*, candidates=(), parents=()):
    """构造完整统一检索结果，供入口层契约测试复用。"""
    return HybridRetrievalResult(
        query="Why is the item missing?",
        query_terms=[],
        vector_documents=[candidate.document for candidate in candidates],
        keyword_documents=[],
        candidates=list(candidates),
        parent_documents=list(parents),
        kg_fact_hits=[],
        kg_expanded_candidates=[],
        candidate_limit=10,
        query_embedding_dimensions=3,
        rerank_used=False,
    )


def test_rag_service_uses_hybrid_retrieval_and_keeps_kg_disabled():
    """正式回答入口必须调用唯一混合服务，并显式保持 KG 关闭。"""
    document = _unified_document()
    result = _hybrid_result(
        candidates=[
            FusedCandidate(
                document=document,
                fused_score=0.1,
                channels=("vector",),
                vector_score=document.score,
            )
        ]
    )

    class FakeRetrieval:
        """记录唯一检索服务调用形状。"""

        def retrieve(self, query, *, include_parent_context, use_kg):
            """校验 RagService 的唯一检索入口参数并返回固定结果。"""
            assert query == "Why is the item missing?"
            assert include_parent_context is True
            assert use_kg is False
            return result

    chat = FakeChat()
    service = RagService(
        retrieval=FakeRetrieval(),
        chat=chat,
        system_prompt="系统提示",
    )

    service.answer("Why is the item missing?")

    assert "平台操作手册" in chat.calls[0][1]
    assert "请先检查任务是否已经发布" in chat.calls[0][1]


def test_build_user_prompt_formats_canonical_knowledge_provenance():
    """回答上下文应直接展示统一知识来源字段，不依赖旧 FAQ 属性别名。"""
    prompt = build_user_prompt("怎么派发任务？", [_unified_document()])

    assert "source_type=document" in prompt
    assert "source_id=file_1" in prompt
    assert "source_chunk_id=chunk_1" in prompt
    assert "section_path=任务派发" in prompt
    assert "page=1" in prompt
    assert "content=请先检查任务是否已经发布。" in prompt


def test_format_document_rejects_anonymous_canonical_shaped_object():
    """提示词边界只接受 RetrievedKnowledgeChunk，完整字段的匿名对象也必须拒绝。"""
    anonymous = SimpleNamespace(**_unified_document().__dict__)

    with pytest.raises(TypeError, match="RetrievedKnowledgeChunk"):
        format_document(1, anonymous)


def test_build_user_prompt_keeps_missing_page_locator_empty():
    """无页码的 FAQ 上下文不得把 Python None 暴露给模型。"""
    faq = replace(
        _unified_document(),
        source_type="faq",
        source_id="faq_1",
        source_chunk_id=None,
        page_start=None,
        page_end=None,
        block_type="faq",
    )

    prompt = build_user_prompt("怎么处理？", [faq])

    assert "page=None" not in prompt
    assert "page=\n" in prompt


def test_rag_uses_retrieved_context():
    """RAG 回答必须把召回正文和分数写入模型上下文。"""
    document = _unified_document()
    result = _hybrid_result(
        candidates=[
            FusedCandidate(
                document=document,
                fused_score=0.1,
                channels=("vector",),
                vector_score=document.score,
            )
        ]
    )
    chat = FakeChat()
    service = RagService(
        retrieval=FakeRetrieval(result),
        chat=chat,
        system_prompt="系统提示",
    )
    assert service.answer("Why is the item missing?") == "Please check whether the assignment was published first."
    assert "平台操作手册" in chat.calls[0][1]
    assert "score=0.82" in chat.calls[0][1]


def test_rag_handles_no_context_without_claiming_realtime_status():
    """无召回内容时提示知识不足，并禁止声称实时后台状态。"""
    chat = FakeChat()
    service = RagService(
        retrieval=FakeRetrieval(_hybrid_result()),
        chat=chat,
        system_prompt="系统提示",
    )
    service.answer("Has the backend refreshed?")
    assert "知识库没有检索到明确答案" in chat.calls[0][1]
    assert "不要编造后台实时状态" in chat.calls[0][1]


def test_rag_returns_safe_fallback_for_whitespace_model_response():
    """模型仅返回空白时必须给出安全兜底文案。"""
    service = RagService(
        retrieval=FakeRetrieval(_hybrid_result()),
        chat=WhitespaceChat(),
        system_prompt="系统提示",
    )
    assert service.answer("Why is the item missing?") == "模型服务暂时没有返回有效内容，请稍后重试或转人工处理。"


def test_build_user_prompt_includes_compacted_conversation_context():
    context = normalize_conversation_context(
        {
            "summary": "此前用户一直在排查报告导出失败。",
            "recent_messages": [
                {"role": "user", "content": "刚才说的那个报告在哪里下载？"},
                {"role": "assistant", "content": "可以在报告中心下载。"},
            ],
        }
    )

    prompt = build_user_prompt("那如果没有按钮呢？", [], conversation_context=context)

    assert "### [WORKING MEMORY]" in prompt
    assert "<earlier_context>\n此前用户一直在排查报告导出失败。\n</earlier_context>" in prompt
    assert "[USER] 刚才说的那个报告在哪里下载？" in prompt
    assert "[Agent] 可以在报告中心下载。" in prompt
    assert "当前用户问题：那如果没有按钮呢？" in prompt


def test_normalize_conversation_context_discards_invalid_or_empty_items():
    context = normalize_conversation_context(
        {
            "summary": "  摘要  ",
            "recent_messages": [
                {"role": "user", "content": "  有效问题  "},
                {"role": "system", "content": "不允许的角色"},
                {"role": "assistant", "content": ""},
                "not a dict",
            ],
        }
    )

    assert context == {
        "summary": "摘要",
        "recent_messages": [{"role": "user", "content": "有效问题"}],
    }
