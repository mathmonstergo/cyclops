import json
from types import SimpleNamespace

import pytest

from cyclops.cli import build_parser, build_rag_tool, main


def test_search_command_uses_hybrid_retrieval_and_keeps_kg_disabled(monkeypatch, capsys):
    """CLI search 必须使用统一服务，且只打印 direct 候选的 canonical 标题。"""
    settings = SimpleNamespace(database_url="postgresql://unused")
    document = SimpleNamespace(
        id="kc_document_child_1",
        score=0.82,
        source_title="平台操作手册",
        source_id="file_1",
    )

    class FakeRetrieval:
        """记录 CLI search 的显式 KG 与 parent 参数。"""

        def retrieve(self, question, *, include_parent_context, use_kg):
            """校验 CLI 检索参数并返回固定候选。"""
            assert question == "报告怎么导出？"
            assert include_parent_context is False
            assert use_kg is False
            return SimpleNamespace(
                candidates=[SimpleNamespace(document=document)],
                parent_documents=[],
            )

    monkeypatch.setattr("cyclops.cli.Settings.load", lambda: settings)
    monkeypatch.setattr(
        "cyclops.cli.build_hybrid_retrieval",
        lambda actual: FakeRetrieval(),
    )

    assert main(["search", "报告怎么导出？"]) == 0
    assert capsys.readouterr().out.strip() == "0.82 kc_document_child_1 平台操作手册"


def test_build_rag_tool_reuses_caller_database_for_hybrid_retrieval(monkeypatch):
    """MCP 等入口装配 RagTool 时必须能与 analytics 共用同一 Database。"""
    settings = SimpleNamespace()
    database = object()
    retrieval = SimpleNamespace(top_k=5, min_score=0.35)
    calls = []

    monkeypatch.setattr(
        "cyclops.cli.build_hybrid_retrieval",
        lambda actual, *, database: calls.append((actual, database)) or retrieval,
    )
    monkeypatch.setattr("cyclops.cli.ChatClient.from_settings", lambda actual: object())
    monkeypatch.setattr("cyclops.cli.load_system_prompt", lambda: "系统提示")

    tool = build_rag_tool(settings, database=database)

    assert tool.retrieval is retrieval
    assert calls == [(settings, database)]


def test_parser_accepts_core_commands():
    """核心 CLI 命令必须均可被解析器识别。"""
    parser = build_parser()
    assert parser.prog == "cyclops"
    for command in [
        "check-config",
        "init-db",
        "import-faq",
        "search",
        "ask",
        "tool-search",
        "tool-answer",
        "wechat-login",
        "wechat-service",
        "admin",
    ]:
        args = parser.parse_args([command])
        assert args.command == command


def test_parser_rejects_removed_sync_knowledge_chunks_command():
    """FAQ 已随写入原子投影，CLI 不得继续暴露旧同步补偿命令。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync-knowledge-chunks"])


def test_wechat_login_dispatches_to_service(monkeypatch):
    settings = SimpleNamespace()
    called = []

    monkeypatch.setattr("cyclops.cli.Settings.load", lambda: settings)

    from cyclops import wechat_service

    monkeypatch.setattr(wechat_service, "login_wechat", lambda actual: called.append(actual))

    assert main(["wechat-login"]) == 0
    assert called == [settings]


def test_wechat_service_dispatches_to_service(monkeypatch):
    settings = SimpleNamespace()
    called = []

    monkeypatch.setattr("cyclops.cli.Settings.load", lambda: settings)

    from cyclops import wechat_service

    monkeypatch.setattr(wechat_service, "run_service", lambda actual: called.append(actual))

    assert main(["wechat-service"]) == 0
    assert called == [settings]


def test_admin_dispatches_to_asgi_runner(monkeypatch):
    """admin 命令应切换到 ASGI runner，同时保留 host/port 参数兼容现有运维脚本。"""
    settings = SimpleNamespace()
    calls = []

    monkeypatch.setattr("cyclops.cli.Settings.load", lambda: settings)

    from cyclops import asgi_app

    monkeypatch.setattr(
        asgi_app,
        "run_admin_asgi",
        lambda actual, *, host, port: calls.append((actual, host, port)),
    )

    assert main(["admin", "--host", "127.0.0.1", "--port", "8765"]) == 0
    assert calls == [(settings, "127.0.0.1", 8765)]


def test_tool_search_prints_json_for_agent(monkeypatch, capsys):
    settings = SimpleNamespace()
    monkeypatch.setattr("cyclops.cli.Settings.load", lambda: settings)

    class FakeResponse:
        def to_dict(self):
            return {
                "tool": "faq_rag",
                "mode": "search",
                "question": "Why is the item missing?",
                "documents": [],
            }

    class FakeTool:
        def search(self, question):
            assert question == "Why is the item missing?"
            return FakeResponse()

    monkeypatch.setattr("cyclops.cli.build_rag_tool", lambda actual: FakeTool())

    assert main(["tool-search", "Why is the item missing?"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "tool": "faq_rag",
        "mode": "search",
        "question": "Why is the item missing?",
        "documents": [],
    }


def test_tool_answer_prints_json_for_agent(monkeypatch, capsys):
    settings = SimpleNamespace()
    monkeypatch.setattr("cyclops.cli.Settings.load", lambda: settings)

    class FakeResponse:
        def to_dict(self):
            return {
                "tool": "faq_rag",
                "mode": "answer_draft",
                "question": "Why is the item missing?",
                "answer_draft": "Please check whether the assignment was published first.",
                "documents": [],
            }

    class FakeTool:
        def answer(self, question):
            assert question == "Why is the item missing?"
            return FakeResponse()

    monkeypatch.setattr("cyclops.cli.build_rag_tool", lambda actual: FakeTool())

    assert main(["tool-answer", "Why is the item missing?"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "tool": "faq_rag",
        "mode": "answer_draft",
        "question": "Why is the item missing?",
        "answer_draft": "Please check whether the assignment was published first.",
        "documents": [],
    }
