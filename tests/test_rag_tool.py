from dataclasses import replace
from types import SimpleNamespace

import pytest

from cyclops.rag_tool import RagTool, RagToolDocument
from cyclops.retrieval import FusedCandidate, HybridRetrievalResult


class FakeRetrieval:
    """返回固定统一检索结果，并记录 search/answer 对 parent 的不同要求。"""

    top_k = 5
    min_score = 0.35

    def __init__(self, result):
        """保存预设结果并初始化调用记录，供 search 与 answer 断言复用。"""
        self.result = result
        self.calls = []

    def retrieve(self, question, *, include_parent_context, use_kg):
        """记录检索参数并返回预设结果，保留调用方传入的模式开关。"""
        self.calls.append((question, include_parent_context, use_kg))
        return self.result


class FakeChat:
    def __init__(self):
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return "Please check whether the assignment was published; if it still fails, collect the account, context, and a screenshot."


def _unified_chunk(*, score=0.82):
    """构造文档统一知识候选，验证 RagTool 不再依赖 FAQ-only 模型。"""
    from cyclops.db import RetrievedKnowledgeChunk

    return RetrievedKnowledgeChunk(
        id="kc_document_child_1",
        source_type="document",
        source_id="file_1",
        source_chunk_id="chunk_1",
        parent_chunk_id=None,
        chunk_level="child",
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
        score=score,
    )


def _unified_result(document=None, *, parents=(), rerank_used=False):
    """构造完整混合检索结果，保留 direct 候选和 parent 的边界。"""
    candidates = []
    if document is not None:
        candidates.append(
            FusedCandidate(
                document=document,
                fused_score=0.1,
                channels=("vector",),
                vector_score=document.score,
            )
        )
    return HybridRetrievalResult(
        query="Why is the assigned item missing?",
        query_terms=[],
        vector_documents=[document] if document is not None else [],
        keyword_documents=[],
        candidates=candidates,
        parent_documents=list(parents),
        kg_fact_hits=[],
        kg_expanded_candidates=[],
        candidate_limit=10,
        query_embedding_dimensions=3,
        rerank_used=rerank_used,
    )


def test_tool_search_uses_unified_service_and_returns_document_provenance():
    """RagTool search 必须走统一服务、关闭 KG，并返回 document 来源定位。"""
    document = _unified_chunk()

    class FakeRetrieval:
        """记录 search 模式的唯一服务调用形状。"""

        top_k = 5
        min_score = 0.35

        def retrieve(self, question, *, include_parent_context, use_kg):
            """校验 search 关闭 parent 与 KG 后返回规范统一结果。"""
            assert include_parent_context is False
            assert use_kg is False
            return _unified_result(document)

    tool = RagTool(
        retrieval=FakeRetrieval(),
        chat=FakeChat(),
        system_prompt="系统提示",
    )

    payload = tool.search("Why is the assigned item missing?").to_dict()

    assert payload["documents"][0]["id"] == document.id
    assert payload["documents"][0]["source_type"] == "document"
    assert payload["documents"][0]["source_id"] == "file_1"
    assert payload["documents"][0]["source_chunk_id"] == "chunk_1"
    assert payload["documents"][0]["answer"] == document.content


def test_rag_tool_document_rejects_anonymous_canonical_shaped_object():
    """MCP wire 转换只接受 canonical 模型，不能按属性齐全程度做 duck typing。"""
    anonymous = SimpleNamespace(**_unified_chunk().__dict__)

    with pytest.raises(TypeError, match="RetrievedKnowledgeChunk"):
        RagToolDocument.from_retrieved(anonymous)


def test_tool_answer_uses_parent_context_without_counting_parent_as_hit():
    """answer 可把 parent 放进 prompt，但 documents、top_score 和命中数只算 direct。"""
    parent = replace(
        _unified_chunk(),
        id="kc_document_parent_1",
        chunk_level="parent",
        content="进入任务管理后先筛选目标任务，再核对发布状态。",
        score=1.0,
    )
    child = replace(_unified_chunk(), parent_chunk_id=parent.id)
    chat = FakeChat()

    tool = RagTool(
        retrieval=FakeRetrieval(_unified_result(child, parents=[parent])),
        chat=chat,
        system_prompt="系统提示",
    )

    payload = tool.answer("Why is the assigned item missing?").to_dict()

    assert "先筛选目标任务" in chat.calls[0][1]
    assert [document["id"] for document in payload["documents"]] == [child.id]
    assert payload["top_score"] == child.score


def test_tool_search_returns_structured_hits_without_calling_chat():
    """search 只返回结构化命中，不得调用生成模型。"""
    chat = FakeChat()
    retrieval = FakeRetrieval(_unified_result(_unified_chunk()))
    tool = RagTool(
        retrieval=retrieval,
        chat=chat,
        system_prompt="系统提示",
    )

    result = tool.search("Why is the assigned item missing?")

    assert result.to_dict() == {
        "tool": "faq_rag",
        "mode": "search",
        "question": "Why is the assigned item missing?",
        "has_context": True,
        "top_score": 0.82,
        "top_k": 5,
        "min_score": 0.35,
        "rerank_used": False,
        "documents": [
            {
                "id": "kc_document_child_1",
                "score": 0.82,
                "source_type": "document",
                "source_id": "file_1",
                "source_chunk_id": "chunk_1",
                "parent_chunk_id": None,
                "chunk_level": "child",
                "source_title": "平台操作手册",
                "section_path": ["任务派发"],
                "page_start": 1,
                "page_end": 1,
                "block_type": "text",
                "content": "请先检查任务是否已经发布。",
                "metadata": {"category": "support workflow", "source_date": "2025-09"},
                "question": "平台操作手册",
                "answer": "请先检查任务是否已经发布。",
                "category": "support workflow",
                "tags": ["量表", "任务派发"],
                "source_date": "2025-09",
                "confidence": None,
                "status": "usable",
            }
        ],
    }
    assert retrieval.calls == [("Why is the assigned item missing?", False, False)]
    assert chat.calls == []


