"""MCP server 单元测试：验证 list_tools 注册、search/answer 业务逻辑、流式 progress、analytics 打点。

测试设计：不在测试里启动真实 stdio server，而是直接调用模块层暴露的纯函数
`handle_search` / `handle_answer`，把 MCP session 用 FakeSession 替身。这样可以在
不依赖 mcp.server.run() 异步事件循环的情况下覆盖业务契约。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cyclops.mcp_server import (
    MCP_TOOL_DEFINITIONS,
    build_mcp_server,
    handle_answer,
    handle_search,
    resolve_requester,
)


class FakeRagTool:
    """模拟 rag_tool.RagTool，记录 search/stream_answer 调用并返回固定结构。"""

    def __init__(self, *, search_result=None, stream_events=None):
        self._search_result = search_result
        self._stream_events = stream_events or []
        self.calls = []

    def search(self, question):
        self.calls.append(("search", question))
        return self._search_result

    def stream_answer(self, question):
        self.calls.append(("stream_answer", question))
        for event in self._stream_events:
            yield event


class FakeDatabase:
    """收集 record_query_event 调用，验证 MCP 调用是否进了 analytics。"""

    def __init__(self):
        self.recorded = []

    def record_query_event(self, event):
        self.recorded.append(event)


class FakeMcpSession:
    """记录 send_progress_notification 调用，便于断言流式 delta 推送顺序。"""

    def __init__(self):
        self.progress_calls = []

    async def send_progress_notification(self, progress_token, progress, total=None, message=None, related_request_id=None):
        self.progress_calls.append(
            {
                "progress_token": progress_token,
                "progress": progress,
                "total": total,
                "message": message,
            }
        )


def _make_search_result(documents=None, top_score=0.82, *, rerank_used=False):
    """构造 MCP 搜索结果，rerank_used 是必填的统一检索诊断。"""
    documents = documents if documents is not None else [
        {
            "id": "kc_1",
            "score": top_score,
            "question": "Why is the assigned item missing?",
            "answer": "Please check publish state.",
            "category": "ops",
            "tags": ["量表"],
            "source_date": "2025-09",
            "confidence": "high",
            "status": "usable",
        }
    ]

    payload = {
        "tool": "faq_rag",
        "mode": "search",
        "question": "Why is the assigned item missing?",
        "has_context": bool(documents),
        "top_score": top_score if documents else None,
        "top_k": 5,
        "min_score": 0.35,
        "rerank_used": rerank_used,
        "documents": documents,
    }
    return SimpleNamespace(to_dict=lambda: payload)


def test_mcp_tool_definitions_expose_search_and_answer():
    """build_mcp_server 注册的工具列表必须包含 search 和 answer，schema 含 query 字段。"""
    names = [tool["name"] for tool in MCP_TOOL_DEFINITIONS]
    assert "search" in names
    assert "answer" in names
    for tool in MCP_TOOL_DEFINITIONS:
        assert "query" in tool["inputSchema"]["properties"]
        assert "query" in tool["inputSchema"]["required"]


def test_default_mcp_services_factory_reuses_database_for_retrieval_and_analytics(
    monkeypatch,
):
    """MCP 每次调用必须让 RagTool 检索与 analytics 共用同一 Database。"""
    from cyclops.mcp_server import _default_mcp_services_factory

    settings = SimpleNamespace(database_url="postgresql://unused")
    database = object()
    rag_tool = object()
    calls = []

    monkeypatch.setattr("cyclops.mcp_server.Database", lambda database_url: database)
    monkeypatch.setattr(
        "cyclops.mcp_server.build_rag_tool",
        lambda actual, *, database: calls.append((actual, database)) or rag_tool,
    )

    actual_tool, actual_database = _default_mcp_services_factory(settings)()

    assert (actual_tool, actual_database) == (rag_tool, database)
    assert calls == [(settings, database)]


def test_resolve_requester_prefers_args_then_env_then_default():
    """requester 身份优先级：args > env > 默认（mcp / None）。"""
    args = {"requester_type": "agent", "requester_id": "listing-writer"}
    assert resolve_requester(args, env={}) == ("agent", "listing-writer")

    assert resolve_requester({}, env={"MCP_REQUESTER_TYPE": "writer-bot", "MCP_REQUESTER_ID": "wb-1"}) == ("writer-bot", "wb-1")

    assert resolve_requester({}, env={}) == ("mcp", None)

    # args 覆盖 env
    assert resolve_requester({"requester_type": "agent"}, env={"MCP_REQUESTER_TYPE": "env-bot"}) == ("agent", None)


def test_handle_search_returns_documents_and_records_event():
    """search 工具应调 rag_tool.search 拿候选并把命中信息打点到 analytics。"""
    rag_tool = FakeRagTool(search_result=_make_search_result(rerank_used=True))
    db = FakeDatabase()

    result = asyncio.run(
        handle_search(
            {"query": "Why is the assigned item missing?", "requester_type": "agent", "requester_id": "writer-1"},
            rag_tool=rag_tool,
            database=db,
            env={},
        )
    )

    assert result["hit_count"] == 1
    assert result["top_score"] == 0.82
    assert result["documents"][0]["id"] == "kc_1"
    assert rag_tool.calls == [("search", "Why is the assigned item missing?")]

    assert len(db.recorded) == 1
    event = db.recorded[0]
    assert event["query"] == "Why is the assigned item missing?"
    assert event["hit_count"] == 1
    assert event["top_score"] == 0.82
    assert "kc_1" in event["retrieved_chunk_ids"]
    assert event["requester_type"] == "agent"
    assert event["requester_id"] == "writer-1"
    assert event["rerank_used"] is True
    assert event["metadata"]["flow"] == "mcp_search"
    assert result["rerank_used"] is True


def test_handle_search_handles_no_results_without_crashing():
    """检索零命中时 record_query_event 仍要写，hit_count=0 让看板能看到。"""
    rag_tool = FakeRagTool(search_result=_make_search_result(documents=[], top_score=None))
    db = FakeDatabase()

    result = asyncio.run(
        handle_search({"query": "no idea"}, rag_tool=rag_tool, database=db, env={})
    )

    assert result["hit_count"] == 0
    assert result["top_score"] is None
    assert db.recorded[0]["hit_count"] == 0
    assert db.recorded[0]["requester_type"] == "mcp"


def test_handle_answer_streams_deltas_via_progress_notification():
    """answer 工具应把 stream_answer 的 delta 通过 progress notification 推送给客户端。"""
    rag_tool = FakeRagTool(
        stream_events=[
            {"type": "delta", "text": "请先"},
            {"type": "delta", "text": "核对"},
            {"type": "delta", "text": "派发状态。"},
            {
                "type": "final",
                "answer_draft": "请先核对派发状态。",
                "documents": [{"id": "kc_1", "score": 0.78}],
                "top_score": 0.78,
                "hit_count": 1,
                "top_k": 5,
                "min_score": 0.35,
                "rerank_used": True,
                "has_context": True,
            },
        ]
    )
    db = FakeDatabase()
    session = FakeMcpSession()

    result = asyncio.run(
        handle_answer(
            {"query": "Why is the assigned item missing?"},
            rag_tool=rag_tool,
            database=db,
            env={},
            session=session,
            progress_token="tok-1",
        )
    )

    # 三条 delta progress + 最终结果
    assert [call["message"] for call in session.progress_calls] == ["请先", "核对", "派发状态。"]
    assert all(call["progress_token"] == "tok-1" for call in session.progress_calls)
    # progress 单调递增
    progresses = [call["progress"] for call in session.progress_calls]
    assert progresses == sorted(progresses)

    assert result["answer"] == "请先核对派发状态。"
    assert result["hit_count"] == 1
    assert result["top_score"] == 0.78
    assert result["documents"][0]["id"] == "kc_1"

    # analytics 打点
    assert len(db.recorded) == 1
    event = db.recorded[0]
    assert event["query"] == "Why is the assigned item missing?"
    assert event["hit_count"] == 1
    assert event["rerank_used"] is True
    assert event["metadata"]["flow"] == "mcp_answer"
    assert result["rerank_used"] is True


def test_handle_answer_works_without_progress_token_or_session():
    """客户端没传 progressToken 也要正常返回最终结果，不抛异常。"""
    rag_tool = FakeRagTool(
        stream_events=[
            {"type": "delta", "text": "a"},
            {
                "type": "final",
                "answer_draft": "a",
                "documents": [],
                "top_score": None,
                "hit_count": 0,
                "top_k": 5,
                "min_score": 0.35,
                "rerank_used": False,
                "has_context": False,
            },
        ]
    )
    db = FakeDatabase()

    result = asyncio.run(
        handle_answer(
            {"query": "q"},
            rag_tool=rag_tool,
            database=db,
            env={},
            session=None,
            progress_token=None,
        )
    )

    assert result["answer"] == "a"
    assert result["hit_count"] == 0
    assert db.recorded[0]["hit_count"] == 0


def test_handle_answer_requires_canonical_final_event():
    """RagTool 流缺少 final 时必须报错，不得伪造一个空结果兼容非法流。"""
    rag_tool = FakeRagTool(stream_events=[{"type": "delta", "text": "partial"}])

    with pytest.raises(RuntimeError, match="final"):
        asyncio.run(
            handle_answer(
                {"query": "q"},
                rag_tool=rag_tool,
                database=FakeDatabase(),
                env={},
            )
        )


def test_handle_search_swallows_analytics_failures():
    """analytics 写入失败时主路径仍要返回正常结果。"""

    class BrokenDb:
        def record_query_event(self, event):
            raise RuntimeError("db down")

    rag_tool = FakeRagTool(search_result=_make_search_result())
    result = asyncio.run(handle_search({"query": "q"}, rag_tool=rag_tool, database=BrokenDb(), env={}))

    assert result["hit_count"] == 1


def test_build_mcp_server_registers_tool_handlers():
    """build_mcp_server 返回的 Server 实例应能列出工具，确认装饰器挂载成功。"""
    server = build_mcp_server(
        services_factory=lambda: (
            FakeRagTool(search_result=_make_search_result()),
            FakeDatabase(),
        ),
        env={},
    )

    # mcp Server 的 list_tools 处理器注册在 request_handlers
    from mcp import types as mcp_types

    assert mcp_types.ListToolsRequest in server.request_handlers
    assert mcp_types.CallToolRequest in server.request_handlers
