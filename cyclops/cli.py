from __future__ import annotations

import argparse
import json
import sys

from cyclops.config import Settings
from cyclops.db import Database
from cyclops.faq_loader import import_faqs
from cyclops.llm import ChatClient, EmbeddingClient, RerankClient
from cyclops.rag import RagService, load_system_prompt
from cyclops.rag_tool import RagTool
from cyclops.retrieval import HybridRetrievalService


def build_parser() -> argparse.ArgumentParser:
    """构建命令行入口，约束是只暴露本地维护需要的受控命令。"""
    parser = argparse.ArgumentParser(prog="cyclops")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config")
    sub.add_parser("init-db")
    import_parser = sub.add_parser("import-faq")
    import_parser.add_argument("--path", default="data/faqs.jsonl")
    search_parser = sub.add_parser("search")
    search_parser.add_argument("question", nargs="?")
    ask_parser = sub.add_parser("ask")
    ask_parser.add_argument("question", nargs="?")
    tool_search_parser = sub.add_parser("tool-search")
    tool_search_parser.add_argument("question", nargs="?")
    tool_answer_parser = sub.add_parser("tool-answer")
    tool_answer_parser.add_argument("question", nargs="?")
    sub.add_parser("wechat-login")
    sub.add_parser("wechat-service")
    admin_parser = sub.add_parser("admin")
    admin_parser.add_argument("--host", default="127.0.0.1")
    admin_parser.add_argument("--port", type=int, default=8765)
    sub.add_parser("mcp")
    return parser


def build_hybrid_retrieval(
    settings: Settings,
    *,
    database: Database | None = None,
) -> HybridRetrievalService:
    """按当前设置装配唯一混合检索服务，可复用调用方已有数据库连接池。"""
    return HybridRetrievalService(
        database=database if database is not None else Database(settings.database_url),
        embeddings=EmbeddingClient.from_settings(settings),
        rerank=RerankClient.from_settings(settings),
        top_k=settings.rag_top_k,
        min_score=settings.rag_min_score,
    )


def build_rag(settings: Settings) -> RagService:
    """装配正式回答服务，检索固定复用统一混合服务。"""
    return RagService(
        retrieval=build_hybrid_retrieval(settings),
        chat=ChatClient.from_settings(settings),
        system_prompt=load_system_prompt(),
    )


def build_rag_tool(
    settings: Settings,
    *,
    database: Database | None = None,
) -> RagTool:
    """装配 agent 工具，search 与 answer 共用唯一混合检索服务。"""
    return RagTool(
        retrieval=build_hybrid_retrieval(settings, database=database),
        chat=ChatClient.from_settings(settings),
        system_prompt=load_system_prompt(),
    )


def print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    """执行 CLI 命令，所有数据库写入都走 Database 封装。"""
    args = build_parser().parse_args(argv)
    settings = Settings.load()

    if args.command == "check-config":
        print("config ok")
        return 0
    if args.command == "init-db":
        Database(settings.database_url).init_schema()
        print("database schema ok")
        return 0
    if args.command == "import-faq":
        count = import_faqs(
            args.path,
            Database(settings.database_url),
            EmbeddingClient.from_settings(settings),
        )
        print(f"imported {count} faq rows")
        return 0
    if args.command == "search":
        question = args.question or input("question: ")
        result = build_hybrid_retrieval(settings).retrieve(
            question,
            include_parent_context=False,
            use_kg=False,
        )
        for candidate in result.candidates:
            document = candidate.document
            title = document.source_title or document.source_id
            print(f"{document.score:.2f} {document.id} {title}")
        return 0
    if args.command == "ask":
        question = args.question or input("question: ")
        print(build_rag(settings).answer(question))
        return 0
    if args.command == "tool-search":
        question = args.question or input("question: ")
        print_json(build_rag_tool(settings).search(question).to_dict())
        return 0
    if args.command == "tool-answer":
        question = args.question or input("question: ")
        print_json(build_rag_tool(settings).answer(question).to_dict())
        return 0
    if args.command == "wechat-login":
        from cyclops.wechat_service import login_wechat

        login_wechat(settings)
        return 0
    if args.command == "wechat-service":
        from cyclops.wechat_service import run_service

        run_service(settings)
        return 0
    if args.command == "admin":
        from cyclops.asgi_app import run_admin_asgi

        run_admin_asgi(settings, host=args.host, port=args.port)
        return 0
    if args.command == "mcp":
        from cyclops.mcp_server import run_stdio

        run_stdio(settings)
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