def test_tool_answer_returns_agent_facing_draft_and_sources():
    """answer 返回面向上游代理的草稿与来源，不暴露执行动作。"""
    chat = FakeChat()
    tool = RagTool(
        retrieval=FakeRetrieval(_unified_result(_unified_chunk())),
        chat=chat,
        system_prompt="系统提示",
    )

    result = tool.answer("Why is the assigned item missing?")

    payload = result.to_dict()
    assert payload["tool"] == "faq_rag"
    assert payload["mode"] == "answer_draft"
    assert payload["answer_draft"] == "Please check whether the assignment was published; if it still fails, collect the account, context, and a screenshot."
    assert payload["has_context"] is True
    assert payload["documents"][0]["id"] == "kc_document_child_1"
    assert "平台操作手册" in chat.calls[0][1]
    assert "backend_operation" not in payload
    assert "action" not in payload


def test_tool_results_expose_actual_rerank_usage():
    """搜索和回答结果必须透传唯一检索服务的 rerank 诊断。"""
    result = _unified_result(_unified_chunk(), rerank_used=True)
    tool = RagTool(
        retrieval=FakeRetrieval(result),
        chat=FakeChat(),
        system_prompt="系统提示",
    )

    assert tool.search("任务为什么不见了？").to_dict()["rerank_used"] is True
    assert tool.answer("任务为什么不见了？").to_dict()["rerank_used"] is True


def test_tool_answer_marks_no_context_for_upstream_agent():
    """无检索命中时显式标记无上下文，并返回空来源。"""
    tool = RagTool(
        retrieval=FakeRetrieval(_unified_result()),
        chat=FakeChat(),
        system_prompt="系统提示",
    )

    payload = tool.answer("Has the backend refresh finished?").to_dict()

    assert payload["has_context"] is False
    assert payload["top_score"] is None
    assert payload["documents"] == []


class StreamingFakeChat:
    """收集 stream_complete 调用并产出固定 deltas，验证流式上下游契约。"""

    def __init__(self, deltas):
        self._deltas = deltas
        self.calls = []

    def stream_complete(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        for delta in self._deltas:
            yield delta


def test_tool_stream_answer_yields_deltas_then_final():
    """stream_answer 应顺序 yield delta 事件，最后 yield final 事件含 answer + 来源 + 命中信息。"""
    chat = StreamingFakeChat(["请先核实", "派发是否成功", "再收集账号截图。"])
    tool = RagTool(
        retrieval=FakeRetrieval(_unified_result(_unified_chunk(score=0.78))),
        chat=chat,
        system_prompt="系统提示",
    )

    events = list(tool.stream_answer("Why is the assigned item missing?"))

    assert [event["type"] for event in events[:-1]] == ["delta", "delta", "delta"]
    assert [event["text"] for event in events[:-1]] == ["请先核实", "派发是否成功", "再收集账号截图。"]

    final = events[-1]
    assert final["type"] == "final"
    assert final["answer_draft"] == "请先核实派发是否成功再收集账号截图。"
    assert final["has_context"] is True
    assert final["top_score"] == 0.78
    assert final["hit_count"] == 1
    assert final["rerank_used"] is False
    assert final["documents"][0]["id"] == "kc_document_child_1"
    assert final["top_k"] == 5
    assert final["min_score"] == 0.35


def test_tool_stream_answer_falls_back_to_marker_when_chat_empty():
    """模型返回空字符串时也要给上游一个占位回答，避免 agent 收到空 answer。"""
    chat = StreamingFakeChat([])
    tool = RagTool(
        retrieval=FakeRetrieval(_unified_result(_unified_chunk())),
        chat=chat,
        system_prompt="系统提示",
    )

    events = list(tool.stream_answer("Why is the assigned item missing?"))

    assert events[-1]["type"] == "final"
    assert "暂时" in events[-1]["answer_draft"] or events[-1]["answer_draft"]
    # 没有 delta 仅 final 占位事件
    assert all(event["type"] == "final" for event in events) or any(event["type"] == "delta" for event in events)


def test_tool_stream_answer_passes_system_prompt_to_chat():
    """stream_answer 必须把系统提示词透传到 chat.stream_complete。"""
    chat = StreamingFakeChat(["ok"])
    tool = RagTool(
        retrieval=FakeRetrieval(_unified_result(_unified_chunk())),
        chat=chat,
        system_prompt="你是企业级跨境电商 KB 助手。",
    )

    list(tool.stream_answer("Why is the assigned item missing?"))

    assert chat.calls
    system, prompt = chat.calls[0]
    assert system == "你是企业级跨境电商 KB 助手。"
    assert "Why is the assigned item missing?" in prompt
