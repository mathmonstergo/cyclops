from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
import uuid

import requests
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from cyclops.ai_assist import AiAssistant, AiSuggestionError
from cyclops.config import DOCUMENT_CHUNKER_TYPES, Settings
from cyclops.db import (
    Database,
    KgExpandedCandidate,
    KgFactHit,
    KgReviewConflictError,
    RETRIEVAL_EVAL_BASELINE_STRATEGY,
    RETRIEVAL_EVAL_CONTRACT_VERSION,
    RETRIEVAL_EVAL_KG_DEBUG_STRATEGY,
    RetrievedKnowledgeChunk,
    build_document_knowledge_chunk_row,
    build_import_candidate_faq_row,
    document_embedding_source_fingerprint,
)
from cyclops.db.builders import child_knowledge_chunk_index, document_child_sources
from cyclops.document_kg import (
    localize_document_kg_map_result,
    premerge_document_kg_map_results,
    reduce_document_kg,
)
from cyclops.document_parser import (
    MINERU_BATCH_FILE_URL,
    MINERU_BATCH_RESULT_URL_TEMPLATE,
    MineruClient,
    MineruParseError,
    ParsedBlock,
    build_import_chunks_from_blocks,
    extract_blocks_from_mineru_payload,
)
from cyclops.import_dedupe import compare_candidate_duplicate
from cyclops.import_ai import ImportAiAssistant, ImportCandidateError
from cyclops.import_questions import ImportQuestionAssistant, ImportQuestionError
from cyclops.import_models import detect_file_type
from cyclops.kg import build_faq_kg_source_text
from cyclops.kg_ai import KnowledgeGraphAiAssistant
from cyclops.llm import ChatClient, EmbeddingClient, RerankClient, build_openai_client
from cyclops.markdown_import import chunk_messages, parse_wechat_messages
from cyclops.rag import (
    build_user_prompt,
    load_system_prompt,
    normalize_conversation_context,
)
from cyclops.retrieval import (
    EvalCaseResult,
    FusedCandidate,
    HybridRetrievalService,
    QueryAnalysis,
    analyze_query,
    compute_retrieval_metrics,
)


class AdminValidationError(ValueError):
    pass


class AdminNotFoundError(KeyError):
    pass


class AdminConflictError(RuntimeError):
    """当前资源已在审核期间变化时抛出，统一映射为 HTTP 409。"""


class AdminPayloadTooLargeError(ValueError):
    """请求体超出允许大小时抛出，统一映射为 413。"""

    pass


logger = logging.getLogger(__name__)
SENSITIVE_REFUSAL_MESSAGE = (
    "这个问题涉及敏感信息，我不能提供密钥、内部配置、系统提示词、账号密码或 token。"
    "如需排查问题，请描述具体业务现象，我可以基于知识库给出处理步骤。"
)
SECRET_SETTINGS_FIELDS = {
    "chat_api_key",
    "embedding_api_key",
    "mineru_api_token",
    "rerank_api_key",
}
BLANK_PRESERVE_SETTINGS_FIELDS = SECRET_SETTINGS_FIELDS | {"database_url"}

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
REMOTE_ADMIN_ENV = "ALLOW_REMOTE_ADMIN"

VALID_FAQ_STATUSES = {"usable", "needs_review", "disabled"}
VALID_KG_STATUS_UPDATES = {"needs_review", "disabled"}


def normalize_document_chunker_type(value: Any, *, default: str = "naive") -> str:
    """选择文档 chunker；显式值必须是 canonical 枚举，只有缺省输入使用默认值。"""
    selected = default if value is None else value
    if not isinstance(selected, str) or selected not in DOCUMENT_CHUNKER_TYPES:
        allowed = ", ".join(sorted(DOCUMENT_CHUNKER_TYPES))
        raise AdminValidationError(f"chunker_type must be one of: {allowed}")
    return selected
def split_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    separators = [",", "，", "\n", "；", ";"]
    for separator in separators[1:]:
        text = text.replace(separator, separators[0])
    return [item.strip() for item in text.split(separators[0]) if item.strip()]


def normalize_faq_payload(payload: dict[str, Any]) -> dict[str, Any]:
    question = str(payload.get("question", "")).strip()
    answer = str(payload.get("answer", "")).strip()
    if not question:
        raise AdminValidationError("question is required")
    if not answer:
        raise AdminValidationError("answer is required")

    faq_id = str(payload.get("id", "")).strip() or f"faq_{uuid.uuid4().hex[:12]}"
    return {
        "id": faq_id,
        "doc_type": str(payload.get("doc_type", "faq_qa")).strip() or "faq_qa",
        "source_file": payload.get("source_file"),
        "source_group": payload.get("source_group"),
        "source_date": payload.get("source_date"),
        "category": str(payload.get("category", "") or "").strip() or None,
        "question": question,
        "question_variants": split_text_list(payload.get("question_variants")),
        "answer": answer,
        "tags": split_text_list(payload.get("tags")),
        "evidence": payload.get("evidence", []),
        "confidence": str(payload.get("confidence", "high")).strip() or "high",
        "status": str(payload.get("status", "usable")).strip() or "usable",
        "sensitivity": payload.get("sensitivity"),
    }


def merge_existing_faq_metadata(payload: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    """保存 FAQ 时补齐前端未提交但会影响管理记录的旧字段。"""
    if existing is None:
        return payload
    merged = dict(payload)
    for key in ("source_file", "source_group", "source_date", "evidence", "confidence", "sensitivity"):
        if key not in merged:
            merged[key] = existing.get(key)
    return merged


def normalize_retrieval_eval_case_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化检索评测用例，关键约束是问题和期望命中口径必须可执行。"""
    question = str(payload.get("question", "")).strip()
    if not question:
        raise AdminValidationError("question is required")
    case_id = str(payload.get("id", "")).strip() or f"eval_{uuid.uuid4().hex[:12]}"
    return {
        "id": case_id,
        "question": question,
        "intent": str(payload.get("intent", "") or "").strip() or None,
        "expected_source_ids": split_text_list(payload.get("expected_source_ids")),
        "expected_chunk_ids": split_text_list(payload.get("expected_chunk_ids")),
        "tags": split_text_list(payload.get("tags")),
        "note": str(payload.get("note", "") or "").strip() or None,
        "status": str(payload.get("status", "active") or "active").strip() or "active",
    }


def normalize_retrieval_alias_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化检索别名词条，关键约束是标准词不能为空。"""
    canonical = str(payload.get("canonical", "")).strip()
    if not canonical:
        raise AdminValidationError("canonical is required")
    return {
        "id": str(payload.get("id", "")).strip() or f"alias_{uuid.uuid4().hex[:12]}",
        "canonical": canonical,
        "aliases": split_text_list(payload.get("aliases")),
        "tags": split_text_list(payload.get("tags")),
        "status": str(payload.get("status", "active") or "active").strip() or "active",
    }


def _parse_progress_percent(progress: dict[str, Any]) -> int:
    """根据 MinerU 页数进度计算百分比，缺少页数时按状态给默认值。"""
    try:
        total_pages = int(progress.get("total_pages") or 0)
        extracted_pages = int(progress.get("extracted_pages") or 0)
    except (TypeError, ValueError):
        total_pages = 0
        extracted_pages = 0
    if total_pages > 0:
        return min(max(round(extracted_pages * 100 / total_pages), 0), 100)
    state = str(progress.get("state") or "")
    if state in {"done", "finished", "success", "completed"}:
        return 100
    return 0


def mask_setting_secret(value: str | None) -> str:
    """脱敏展示设置页敏感值，关键约束是永不返回完整明文。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "••••••"
    return f"{text[:3]}••••••{text[-4:]}"


def mask_database_url(value: str) -> str:
    """脱敏数据库连接串中的密码，保留主机和库名用于设置卡片摘要。"""
    parsed = urlparse(value)
    if parsed.password is None:
        return value
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{username}:***@{hostname}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def merge_settings_payload_preserving_blank_secrets(
    current: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """合并设置 payload，关键约束是敏感字段空值表示保留旧配置。"""
    merged = dict(current)
    for key, value in payload.items():
        if (
            key in BLANK_PRESERVE_SETTINGS_FIELDS
            and (value is None or str(value).strip() == "")
        ):
            continue
        merged[key] = value
    return merged


def settings_payload_to_env(payload: dict[str, Any]) -> dict[str, str]:
    """把设置页 payload 规范化成运行时环境键值，并复用 Settings 校验关键约束。"""
    env_values = {
        "DATABASE_URL": str(payload.get("database_url", "")).strip(),
        "CHAT_BASE_URL": str(payload.get("chat_base_url", "")).strip(),
        "CHAT_API_KEY": str(payload.get("chat_api_key", "")).strip(),
        "CHAT_MODEL": str(payload.get("chat_model", "")).strip(),
        "EMBEDDING_BASE_URL": str(payload.get("embedding_base_url", "")).strip(),
        "EMBEDDING_API_KEY": str(payload.get("embedding_api_key", "")).strip(),
        "EMBEDDING_MODEL": str(payload.get("embedding_model", "")).strip(),
        "EMBEDDING_DIMENSIONS": str(payload.get("embedding_dimensions", "")).strip(),
        "WECHAT_TOKEN_FILE": str(payload.get("wechat_token_file", "")).strip(),
        "WECHAT_MESSAGE_CHUNK_SIZE": str(payload.get("wechat_message_chunk_size", "")).strip(),
        "RAG_TOP_K": str(payload.get("rag_top_k", "")).strip(),
        "RAG_MIN_SCORE": str(payload.get("rag_min_score", "")).strip(),
        "UPLOAD_DIR": str(payload.get("upload_dir", "")).strip(),
        "MINERU_API_MODE": "standard",
        "MINERU_API_TOKEN": str(payload.get("mineru_api_token", "")).strip(),
        "MINERU_PARSE_TIMEOUT_SECONDS": str(
            payload.get("mineru_parse_timeout_seconds", "")
        ).strip(),
        "MINERU_USE_KB_PACKAGER": "true" if payload.get("mineru_use_kb_packager") else "false",
        "DOCUMENT_CHUNK_TOKEN_NUM": str(payload.get("document_chunk_token_num", "")).strip(),
        "DOCUMENT_CHUNKER_TYPE": str(payload.get("document_chunker_type", "")).strip(),
        "DOCUMENT_CHUNK_DELIMITER": str(payload.get("document_chunk_delimiter", "")),
        "DOCUMENT_CHUNK_OVERLAP_PERCENT": str(
            payload.get("document_chunk_overlap_percent", "")
        ).strip(),
        "DOCUMENT_CHILDREN_DELIMITER": str(payload.get("document_children_delimiter", "")),
        "DOCUMENT_TABLE_CONTEXT_SIZE": str(payload.get("document_table_context_size", "")).strip(),
        "DOCUMENT_IMAGE_CONTEXT_SIZE": str(payload.get("document_image_context_size", "")).strip(),
        "DB_POOL_MIN_SIZE": str(payload.get("db_pool_min_size", "")).strip(),
        "DB_POOL_MAX_SIZE": str(payload.get("db_pool_max_size", "")).strip(),
        "CHAT_TIMEOUT_SECONDS": str(payload.get("chat_timeout_seconds", "")).strip(),
        "EMBEDDING_TIMEOUT_SECONDS": str(payload.get("embedding_timeout_seconds", "")).strip(),
        "RERANK_TIMEOUT_SECONDS": str(payload.get("rerank_timeout_seconds", "")).strip(),
        "ASSISTANT_MAX_CONCURRENT_STREAMS": str(
            payload.get("assistant_max_concurrent_streams", "")
        ).strip(),
        "IMPORT_PARSE_WORKER_POLL_INTERVAL_SECONDS": str(
            payload.get("import_parse_worker_poll_interval_seconds", "")
        ).strip(),
        "IMPORT_PARSE_WORKER_LEASE_SECONDS": str(
            payload.get("import_parse_worker_lease_seconds", "")
        ).strip(),
        "RERANK_BASE_URL": str(payload.get("rerank_base_url", "")).strip(),
        "RERANK_API_KEY": str(payload.get("rerank_api_key", "")).strip(),
        "RERANK_MODEL": str(payload.get("rerank_model", "")).strip(),
        "RERANK_INPUT_SIZE": str(payload.get("rerank_input_size", "")).strip(),
    }
    try:
        Settings.from_env(env_values)
    except Exception as exc:
        raise AdminValidationError(str(exc)) from exc
    return env_values


def settings_to_tenant_settings(settings: Settings) -> dict[str, Any]:
    """把 Settings 转成可持久化的租户配置，保留布尔和数字类型。"""
    return {
        "database_url": settings.database_url,
        "chat_base_url": settings.chat_base_url,
        "chat_api_key": settings.chat_api_key,
        "chat_model": settings.chat_model,
        "embedding_base_url": settings.embedding_base_url,
        "embedding_api_key": settings.embedding_api_key,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "wechat_token_file": str(settings.wechat_token_file),
        "wechat_message_chunk_size": settings.wechat_message_chunk_size,
        "rag_top_k": settings.rag_top_k,
        "rag_min_score": settings.rag_min_score,
        "upload_dir": str(settings.upload_dir),
        "mineru_api_token": settings.mineru_api_token or "",
        "mineru_parse_timeout_seconds": settings.mineru_parse_timeout_seconds,
        "mineru_use_kb_packager": settings.mineru_use_kb_packager,
        "document_chunk_token_num": settings.document_chunk_token_num,
        "document_chunker_type": settings.document_chunker_type,
        "document_chunk_delimiter": settings.document_chunk_delimiter,
        "document_chunk_overlap_percent": settings.document_chunk_overlap_percent,
        "document_children_delimiter": settings.document_children_delimiter,
        "document_table_context_size": settings.document_table_context_size,
        "document_image_context_size": settings.document_image_context_size,
        "db_pool_min_size": settings.db_pool_min_size,
        "db_pool_max_size": settings.db_pool_max_size,
        "chat_timeout_seconds": settings.chat_timeout_seconds,
        "embedding_timeout_seconds": settings.embedding_timeout_seconds,
        "rerank_timeout_seconds": settings.rerank_timeout_seconds,
        "assistant_max_concurrent_streams": settings.assistant_max_concurrent_streams,
        "import_parse_worker_poll_interval_seconds": (
            settings.import_parse_worker_poll_interval_seconds
        ),
        "import_parse_worker_lease_seconds": settings.import_parse_worker_lease_seconds,
        "rerank_base_url": settings.rerank_base_url,
        "rerank_api_key": settings.rerank_api_key,
        "rerank_model": settings.rerank_model,
        "rerank_input_size": settings.rerank_input_size,
    }


def write_tenant_settings(settings_file: Path, values: dict[str, Any], tenant_id: str = "default") -> None:
    """写入本地租户设置文件，保留未来多租户配置的扩展结构。"""
    payload: dict[str, Any] = {}
    if settings_file.exists():
        try:
            loaded = json.loads(settings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AdminValidationError(f"Invalid settings file: {settings_file}") from exc
        if isinstance(loaded, dict):
            payload = loaded
    tenants = payload.get("tenants")
    if not isinstance(tenants, dict):
        tenants = {}
    tenants[tenant_id] = values
    payload["version"] = 1
    payload["active_tenant_id"] = tenant_id
    payload["tenants"] = tenants
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    settings_file.chmod(0o600)


def _isoformat(value: Any) -> str | None:
    """容忍 None / 已经是字符串的列，给前端统一的 ISO 时间戳。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def safe_upload_name(filename: str) -> str:
    """清理上传文件名，只保留本地存储需要的安全字符。

    关键约束：`.` / `..` / 空基名直接返回安全占位 `upload`，避免拼接路径时被
    解释为上级目录或隐藏文件；其余只保留字母、数字、点、下划线、连字符和中文字符。
    """
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        return "upload"
    return re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name)


def ensure_loopback_or_explicit_opt_in(host: str, env: Mapping[str, str]) -> None:
    """启动守门：非 loopback host 必须显式 env 同意，避免误暴露无鉴权后台。

    关键约束：env 值严格判等 `"1"`，避免拼写造成意外放行；命中显式同意时
    在 stderr 打 warning 提醒当前架构无鉴权 + 无上传限额。
    """
    if host in LOOPBACK_HOSTS:
        return
    if env.get(REMOTE_ADMIN_ENV, "").strip() == "1":
        print(
            f"warning: admin server binding non-loopback host {host!r}; "
            "no auth or upload limits enforced — set up reverse proxy or auth before use",
            file=sys.stderr,
            flush=True,
        )
        return
    raise RuntimeError(
        f"non-loopback admin host {host!r} requires {REMOTE_ADMIN_ENV}=1; "
        "default-bind to 127.0.0.1 or set the env to acknowledge the exposure risk"
    )


def ensure_request_size(content_length: int, max_bytes: int, kind: str) -> None:
    """请求体大小守门，超限直接抛 AdminPayloadTooLargeError 防止读到内存。

    关键约束：必须在 read body 之前调用；超限时不读 body，直接走 413 响应。
    """
    if content_length > max_bytes:
        raise AdminPayloadTooLargeError(
            f"{kind} body exceeds limit: {content_length} > {max_bytes}"
        )


def classify_error_response(exc: Exception) -> tuple[HTTPStatus, dict[str, Any]]:
    """把异常分级为 HTTP 状态码 + 前端可见响应体。

    关键约束：500 类异常脱敏为固定文案 `internal error`，不暴露 raw message
    （SQL 细节、文件路径、内部 URL 等），完整堆栈走日志另写。已识别业务异常保留
    原文案，方便前端把"必填字段缺失"这类信息直接展示给用户。
    """
    if isinstance(exc, AdminNotFoundError):
        return HTTPStatus.NOT_FOUND, {"error": str(exc)}
    if isinstance(exc, AdminConflictError):
        return HTTPStatus.CONFLICT, {"error": str(exc)}
    if isinstance(exc, AdminPayloadTooLargeError):
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)}
    if isinstance(exc, AdminValidationError | AiSuggestionError | ImportCandidateError):
        return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
    return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"}


def assistant_model_error_message(exc: Exception) -> str:
    """整理回答生成模型错误，关键约束是展示外部服务原因但不暴露堆栈和长文本。"""
    raw = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            raw = str(error.get("message") or "").strip()
        if not raw:
            raw = str(body.get("message") or "").strip()
    if not raw:
        raw = str(exc).strip() or exc.__class__.__name__
    compact = re.sub(r"\s+", " ", raw)
    if len(compact) > 180:
        compact = f"{compact[:177]}..."
    return f"模型服务调用失败：{compact}"


def ensure_upload_path_within(upload_dir: Path, candidate: Path) -> Path:
    """resolve 后必须落在 upload_dir 内，覆盖符号链接或拼接穿越攻击。

    关键约束：返回 resolve 后的绝对路径供调用方使用；穿越时抛
    AdminValidationError 走 400，避免泄漏目标路径。
    """
    resolved_dir = upload_dir.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_dir)
    except ValueError as exc:
        raise AdminValidationError(
            f"upload path escapes upload_dir: {candidate.name}"
        ) from exc
    return resolved_candidate


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def format_sse_event(event: dict[str, Any]) -> str:
    """把任务进度事件格式化成浏览器 EventSource 可读的 SSE 文本。"""
    event_type = str(event.get("type", "message"))
    payload = json.dumps(jsonable(event), ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


def parse_sse_event(content: str) -> dict[str, Any]:
    """解析单个 SSE 事件块，供测试校验事件名和 JSON 数据。"""
    event_name = "message"
    data_lines: list[str] = []
    for line in content.strip().splitlines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return {"event": event_name, "data": data}


def assistant_document_payload(doc: RetrievedKnowledgeChunk) -> dict[str, Any]:
    """把 canonical 检索文档转换为问答来源结构，不接受旧对象形状。"""
    if not isinstance(doc, RetrievedKnowledgeChunk):
        raise TypeError("assistant document must be RetrievedKnowledgeChunk")
    metadata = doc.metadata
    category = metadata.get("category") or doc.source_type
    source_date = metadata.get("source_date")
    return {
        "id": doc.id,
        "source_type": doc.source_type,
        "source_id": doc.source_id,
        "source_chunk_id": doc.source_chunk_id,
        "parent_chunk_id": doc.parent_chunk_id,
        "chunk_level": doc.chunk_level,
        "source_title": doc.source_title,
        "section_path": doc.section_path,
        "page_start": doc.page_start,
        "page_end": doc.page_end,
        "block_type": doc.block_type,
        "source_offsets": doc.source_offsets,
        "content": doc.content,
        "metadata": metadata,
        "score": doc.score,
        "question": doc.source_title or doc.source_id,
        "answer": doc.content,
        "category": str(category) if category else None,
        "tags": doc.tags,
        "source_date": str(source_date) if source_date else None,
        "confidence": doc.confidence,
        "status": doc.status,
    }


def retrieval_eval_item_payload(candidate: FusedCandidate) -> dict[str, Any]:
    """序列化正式评测候选，关键约束是只接受具备完整 ID 的 FAQ/文档来源。"""
    if not isinstance(candidate, FusedCandidate):
        raise TypeError("retrieval eval item must be FusedCandidate")
    doc = candidate.document
    if not isinstance(doc, RetrievedKnowledgeChunk):
        raise TypeError("retrieval eval document must be RetrievedKnowledgeChunk")
    if doc.source_type not in {"faq", "document"}:
        raise ValueError("retrieval eval source_type must be faq or document")
    if (
        not isinstance(doc.id, str)
        or not doc.id.strip()
        or not isinstance(doc.source_id, str)
        or not doc.source_id.strip()
    ):
        raise ValueError("retrieval eval candidate requires non-empty id and source_id")
    return {
        "id": doc.id,
        "source_id": doc.source_id,
        "source_type": doc.source_type,
        "source_chunk_id": doc.source_chunk_id,
        "parent_chunk_id": doc.parent_chunk_id,
        "chunk_level": doc.chunk_level,
        "source_title": doc.source_title,
        "section_path": doc.section_path,
        "page_start": doc.page_start,
        "page_end": doc.page_end,
        "block_type": doc.block_type,
        "content": doc.content,
        "channels": list(candidate.channels),
        "fused_score": candidate.fused_score,
        "vector_score": candidate.vector_score,
        "keyword_score": candidate.keyword_score,
        "kg_score": candidate.kg_score,
        "kg_matches": [kg_fact_hit_payload(match) for match in candidate.kg_matches],
    }


def kg_fact_hit_payload(hit: KgFactHit) -> dict[str, Any]:
    """序列化 KG fact 命中，关键约束是保留合成 ID 仅作调试诊断。"""
    return {
        "fact_chunk_id": hit.fact_chunk_id,
        "fact_id": hit.fact_id,
        "fact_type": hit.fact_type,
        "fact_rank": hit.fact_rank,
        "fact_score": hit.fact_score,
    }


def kg_fact_analysis_payload(
    fact_hits: list[KgFactHit],
    kg_candidates: list[KgExpandedCandidate],
) -> list[dict[str, Any]]:
    """整理 fact 到原始候选的展开诊断，孤儿 fact 明确保留空候选列表。"""
    expanded_ids = {hit.fact_chunk_id: [] for hit in fact_hits}
    for candidate in kg_candidates:
        for match in candidate.kg_matches:
            candidate_ids = expanded_ids.get(match.fact_chunk_id)
            if candidate_ids is None:
                raise ValueError(
                    f"unknown KG fact match: {match.fact_chunk_id}"
                )
            if candidate.document.id not in candidate_ids:
                candidate_ids.append(candidate.document.id)
    return [
        {
            **kg_fact_hit_payload(hit),
            "expanded_candidate_ids": expanded_ids[hit.fact_chunk_id],
        }
        for hit in fact_hits
    ]


def document_knowledge_rows_for_embedding(chunk: dict[str, Any], import_file: dict[str, Any]) -> list[dict[str, Any]]:
    """生成文档 parent/child，与状态统计共用唯一 child 来源选择器。"""
    parent_row = build_document_knowledge_chunk_row(
        {**chunk, "retrieval_status": "usable", "chunk_level": "parent"},
        import_file,
        knowledge_chunk_id=chunk["id"],
    )
    child_texts, child_blocks = document_child_sources(chunk)
    if child_blocks:
        return _child_rows_from_blocks(chunk, import_file, parent_row, child_blocks)
    return _child_rows_from_texts(chunk, import_file, parent_row, child_texts)


def _child_rows_from_texts(
    chunk: dict[str, Any],
    import_file: dict[str, Any],
    parent_row: dict[str, Any],
    child_texts: list[str],
) -> list[dict[str, Any]]:
    """从文本子段生成 child，合成 ID 只用于知识行而不改写来源切片 ID。"""
    rows = [parent_row]
    parent_index = int(chunk.get("chunk_index", 0))
    for child_index, child_text in enumerate(child_texts, start=1):
        child_chunk = {
            **chunk,
            "source_text": child_text,
            "source_blocks": [],
            "chunk_index": child_knowledge_chunk_index(parent_index, child_index),
            "parent_chunk_id": parent_row["id"],
            "chunk_level": "child",
            "parent_content": parent_row["content"],
            "retrieval_status": "usable",
        }
        rows.append(
            build_document_knowledge_chunk_row(
                child_chunk,
                import_file,
                knowledge_chunk_id=f"{chunk['id']}_child_{child_index}",
            )
        )
    return rows


def _child_rows_from_blocks(
    chunk: dict[str, Any],
    import_file: dict[str, Any],
    parent_row: dict[str, Any],
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从结构块生成 child，合成 ID 只用于知识行而不改写来源切片 ID。"""
    rows = [parent_row]
    parent_index = int(chunk.get("chunk_index", 0))
    for child_index, block in enumerate(blocks, start=1):
        block_text = str(block.get("text") or "").strip()
        if not block_text:
            continue
        child_meta = structured_block_metadata(block, chunk)
        child_chunk = {
            **chunk,
            **child_meta,
            "source_text": block_text,
            "source_blocks": [block],
            "chunk_index": child_knowledge_chunk_index(parent_index, child_index),
            "parent_chunk_id": parent_row["id"],
            "chunk_level": "child",
            "parent_content": parent_row["content"],
            "retrieval_status": "usable",
        }
        rows.append(
            build_document_knowledge_chunk_row(
                child_chunk,
                import_file,
                knowledge_chunk_id=f"{chunk['id']}_child_{child_index}",
            )
        )
    return rows


def structured_block_metadata(block: dict[str, Any], parent_chunk: dict[str, Any]) -> dict[str, Any]:
    """从结构化来源块生成 child metadata，缺失字段继承 parent。"""
    section = str(block.get("section_title") or "").strip()
    section_path = [part.strip() for part in section.split(">") if part.strip()]
    page_number = _optional_int(block.get("page_number"))
    evidence = block.get("evidence") if isinstance(block.get("evidence"), dict) else {}
    if page_number is None:
        page_number = _optional_int(evidence.get("page_number"))
    source_offsets = {}
    position_tag = block.get("position_tag") or evidence.get("position_tag")
    if position_tag:
        source_offsets["position_tag"] = position_tag
    if evidence:
        source_offsets["evidence"] = dict(evidence)
    return {
        "section_path": section_path or parent_chunk.get("section_path") or [],
        "page_start": page_number if page_number is not None else parent_chunk.get("page_start"),
        "page_end": page_number if page_number is not None else parent_chunk.get("page_end"),
        "block_type": block.get("block_type") or parent_chunk.get("block_type"),
        "source_offsets": source_offsets or parent_chunk.get("source_offsets") or {},
    }


def _optional_int(value: Any) -> int | None:
    """把结构化块里的页码转为整数，非法值保持为空。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def assistant_step_event(
    step_id: str,
    title: str,
    status: str,
    started_at: float,
    *,
    summary: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """构造流程可视化节点事件，统一节点状态、耗时和摘要字段。"""
    return {
        "type": "step",
        "step_id": step_id,
        "title": title,
        "status": status,
        "duration_ms": max(round((time.perf_counter() - started_at) * 1000), 0),
        "summary": summary,
        **extra,
    }


@dataclass
class AdminApp:
    settings: Settings
    db: Database | None = None
    embeddings: EmbeddingClient | None = None
    chat: ChatClient | None = None
    rerank: RerankClient | None = None
    rerank_resolved: bool = False
    retrieval: HybridRetrievalService | None = None
    assistant_stream_semaphore: threading.BoundedSemaphore | None = None
    settings_file: Path = Path("data/settings.local.json")
    tenant_id: str = "default"

    def database(self) -> Database:
        """获取数据库访问对象；关键约束是 ASGI 模式下启用配置化连接池。"""
        if self.db is None:
            self.db = Database(
                self.settings.database_url,
                pool_min_size=getattr(self.settings, "db_pool_min_size", 0),
                pool_max_size=getattr(self.settings, "db_pool_max_size", 0),
            )
        return self.db

    def embedding_client(self) -> EmbeddingClient:
        if self.embeddings is None:
            self.embeddings = EmbeddingClient.from_settings(self.settings)
        return self.embeddings

    def chat_client(self) -> ChatClient:
        if self.chat is None:
            self.chat = ChatClient.from_settings(self.settings)
        return self.chat

    def _chat_client_for_payload(self, payload: dict[str, Any]) -> ChatClient:
        """支持单次请求覆盖 chat 供应商：三件套齐了就临时造一个，否则走全局默认。"""
        base_url = str(payload.get("chat_base_url") or "").strip()
        api_key = str(payload.get("chat_api_key") or "").strip()
        model = str(payload.get("chat_model") or "").strip()
        if base_url and api_key and model:
            return ChatClient(
                build_openai_client(
                    base_url,
                    api_key,
                    timeout=getattr(self.settings, "chat_timeout_seconds", 60.0),
                ),
                model=model,
            )
        return self.chat_client()

    def _resolved_chat_provider_values(self, payload: dict[str, Any]) -> tuple[str, str, str]:
        """合并临时 Chat 配置和已保存配置；关键约束是空 key 不要求前端回填明文。"""
        base_url = str(payload.get("chat_base_url") or getattr(self.settings, "chat_base_url", "") or "").strip()
        api_key = str(payload.get("chat_api_key") or getattr(self.settings, "chat_api_key", "") or "").strip()
        model = str(payload.get("chat_model") or getattr(self.settings, "chat_model", "") or "").strip()
        return base_url, api_key, model

    def probe_chat_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        """用最小 chat completion 测试一次连通性，结果以 ok/error 形式回，不抛 4xx。"""
        base_url, api_key, model = self._resolved_chat_provider_values(payload)
        if not (base_url and api_key and model):
            return {"ok": False, "error": "请先在设置页配置 base_url、api_key、model，或输入临时值"}
        try:
            started = time.perf_counter()
            client = ChatClient(build_openai_client(base_url, api_key), model=model)
            sample = client.complete("", "ping")
            return {
                "ok": True,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "model": model,
                "sample": (sample or "")[:80],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}

    def list_chat_provider_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        """调供应商的 GET /models 列出可用模型，归一化为 {items: [{id, owned_by}]}。"""
        base_url, api_key, _model = self._resolved_chat_provider_values(payload)
        if not (base_url and api_key):
            return {"items": [], "ok": False, "error": "请先在设置页配置 base_url、api_key，或输入临时值"}
        try:
            url = base_url.rstrip("/") + "/models"
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("data") if isinstance(data, dict) else None
            items: list[dict[str, str]] = []
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("id"):
                        items.append({
                            "id": str(item["id"]),
                            "owned_by": str(item.get("owned_by") or ""),
                        })
            items.sort(key=lambda m: m["id"])
            return {"ok": True, "items": items}
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            body = exc.response.text[:200] if exc.response is not None else ""
            return {"ok": False, "items": [], "error": f"HTTP {status} {body}"}
        except Exception as exc:
            return {"ok": False, "items": [], "error": str(exc) or exc.__class__.__name__}

    def rerank_client(self) -> RerankClient | None:
        """按配置返回 RerankClient；缺一项即返回 None 让上游透传。"""
        if not self.rerank_resolved:
            self.rerank = RerankClient.from_settings(self.settings)
            self.rerank_resolved = True
        return self.rerank

    def hybrid_retrieval_service(self) -> HybridRetrievalService:
        """获取唯一混合检索服务，并复用 AdminApp 管理的 DB、embedding 与 rerank。"""
        if self.retrieval is None:
            self.retrieval = HybridRetrievalService(
                database=self.database(),
                embeddings=self.embedding_client(),
                rerank=self.rerank_client(),
                top_k=getattr(self.settings, "rag_top_k", 5),
                min_score=getattr(self.settings, "rag_min_score", 0.35),
            )
        return self.retrieval

    def assistant_system_prompt(self) -> str:
        """读取智能问答系统提示词；未配置时返回空值，不再注入代码硬编码提示。"""
        try:
            return load_system_prompt()
        except FileNotFoundError:
            return ""

    def assistant_system_prompt_from_payload(self, payload: dict[str, Any]) -> str:
        """读取会话级系统提示词；为空时只回退到本地配置文件，不使用代码默认值。"""
        system_prompt = str(payload.get("system_prompt", "") or "").strip()
        return system_prompt or self.assistant_system_prompt()

    def settings_snapshot(self) -> dict[str, Any]:
        """给本地设置页返回当前运行配置，敏感字段只返回脱敏摘要。"""
        return {
            "database_url": mask_database_url(self.settings.database_url),
            "database_url_configured": bool(self.settings.database_url),
            "chat_base_url": self.settings.chat_base_url,
            "chat_api_key": mask_setting_secret(self.settings.chat_api_key),
            "chat_api_key_configured": bool(self.settings.chat_api_key),
            "chat_model": self.settings.chat_model,
            "embedding_base_url": self.settings.embedding_base_url,
            "embedding_api_key": mask_setting_secret(self.settings.embedding_api_key),
            "embedding_api_key_configured": bool(self.settings.embedding_api_key),
            "embedding_model": self.settings.embedding_model,
            "embedding_dimensions": self.settings.embedding_dimensions,
            "wechat_token_file": str(self.settings.wechat_token_file),
            "wechat_message_chunk_size": self.settings.wechat_message_chunk_size,
            "rag_top_k": self.settings.rag_top_k,
            "rag_min_score": self.settings.rag_min_score,
            "upload_dir": str(self.settings.upload_dir),
            "mineru_api_token": mask_setting_secret(self.settings.mineru_api_token),
            "mineru_api_token_configured": bool(self.settings.mineru_api_token),
            "mineru_parse_timeout_seconds": self.settings.mineru_parse_timeout_seconds,
            "mineru_use_kb_packager": self.settings.mineru_use_kb_packager,
            "document_chunk_token_num": self.settings.document_chunk_token_num,
            "document_chunker_type": self.settings.document_chunker_type,
            "document_chunk_delimiter": self.settings.document_chunk_delimiter,
            "document_chunk_overlap_percent": self.settings.document_chunk_overlap_percent,
            "document_children_delimiter": self.settings.document_children_delimiter,
            "document_table_context_size": self.settings.document_table_context_size,
            "document_image_context_size": self.settings.document_image_context_size,
            "db_pool_min_size": getattr(self.settings, "db_pool_min_size", 1),
            "db_pool_max_size": getattr(self.settings, "db_pool_max_size", 10),
            "chat_timeout_seconds": getattr(self.settings, "chat_timeout_seconds", 60.0),
            "embedding_timeout_seconds": getattr(
                self.settings,
                "embedding_timeout_seconds",
                30.0,
            ),
            "rerank_timeout_seconds": getattr(self.settings, "rerank_timeout_seconds", 30.0),
            "assistant_max_concurrent_streams": getattr(
                self.settings,
                "assistant_max_concurrent_streams",
                4,
            ),
            "import_parse_worker_poll_interval_seconds": getattr(
                self.settings,
                "import_parse_worker_poll_interval_seconds",
                1.0,
            ),
            "import_parse_worker_lease_seconds": getattr(
                self.settings,
                "import_parse_worker_lease_seconds",
                60,
            ),
            "rerank_base_url": self.settings.rerank_base_url,
            "rerank_api_key": mask_setting_secret(self.settings.rerank_api_key),
            "rerank_api_key_configured": bool(self.settings.rerank_api_key),
            "rerank_model": self.settings.rerank_model,
            "rerank_input_size": self.settings.rerank_input_size,
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存租户设置，并精确失效受变更配置影响的运行对象。"""
        merged_payload = merge_settings_payload_preserving_blank_secrets(
            settings_to_tenant_settings(self.settings),
            payload,
        )
        env_values = settings_payload_to_env(merged_payload)
        next_settings = Settings.from_env(env_values)
        database_config_changed = (
            self.settings.database_url,
            self.settings.db_pool_min_size,
            self.settings.db_pool_max_size,
        ) != (
            next_settings.database_url,
            next_settings.db_pool_min_size,
            next_settings.db_pool_max_size,
        )
        stream_limit_changed = (
            self.settings.assistant_max_concurrent_streams
            != next_settings.assistant_max_concurrent_streams
        )
        if database_config_changed and self.db is not None:
            self.db.close()
            self.db = None
        write_tenant_settings(
            self.settings_file,
            settings_to_tenant_settings(next_settings),
            tenant_id=self.tenant_id,
        )
        self.settings = next_settings
        self.embeddings = None
        self.chat = None
        self.rerank = None
        self.rerank_resolved = False
        self.retrieval = None
        if stream_limit_changed:
            self.assistant_stream_semaphore = None
        return self.settings_snapshot()

    def list_faqs(self, params: dict[str, list[str]]) -> dict[str, Any]:
        page = max(int(params.get("page", ["1"])[0]), 1)
        page_size = min(max(int(params.get("page_size", ["10"])[0]), 1), 100)
        data = self.database().list_faqs(
            query=params.get("query", [""])[0],
            status=params.get("status", [""])[0] or None,
            embedding_status=params.get("embedding_status", [""])[0] or None,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        data["page"] = page
        data["page_size"] = page_size
        return data

    def list_retrieval_eval_cases(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """列出检索评测用例，第一版供接口和脚本手工维护样本集。"""
        return self.database().list_retrieval_eval_cases(
            status=params.get("status", [""])[0] or None,
            limit=min(max(int(params.get("limit", ["50"])[0]), 1), 100),
            offset=max(int(params.get("offset", ["0"])[0]), 0),
        )

    def create_retrieval_eval_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建检索评测用例，真实客服问题先作为人工样本沉淀。"""
        row = normalize_retrieval_eval_case_payload(payload)
        return self.database().create_retrieval_eval_case(row)

    def list_retrieval_aliases(self) -> dict[str, Any]:
        """列出启用检索别名，供接口和关键词扩展复用。"""
        rows = self.database().list_retrieval_aliases()
        return {"items": rows, "total": len(rows)}

    def save_retrieval_alias(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存检索别名词条，第一版只提供最小后端接口。"""
        row = normalize_retrieval_alias_payload(payload)
        return self.database().upsert_retrieval_alias(row)

    def run_retrieval_eval_case(
        self,
        case_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """运行单条检索评测，关键约束是 KG 召回必须由 use_kg 显式开启。"""
        if not isinstance(payload, dict):
            raise AdminValidationError("retrieval eval payload must be a JSON object")
        if payload == {}:
            use_kg = False
        elif set(payload) == {"use_kg"} and payload["use_kg"] is True:
            use_kg = True
        else:
            raise AdminValidationError(
                "retrieval eval payload must be {} or contain exactly use_kg=true"
            )
        case = self.database().get_retrieval_eval_case(case_id)
        if case is None:
            raise AdminNotFoundError(f"Retrieval eval case not found: {case_id}")

        question = str(case["question"]).strip()
        analysis = analyze_query(question, self.chat_client())
        query = analysis.query_rewrite or question
        top_k = getattr(self.settings, "rag_top_k", 5)
        retrieval_result = self.hybrid_retrieval_service().retrieve(
            query,
            include_parent_context=False,
            use_kg=use_kg,
        )
        retrieved_items = [
            retrieval_eval_item_payload(candidate)
            for candidate in retrieval_result.candidates
        ]
        expected_ids = split_text_list(case.get("expected_chunk_ids")) or split_text_list(
            case.get("expected_source_ids")
        )
        retrieved_ids = [
            item["id"] if split_text_list(case.get("expected_chunk_ids")) else item["source_id"]
            for item in retrieved_items
        ]
        metrics = compute_retrieval_metrics(
            [
                EvalCaseResult(
                    question=question,
                    expected_ids=expected_ids,
                    retrieved_ids=retrieved_ids,
                )
            ],
            k=top_k,
        )
        row = {
            "case_id": case_id,
            "strategy": (
                RETRIEVAL_EVAL_KG_DEBUG_STRATEGY
                if use_kg
                else RETRIEVAL_EVAL_BASELINE_STRATEGY
            ),
            "retrieved_items": retrieved_items,
            "metrics": metrics,
            "analysis": {
                "contract_version": RETRIEVAL_EVAL_CONTRACT_VERSION,
                **analysis.to_dict(),
                "query_terms": retrieval_result.query_terms,
                "vector_count": len(retrieval_result.vector_documents),
                "keyword_count": len(retrieval_result.keyword_documents),
                "kg_fact_count": len(retrieval_result.kg_fact_hits),
                "kg_expanded_candidate_count": len(
                    retrieval_result.kg_expanded_candidates
                ),
                "kg_facts": kg_fact_analysis_payload(
                    retrieval_result.kg_fact_hits,
                    retrieval_result.kg_expanded_candidates,
                ),
                "use_kg": use_kg,
                "candidate_limit": retrieval_result.candidate_limit,
                "rerank_used": retrieval_result.rerank_used,
            },
        }
        return self.database().record_retrieval_eval_run(row)

    def kg_subgraph(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """读取 usable 子图，严格拒绝 status 等非当前查询字段。"""
        allowed_fields = {
            "center_entity_id",
            "hops",
            "entity_type",
            "relation_type",
            "limit",
        }
        unsupported_fields = set(params).difference(allowed_fields)
        if unsupported_fields:
            fields = ", ".join(sorted(unsupported_fields))
            raise AdminValidationError(f"unsupported KG subgraph query fields: {fields}")
        center_entity_id = str(params.get("center_entity_id", [""])[0]).strip()
        if not center_entity_id:
            raise AdminValidationError("center_entity_id is required")
        result = self.database().get_kg_subgraph(
            center_entity_id=center_entity_id,
            hops=min(max(self._int_param(params, "hops", 1), 1), 2),
            entity_types=split_text_list(params.get("entity_type", [""])[0]),
            relation_types=split_text_list(params.get("relation_type", [""])[0]),
            limit=min(max(self._int_param(params, "limit", 80), 1), 200),
        )
        if result is None:
            raise AdminNotFoundError(f"KG center entity not found: {center_entity_id}")
        return result

    def list_kg_entities(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """列出 KG 实体审核候选，关键约束是只做筛选分页不修改状态。"""
        return self.database().list_kg_entities(
            status=params.get("status", [""])[0] or None,
            entity_type=params.get("entity_type", [""])[0] or None,
            limit=min(max(self._int_param(params, "limit", 50), 1), 100),
            offset=max(self._int_param(params, "offset", 0), 0),
        )

    def list_kg_relations(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """列出 KG 关系审核候选，关键约束是带头尾实体和证据供人工判断。"""
        return self.database().list_kg_relations(
            status=params.get("status", [""])[0] or None,
            relation_type=params.get("relation_type", [""])[0] or None,
            limit=min(max(self._int_param(params, "limit", 50), 1), 100),
            offset=max(self._int_param(params, "offset", 0), 0),
        )

    def confirm_kg_entity(
        self,
        entity_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """确认 KG 实体；关键约束是只确认调用方实际审核的 revision。"""
        expected_revision = self._kg_expected_revision(payload)
        try:
            return {
                "item": self.database().confirm_kg_entity(
                    entity_id,
                    expected_revision=expected_revision,
                )
            }
        except KeyError as exc:
            raise AdminNotFoundError(str(exc)) from exc
        except KgReviewConflictError as exc:
            raise AdminConflictError(str(exc)) from exc
        except ValueError as exc:
            raise AdminValidationError(str(exc)) from exc

    def confirm_kg_relation(
        self,
        relation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """确认 KG 关系；关键约束是只确认调用方实际审核的 revision。"""
        expected_revision = self._kg_expected_revision(payload)
        try:
            return {
                "item": self.database().confirm_kg_relation(
                    relation_id,
                    expected_revision=expected_revision,
                )
            }
        except KeyError as exc:
            raise AdminNotFoundError(str(exc)) from exc
        except KgReviewConflictError as exc:
            raise AdminConflictError(str(exc)) from exc
        except ValueError as exc:
            raise AdminValidationError(str(exc)) from exc

    def set_kg_entity_status(self, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新 KG 实体审核状态，关键约束是状态枚举受控。"""
        status = self._kg_review_status(payload)
        try:
            return {"item": self.database().set_kg_entity_status(entity_id, status)}
        except KeyError as exc:
            raise AdminNotFoundError(str(exc)) from exc

    @staticmethod
    def _kg_expected_revision(payload: dict[str, Any]) -> int:
        """读取唯一确认 payload；关键约束是必须且只能包含正整数 expected_revision。"""
        if set(payload) != {"expected_revision"}:
            raise AdminValidationError(
                "KG confirm payload must contain exactly expected_revision"
            )
        value = payload["expected_revision"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AdminValidationError("expected_revision must be a positive integer")
        return value

    def set_kg_relation_status(self, relation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新 KG 关系审核状态，关键约束是状态枚举受控。"""
        status = self._kg_review_status(payload)
        try:
            return {"item": self.database().set_kg_relation_status(relation_id, status)}
        except KeyError as exc:
            raise AdminNotFoundError(str(exc)) from exc

    def queue_faq_kg_extraction_job(
        self,
        faq_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """要求严格空对象并创建 FAQ 资源级 queued job。"""
        self._require_empty_kg_job_payload(payload)
        try:
            return self.database().create_faq_kg_extraction_job(
                faq_id,
                model=str(getattr(self.settings, "chat_model", "") or ""),
            )
        except KeyError as exc:
            raise AdminNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise AdminValidationError(str(exc)) from exc

    def queue_document_kg_extraction_job(
        self,
        file_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """要求严格空对象并创建整篇文档父任务与 manifest items。"""
        self._require_empty_kg_job_payload(payload)
        try:
            return self.database().create_document_kg_extraction_job(
                file_id,
                model=str(getattr(self.settings, "chat_model", "") or ""),
            )
        except KeyError as exc:
            raise AdminNotFoundError(str(exc)) from exc
        except ValueError as exc:
            raise AdminValidationError(str(exc)) from exc

    def get_latest_faq_kg_extraction_job(
        self,
        faq_id: str,
    ) -> dict[str, Any] | None:
        """读取 FAQ 最近一次公开 job DTO，不接受其他来源类型。"""
        return self.database().get_latest_kg_extraction_job(
            source_type="faq",
            source_id=faq_id,
        )

    def get_latest_document_kg_extraction_job(
        self,
        file_id: str,
    ) -> dict[str, Any] | None:
        """读取整篇文档最近一次公开 job DTO，不接受 chunk ID。"""
        return self.database().get_latest_kg_extraction_job(
            source_type="document",
            source_id=file_id,
        )

    def get_kg_extraction_job(self, job_id: str) -> dict[str, Any]:
        """读取 KG 抽取任务，关键约束是不存在的任务映射为管理端 404。"""
        job = self.database().get_kg_extraction_job(job_id)
        if job is None:
            raise AdminNotFoundError(f"KG extraction job not found: {job_id}")
        return job

    def process_kg_extraction_job_step(self, job: dict[str, Any]) -> dict[str, Any]:
        """按已 claim source_type/phase 推进一步；一次调用至多一次 Chat。"""
        database = self.database()
        job_id = job["id"]
        lease_token = job["lease_token"]
        source_type = job["source_type"]
        phase = job["phase"]
        try:
            if source_type == "faq" and phase == "mapping":
                faq = database.get_faq(job["source_id"])
                if faq is None:
                    raise ValueError(f"FAQ KG source is missing: {job['source_id']}")
                if faq.get("status") != "usable":
                    raise ValueError("FAQ KG source must remain usable")
                source_text = build_faq_kg_source_text(faq)
                source = {
                    "source_type": "faq",
                    "source_id": faq["id"],
                    "source_chunk_id": None,
                    "source_title": faq.get("question"),
                    "section_path": [],
                    "page_start": None,
                    "page_end": None,
                }
                extraction = KnowledgeGraphAiAssistant(self.chat_client()).extract(
                    source_text=source_text,
                    source=source,
                )
                return database.complete_faq_kg_extraction_job(
                    job_id,
                    lease_token=lease_token,
                    extraction=extraction,
                )

            if source_type == "document" and phase == "mapping":
                item = database.load_document_kg_map_item(
                    job_id,
                    lease_token=lease_token,
                )
                if item is None:
                    raise ValueError("document KG mapping job has no pending Map item")
                extraction = KnowledgeGraphAiAssistant(self.chat_client()).extract(
                    source_text=item["source_text"],
                    source=item["source"],
                )
                map_result = localize_document_kg_map_result(
                    extraction,
                    job_id=job_id,
                    chunk_id=item["chunk_id"],
                    chunk_order=item["chunk_order"],
                )
                return database.complete_document_kg_map_item(
                    job_id,
                    item["id"],
                    lease_token=lease_token,
                    map_result=map_result,
                )

            if source_type == "document" and phase == "resolving":
                map_results = database.load_document_kg_map_results(
                    job_id,
                    lease_token=lease_token,
                )
                premerged = premerge_document_kg_map_results(map_results)
                if len(premerged["entities"]) <= 1:
                    resolution = {"groups": []}
                else:
                    resolution = KnowledgeGraphAiAssistant(
                        self.chat_client()
                    ).resolve_document_entities(entities=premerged["entities"])
                return database.save_document_kg_resolution(
                    job_id,
                    lease_token=lease_token,
                    resolution_result=resolution,
                )

            if source_type == "document" and phase == "reducing":
                map_results = database.load_document_kg_map_results(
                    job_id,
                    lease_token=lease_token,
                )
                premerged = premerge_document_kg_map_results(map_results)
                extraction = reduce_document_kg(
                    premerged,
                    job["resolution_result"],
                )
                return database.complete_document_kg_extraction_job(
                    job_id,
                    lease_token=lease_token,
                    extraction=extraction,
                )

            raise ValueError(
                f"unsupported KG extraction step: {source_type}/{phase}"
            )
        except Exception as exc:
            try:
                return database.fail_kg_extraction_job(
                    job_id,
                    lease_token=lease_token,
                    error=str(exc)[:1000],
                )
            except Exception:
                logger.error(
                    "Failed to update KG extraction job to failed: %s",
                    job_id,
                    exc_info=True,
                )
                raise

    @staticmethod
    def _require_empty_kg_job_payload(payload: dict[str, Any]) -> None:
        """校验资源级 KG 排队 payload；只允许显式 JSON 空对象。"""
        if not isinstance(payload, dict) or payload:
            raise AdminValidationError("KG extraction payload must be an empty object")

    @staticmethod
    def _kg_review_status(payload: dict[str, Any]) -> str:
        """读取 KG 状态，关键约束是只含 status 且 usable 只能走显式 confirm。"""
        if set(payload) != {"status"}:
            raise AdminValidationError("KG review payload must contain exactly status")
        raw_status = payload["status"]
        if not isinstance(raw_status, str):
            raise AdminValidationError("status must be a string")
        status = raw_status.strip()
        if status == "usable":
            raise AdminValidationError("usable status requires the confirm endpoint")
        if status not in VALID_KG_STATUS_UPDATES:
            raise AdminValidationError("status must be needs_review or disabled")
        return status

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _since_from_days(cls, days: int) -> datetime:
        return cls._utc_now() - timedelta(days=max(int(days), 0))

    @staticmethod
    def _int_param(params: dict[str, Any], key: str, default: int) -> int:
        raw = params.get(key)
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float_param(params: dict[str, Any], key: str, default: float) -> float:
        raw = params.get(key)
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def analytics_overview(self) -> dict[str, Any]:
        """看板概览：今日 / 7 日 / 30 日命中率。"""
        now = self._utc_now()
        today = now - timedelta(days=1)
        last_7d = now - timedelta(days=7)
        last_30d = now - timedelta(days=30)
        return self.database().query_analytics_overview(
            today=today, last_7d=last_7d, last_30d=last_30d
        )

    def list_top_queries(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """高频查询表。"""
        limit = self._int_param(params, "limit", 20)
        days = self._int_param(params, "days", 7)
        since = self._since_from_days(days)
        items = self.database().list_top_queries(limit=limit, since=since)
        return {"items": items, "since": since.isoformat(), "limit": limit}

    def list_zero_hit_queries(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """零命中查询表。"""
        limit = self._int_param(params, "limit", 50)
        days = self._int_param(params, "days", 7)
        since = self._since_from_days(days)
        items = self.database().list_zero_hit_queries(limit=limit, since=since)
        return {"items": items, "since": since.isoformat(), "limit": limit}

    def list_low_score_queries(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """低置信查询表。"""
        limit = self._int_param(params, "limit", 50)
        days = self._int_param(params, "days", 7)
        threshold = self._float_param(
            params, "threshold", float(getattr(self.settings, "rag_min_score", 0.35))
        )
        since = self._since_from_days(days)
        items = self.database().list_low_score_queries(
            limit=limit, since=since, threshold=threshold
        )
        return {
            "items": items,
            "since": since.isoformat(),
            "limit": limit,
            "threshold": threshold,
        }

    def list_top_referenced_chunks(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """chunk 引用频次表。"""
        limit = self._int_param(params, "limit", 20)
        days = self._int_param(params, "days", 7)
        since = self._since_from_days(days)
        items = self.database().top_referenced_chunks(limit=limit, since=since)
        return {"items": items, "since": since.isoformat(), "limit": limit}

    def query_hit_rate_timeseries(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """命中率时序，按日聚合。"""
        days = self._int_param(params, "days", 7)
        since = self._since_from_days(days)
        rows = self.database().query_hit_rate_timeseries(since=since)
        items = []
        for row in rows:
            total = int(row.get("total") or 0)
            hits = int(row.get("hits") or 0)
            hit_rate = (hits / total) if total else 0.0
            bucket = row.get("bucket")
            items.append(
                {
                    "bucket": bucket.isoformat() if hasattr(bucket, "isoformat") else str(bucket),
                    "total": total,
                    "hits": hits,
                    "hit_rate": hit_rate,
                }
            )
        return {"items": items, "since": since.isoformat()}

    def list_cluster_summaries(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """读取最近的零命中聚类摘要。"""
        limit = self._int_param(params, "limit", 20)
        items = self.database().list_cluster_summaries(limit=limit)
        formatted = []
        for row in items:
            formatted.append(
                {
                    **row,
                    "created_at": _isoformat(row.get("created_at")),
                    "period_start": _isoformat(row.get("period_start")),
                    "period_end": _isoformat(row.get("period_end")),
                }
            )
        return {"items": formatted}

    def cluster_zero_hit_queries(self, payload: dict[str, Any]) -> dict[str, Any]:
        """触发零命中 LLM 聚类：取最近 N 天的零命中查询，让 chat 给出主题分组。"""
        days = int(payload.get("days") or 7)
        limit = int(payload.get("limit") or 200)
        since = self._since_from_days(days)
        until = self._utc_now()
        queries = self.database().list_zero_hit_queries(limit=limit, since=since)
        if not queries:
            return {"items": [], "message": "no zero-hit queries in window"}
        sample = [str(row.get("query") or "").strip() for row in queries]
        sample = [item for item in sample if item][:limit]
        prompt = "\n".join(
            [
                f"下面是过去 {days} 天 {len(sample)} 条没有命中知识库的用户查询。",
                "请按主题聚类（不超过 10 类），每类给出：",
                "- cluster_label（中文短语）",
                "- suggested_content（建议补充什么内容）",
                "- representative_queries（代表性 3-5 条原文）",
                "只输出 JSON，结构 {\"clusters\": [...]}。",
                "",
                "查询列表：",
                *[f"- {item}" for item in sample],
            ]
        )
        raw = self.chat_client().complete(
            "你是企业级知识库的内容运营助手，输出中文 JSON。",
            prompt,
        )
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AdminValidationError(f"cluster response is not JSON: {exc}") from exc
        clusters = parsed.get("clusters") if isinstance(parsed, dict) else None
        if not isinstance(clusters, list):
            raise AdminValidationError("cluster response missing 'clusters' list")
        saved: list[dict[str, Any]] = []
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            label = str(cluster.get("cluster_label") or "").strip()
            if not label:
                continue
            sample_queries = [
                str(item).strip()
                for item in (cluster.get("representative_queries") or [])
                if str(item).strip()
            ]
            row = self.database().save_cluster_summary(
                {
                    "period_start": since,
                    "period_end": until,
                    "cluster_label": label,
                    "suggested_content": cluster.get("suggested_content"),
                    "event_count": len(sample_queries),
                    "sample_queries": sample_queries,
                }
            )
            saved.append(
                {
                    **row,
                    "created_at": _isoformat(row.get("created_at")),
                    "period_start": _isoformat(row.get("period_start")),
                    "period_end": _isoformat(row.get("period_end")),
                }
            )
        return {"items": saved, "total": len(saved), "window_days": days}

    def get_faq(self, faq_id: str) -> dict[str, Any]:
        row = self.database().get_faq(faq_id)
        if row is None:
            raise AdminNotFoundError(f"FAQ not found: {faq_id}")
        return row

    def save_faq(self, payload: dict[str, Any]) -> dict[str, Any]:
        faq_id = str(payload.get("id", "")).strip()
        existing = self.database().get_faq(faq_id) if faq_id else None
        row = normalize_faq_payload(merge_existing_faq_metadata(payload, existing))
        return self.database().save_faq_text(row)

    def batch_update_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """批量切换 FAQ 状态，只接受明确选择的 id 和受控状态值。"""
        raw_ids = payload.get("ids", [])
        if not isinstance(raw_ids, list):
            raise AdminValidationError("ids must be a list")
        ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        if not ids:
            raise AdminValidationError("ids is required")

        status = str(payload.get("status", "")).strip()
        if status not in VALID_FAQ_STATUSES:
            raise AdminValidationError("status must be usable, needs_review, or disabled")

        rows = self.database().update_faq_statuses(ids, status)
        return {"count": len(rows), "items": rows}

    def embed_faq(self, faq_id: str) -> dict[str, Any]:
        """按当前内容指纹生成 FAQ 向量，并由数据库原子刷新统一投影。"""
        try:
            row = self.database().prepare_faq_embedding(faq_id)
        except KeyError as exc:
            raise AdminNotFoundError(str(exc)) from exc
        content_hash = row.get("content_hash")
        if not isinstance(content_hash, str) or not content_hash:
            raise AdminValidationError("FAQ content hash is required before embedding")
        embedding_client = self.embedding_client()
        try:
            vector = embedding_client.embed(row["embedding_text"])
        except Exception as exc:
            try:
                return self.database().mark_embedding_failed(
                    faq_id,
                    str(exc),
                    expected_content_hash=content_hash,
                )
            except ValueError as conflict:
                raise AdminValidationError(str(conflict)) from conflict
        try:
            return self.database().update_faq_embedding(
                faq_id,
                vector,
                embedding_model=embedding_client.model,
                embedding_dimensions=embedding_client.dimensions,
                expected_content_hash=content_hash,
            )
        except ValueError as exc:
            raise AdminValidationError(str(exc)) from exc

    def embed_pending(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = min(max(int(payload.get("limit", 50)), 1), 200)
        results = []
        for row in self.database().list_embedding_candidates(limit=limit):
            results.append(self.embed_faq(row["id"]))
        return {"count": len(results), "items": results}

    def optimize(self, payload: dict[str, Any]) -> dict[str, Any]:
        question = str(payload.get("question", "")).strip()
        answer = str(payload.get("answer", "")).strip()
        if not question:
            raise AdminValidationError("question is required")
        if not answer:
            raise AdminValidationError("answer is required")
        return AiAssistant(self.chat_client()).optimize(question, answer).to_dict()

    def list_import_files(self, params: dict[str, list[str]]) -> dict[str, Any]:
        """列出导入文件，并把数据库最新任务整理成不含 lease 的只读 DTO。"""
        result = self.database().list_import_files(
            query=params.get("query", [""])[0],
            status=params.get("status", [""])[0] or None,
            limit=min(max(int(params.get("limit", ["50"])[0]), 1), 100),
            offset=max(int(params.get("offset", ["0"])[0]), 0),
        )
        return {
            **result,
            "items": [
                {
                    **item,
                    "parse_job": (
                        self._import_parse_job_payload(item["parse_job"])
                        if item["parse_job"] is not None
                        else None
                    ),
                }
                for item in result["items"]
            ],
        }

    def get_import_file(self, file_id: str) -> dict[str, Any]:
        """读取文件与最新持久解析任务；关键约束是 GET 不访问 provider。"""
        record = self.database().get_import_file(file_id)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        job = self.database().get_latest_import_parse_job_for_file(file_id)
        return {
            "file": {
                **record,
                "embedding_summary": self.database().get_import_file_embedding_summary(
                    file_id
                ),
            },
            "parse_job": self._import_parse_job_payload(job) if job is not None else None,
        }

    def create_import_file(
        self,
        filename: str,
        content: bytes,
        *,
        auto_parse: bool = True,
        chunker_type: str | None = None,
    ) -> dict[str, Any]:
        """保存上传原件；文档类文件记录默认 chunker，后续解析任务可按文件覆盖。"""
        if not content:
            raise AdminValidationError("uploaded file is empty")
        file_type, parser = detect_file_type(filename)
        file_id = f"imp_{uuid.uuid4().hex[:12]}"
        safe_name = safe_upload_name(filename)
        upload_dir = Path(self.settings.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_path = upload_dir / f"{file_id}_{safe_name}"
        ensure_upload_path_within(upload_dir, stored_path)
        stored_path.write_bytes(content)

        status = "pending" if parser != "unsupported" else "unsupported"
        selected_chunker = normalize_document_chunker_type(
            chunker_type,
            default=getattr(self.settings, "document_chunker_type", "naive"),
        )
        record = self.database().create_import_file(
            {
                "id": file_id,
                "original_name": safe_name,
                "stored_path": str(stored_path),
                "file_type": file_type,
                "parser": parser,
                "chunker_type": selected_chunker,
                "status": status,
            }
        )
        if auto_parse and parser in {"markdown_chat", "mineru"}:
            job = self.start_import_parse_job(
                file_id,
                {"chunker_type": selected_chunker},
            )
            return {**record, "status": "processing", "parse_job": job}
        return record

    def _build_markdown_import_chunks(
        self,
        file_id: str,
        content: bytes,
    ) -> list[dict[str, Any]]:
        """把 UTF-8 微信 Markdown 构造成当前导入切片，不执行数据库写入。"""
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdminValidationError("uploaded Markdown must be UTF-8") from exc
        messages = parse_wechat_messages(text)
        chunks = chunk_messages(messages, mode="by_days", days=1)
        return [
            {
                "id": f"chunk_{uuid.uuid4().hex[:12]}",
                "file_id": file_id,
                "chunk_index": index,
                "section_path": [],
                "page_start": None,
                "page_end": None,
                "block_type": "chat_turns",
                "source_offsets": {},
                "source_blocks": [],
                "children_delimiter": "",
                "start_at": chunk.start_at,
                "end_at": chunk.end_at,
                "message_count": chunk.message_count,
                "keywords": json.dumps(chunk.keywords, ensure_ascii=False),
                "source_text": chunk.text,
                "status": "pending",
                "candidate_count": 0,
            }
            for index, chunk in enumerate(chunks, start=1)
        ]

    def _mineru_client(self, import_file_id: str | None = None) -> MineruClient:
        """创建 MinerU 客户端，关键约束是资产按导入文件隔离存储。"""
        asset_output_dir = Path(self.settings.upload_dir) / "mineru-assets"
        if import_file_id:
            asset_output_dir = asset_output_dir / safe_upload_name(import_file_id)
        return MineruClient(
            api_token=getattr(self.settings, "mineru_api_token", None),
            batch_file_url=MINERU_BATCH_FILE_URL,
            batch_result_url_template=MINERU_BATCH_RESULT_URL_TEMPLATE,
            timeout_seconds=self.settings.mineru_parse_timeout_seconds,
            use_kb_packager=getattr(self.settings, "mineru_use_kb_packager", True),
            asset_output_dir=asset_output_dir,
        )

    def start_import_parse_job(self, file_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """只创建 queued 解析任务，provider 调用统一由持久 worker 执行。"""
        if set(payload) != {"chunker_type"}:
            raise AdminValidationError("parse job body must contain only chunker_type")
        record = self.database().get_import_file(file_id)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        if record["parser"] not in {"markdown_chat", "mineru"}:
            raise AdminValidationError("only Markdown chat or MinerU files can be parsed")
        chunker_type = payload["chunker_type"]
        if not isinstance(chunker_type, str) or chunker_type not in DOCUMENT_CHUNKER_TYPES:
            allowed = ", ".join(sorted(DOCUMENT_CHUNKER_TYPES))
            raise AdminValidationError(f"chunker_type must be one of: {allowed}")
        stored_path = Path(record["stored_path"])
        if not stored_path.exists() or not stored_path.is_file():
            raise AdminValidationError("stored upload file is missing")
        input_fingerprint = self._import_parse_input_fingerprint(
            record,
            stored_path,
            chunker_type=chunker_type,
        )
        try:
            job = self.database().create_import_parse_job(
                file_id,
                chunker_type=chunker_type,
                input_fingerprint=input_fingerprint,
            )
        except ValueError as exc:
            if "active import parse job" in str(exc):
                raise AdminConflictError(str(exc)) from exc
            raise AdminValidationError(str(exc)) from exc
        return self._import_parse_job_payload(job)

    def get_import_parse_job(self, job_id: str) -> dict[str, Any]:
        """按 ID 只读持久解析任务；关键约束是读取不推进 provider。"""
        job = self.database().get_import_parse_job(job_id)
        if job is None:
            raise AdminNotFoundError(f"Import parse job not found: {job_id}")
        return self._import_parse_job_payload(job)

    def process_import_parse_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """推进一个已领取任务；业务阶段只以数据库 job 状态为真相。"""
        job_id = job["id"]
        lease_token = job["lease_token"]
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("claimed import parse job requires lease_token")
        try:
            return self._process_import_parse_job_claim(job, lease_token=lease_token)
        except Exception as exc:
            error = (str(exc).strip() or exc.__class__.__name__)[:1000]
            try:
                return self.database().fail_import_parse_job(
                    job_id,
                    lease_token=lease_token,
                    error=error,
                )
            except Exception:
                logger.exception("failed to persist import parse job error: %s", job_id)
                raise

    def _process_import_parse_job_claim(
        self,
        job: dict[str, Any],
        *,
        lease_token: str,
    ) -> dict[str, Any]:
        """按 submitting/polling/finalizing 三阶段推进当前 claim。"""
        record = self.database().get_import_file(job["file_id"])
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {job['file_id']}")
        stored_path = Path(record["stored_path"])
        if not stored_path.exists() or not stored_path.is_file():
            raise AdminValidationError("stored upload file is missing")
        live_fingerprint = self._import_parse_input_fingerprint(
            record,
            stored_path,
            chunker_type=job["chunker_type"],
        )
        if live_fingerprint != job["input_fingerprint"]:
            raise AdminConflictError("import parse input fingerprint changed")

        if job["status"] == "submitting":
            if record["parser"] == "markdown_chat":
                chunks = self._build_markdown_import_chunks(
                    record["id"],
                    stored_path.read_bytes(),
                )
                return self.database().complete_import_parse_job(
                    job["id"],
                    lease_token=lease_token,
                    input_fingerprint=self._import_parse_input_fingerprint(
                        record,
                        stored_path,
                        chunker_type=job["chunker_type"],
                    ),
                    chunks=chunks,
                    progress={"state": "completed", "percent": 100},
                )
            if record["parser"] != "mineru":
                raise AdminValidationError("unsupported import parser")
            status = self._mineru_client(record["id"]).start_file(stored_path)
            return self.database().update_import_parse_job_progress(
                job["id"],
                lease_token=lease_token,
                status="polling",
                progress=self._mineru_progress_payload(status),
                provider_batch_id=status.batch_id,
                provider_file_name=status.file_name,
                next_poll_at=self._next_import_parse_poll_at(),
            )

        if job["status"] not in {"polling", "finalizing"}:
            raise ValueError(f"unsupported import parse job status: {job['status']}")
        batch_id = job["provider_batch_id"]
        file_name = job["provider_file_name"]
        if not isinstance(batch_id, str) or not batch_id:
            raise ValueError("polling import parse job requires provider_batch_id")
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("polling import parse job requires provider_file_name")
        status = self._mineru_client(record["id"]).get_task_status(batch_id, file_name)
        progress = self._mineru_progress_payload(status)
        provider_state = str(status.state).lower()
        if provider_state in {"failed", "error", "cancelled", "canceled"}:
            raise MineruParseError(status.error or "MinerU parse failed")
        if provider_state not in {"done", "finished", "success", "completed"}:
            if job["status"] == "finalizing":
                raise MineruParseError("MinerU finalizing result is not ready")
            return self.database().update_import_parse_job_progress(
                job["id"],
                lease_token=lease_token,
                status="polling",
                progress=progress,
                provider_batch_id=batch_id,
                provider_file_name=file_name,
                next_poll_at=self._next_import_parse_poll_at(),
            )
        finalizing_job = self.database().begin_import_parse_job_finalization(
            job["id"],
            lease_token=lease_token,
            progress=progress,
        )
        payload = self._mineru_client(record["id"]).download_task_result(status)
        blocks = extract_blocks_from_mineru_payload(
            payload,
            source_file=record["original_name"],
            use_kb_packager=getattr(self.settings, "mineru_use_kb_packager", True),
        )
        chunks = self._build_document_import_chunks(
            record["id"],
            blocks,
            chunker_type=finalizing_job["chunker_type"],
        )
        completed_progress = {**progress, "state": "completed", "percent": 100}
        return self.database().complete_import_parse_job(
            job["id"],
            lease_token=lease_token,
            input_fingerprint=self._import_parse_input_fingerprint(
                record,
                stored_path,
                chunker_type=finalizing_job["chunker_type"],
            ),
            chunks=chunks,
            progress=completed_progress,
        )

    def _build_document_import_chunks(
        self,
        file_id: str,
        blocks: list[ParsedBlock],
        *,
        chunker_type: str,
    ) -> list[dict[str, Any]]:
        """按文件持久化 chunker 生成审核切片，禁止在构建阶段补默认值。"""
        if not isinstance(chunker_type, str) or chunker_type not in DOCUMENT_CHUNKER_TYPES:
            allowed = ", ".join(sorted(DOCUMENT_CHUNKER_TYPES))
            raise AdminValidationError(f"chunker_type must be one of: {allowed}")
        return build_import_chunks_from_blocks(
            file_id,
            blocks,
            chunk_token_num=getattr(self.settings, "document_chunk_token_num", 512),
            chunker_type=chunker_type,
            delimiter=getattr(self.settings, "document_chunk_delimiter", "\n。；！？"),
            overlapped_percent=getattr(self.settings, "document_chunk_overlap_percent", 0),
            children_delimiter=getattr(self.settings, "document_children_delimiter", ""),
            table_context_size=getattr(self.settings, "document_table_context_size", 0),
            image_context_size=getattr(self.settings, "document_image_context_size", 0),
        )

    def _mineru_progress_payload(self, status: Any) -> dict[str, Any]:
        """把 MinerU 原始进度整理成前端可直接消费的 JSON。"""
        progress = dict(getattr(status, "progress", None) or {})
        progress["state"] = getattr(status, "state", None) or progress.get("state") or "pending"
        return progress

    def _import_parse_input_fingerprint(
        self,
        record: dict[str, Any],
        stored_path: Path,
        *,
        chunker_type: str,
    ) -> str:
        """计算文件内容与解析路线指纹，阻止迟到任务覆盖变化后的来源。"""
        parser = record["parser"]
        if not isinstance(parser, str) or not parser:
            raise AdminValidationError("import parser is required")
        selected_chunker = normalize_document_chunker_type(chunker_type)
        parse_contract: dict[str, Any] = {
            "parser": parser,
            "chunker_type": selected_chunker,
        }
        if parser == "mineru":
            parse_contract.update(
                {
                    "mineru_use_kb_packager": getattr(
                        self.settings,
                        "mineru_use_kb_packager",
                        True,
                    ),
                    "document_chunk_token_num": getattr(
                        self.settings,
                        "document_chunk_token_num",
                        512,
                    ),
                    "document_chunk_delimiter": getattr(
                        self.settings,
                        "document_chunk_delimiter",
                        "\n。；！？",
                    ),
                    "document_chunk_overlap_percent": getattr(
                        self.settings,
                        "document_chunk_overlap_percent",
                        0,
                    ),
                    "document_children_delimiter": getattr(
                        self.settings,
                        "document_children_delimiter",
                        "",
                    ),
                    "document_table_context_size": getattr(
                        self.settings,
                        "document_table_context_size",
                        0,
                    ),
                    "document_image_context_size": getattr(
                        self.settings,
                        "document_image_context_size",
                        0,
                    ),
                }
            )
        digest = hashlib.sha256()
        digest.update(b"cyclops-import-parse-v2\0")
        digest.update(
            json.dumps(
                parse_contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\0")
        with stored_path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return f"sha256:{digest.hexdigest()}"

    def _next_import_parse_poll_at(self) -> datetime:
        """计算 provider 下一次轮询时间，与 worker 扫描间隔保持同一配置。"""
        interval = float(
            getattr(self.settings, "import_parse_worker_poll_interval_seconds", 1.0)
        )
        if interval <= 0:
            raise ValueError("import parse worker poll interval must be positive")
        return datetime.now(timezone.utc) + timedelta(seconds=interval)

    @staticmethod
    def _import_parse_job_payload(job: dict[str, Any]) -> dict[str, Any]:
        """序列化解析任务并移除 lease；缺字段或标量 progress 必须失败。"""
        progress = job["progress"]
        if not isinstance(progress, dict):
            raise TypeError("progress must be a JSON object")
        return {
            "id": job["id"],
            "file_id": job["file_id"],
            "status": job["status"],
            "chunker_type": job["chunker_type"],
            "input_fingerprint": job["input_fingerprint"],
            "provider_batch_id": job["provider_batch_id"],
            "provider_file_name": job["provider_file_name"],
            "progress": dict(progress),
            "percent": _parse_progress_percent(progress),
            "error": job["error"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }

    def list_import_chunks(self, file_id: str) -> dict[str, Any]:
        """返回某个导入文件的时间切块列表。"""
        return {"items": self.database().list_import_chunks(file_id)}

    def update_import_chunk_text(self, chunk_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """保存用户编辑后的切片原文，并返回文档向量状态摘要。"""
        source_text = str(payload.get("source_text", "")).strip()
        if not source_text:
            raise AdminValidationError("source_text is required")
        item = self.database().update_import_chunk_text(chunk_id, source_text)
        return {
            "item": item,
            "embedding_summary": self.database().get_import_file_embedding_summary(item["file_id"]),
        }

    def embed_import_file(self, file_id: str) -> dict[str, Any]:
        """把解析完成的文档切片生成向量并写入统一知识单元表。"""
        record = self.database().get_import_file(file_id)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        if record.get("is_disabled"):
            raise AdminValidationError("disabled import file cannot be embedded")
        if record.get("status") not in {"needs_review", "completed"}:
            raise AdminValidationError("document must be parsed before embedding")
        chunks = self.database().list_import_chunks(file_id)
        if not chunks:
            raise AdminValidationError("parsed document has no chunks")

        # 只嵌"非绿"切片：跳过已索引(ready)与已禁用切片，避免重复消耗 embedding 调用。
        pending_chunks = [
            chunk
            for chunk in chunks
            if not chunk.get("is_disabled") and chunk.get("embedding_status") != "ready"
        ]

        embedding_client = self.embedding_client()
        rows = []
        for chunk in pending_chunks:
            rows.extend(self._embed_document_chunk_rows(chunk, record, embedding_client))
        return {
            "file_id": file_id,
            "count": len(rows),
            "items": rows,
            "embedding_summary": self.database().get_import_file_embedding_summary(file_id),
        }

    def embed_import_chunk(self, chunk_id: str) -> dict[str, Any]:
        """对单个切片重新生成向量，不影响同文档其他切片。
        典型场景是编辑切片原文后 embedding 被标记为 stale，用户单独刷新这一片。
        """
        chunk = self.database().get_import_chunk(chunk_id)
        if chunk is None:
            raise AdminNotFoundError(f"Import chunk not found: {chunk_id}")
        if chunk.get("is_disabled"):
            raise AdminValidationError("disabled import chunk cannot be embedded")
        record = self.database().get_import_file(chunk["file_id"])
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {chunk['file_id']}")
        if record.get("is_disabled"):
            raise AdminValidationError("disabled import file cannot be embedded")

        embedding_client = self.embedding_client()
        rows = self._embed_document_chunk_rows(chunk, record, embedding_client)
        return {
            "chunk_id": chunk_id,
            "file_id": chunk["file_id"],
            "count": len(rows),
            "items": rows,
            "embedding_summary": self.database().get_import_file_embedding_summary(chunk["file_id"]),
            "messages": [f"已重新生成切片向量 ({len(rows)} 条)"],
        }

    def _embed_document_chunk_rows(
        self,
        chunk: dict[str, Any],
        import_file: dict[str, Any],
        embedding_client: Any,
    ) -> list[dict[str, Any]]:
        """生成一个来源切片的全部向量后原子提交，关键约束是 provider 调用期间不持有数据库锁。"""
        source_fingerprint = document_embedding_source_fingerprint(import_file, chunk)
        chunk_rows = document_knowledge_rows_for_embedding(chunk, import_file)
        items = [
            (row, embedding_client.embed(row["embedding_text"]))
            for row in chunk_rows
        ]
        try:
            return self.database().replace_document_chunk_embeddings(
                file_id=import_file["id"],
                chunk_id=chunk["id"],
                source_fingerprint=source_fingerprint,
                items=items,
                embedding_model=embedding_client.model,
                embedding_dimensions=embedding_client.dimensions,
            )
        except ValueError as exc:
            raise AdminValidationError(str(exc)) from exc

    def get_import_file_for_download(self, file_id: str) -> tuple[dict[str, Any], Path]:
        """返回可下载的原件路径，关键约束是必须来自已登记导入文件。"""
        record = self.database().get_import_file(file_id)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        stored_path = Path(record["stored_path"])
        if not stored_path.exists():
            raise AdminValidationError("stored upload file is missing")
        return record, stored_path

    def get_import_asset(self, file_id: str, asset_relpath: str) -> tuple[dict[str, Any], Path]:
        """返回 MinerU 资产文件（image / table_img / equation_img）的本地路径。

        关键约束：拼出的最终路径必须落在 `<upload_dir>/mineru-assets/<safe(file_id)>/` 之内，
        防止 `../` 逃逸；文件不存在或对应 import_file 没登记都报 404。
        """
        record = self.database().get_import_file(file_id)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        asset_root = Path(self.settings.upload_dir) / "mineru-assets" / safe_upload_name(file_id)
        if not str(asset_relpath or "").strip():
            raise AdminValidationError("asset path is required")
        candidate = asset_root / asset_relpath
        resolved = ensure_upload_path_within(asset_root, candidate)
        if not resolved.exists() or not resolved.is_file():
            raise AdminNotFoundError(f"Asset not found: {asset_relpath}")
        return record, resolved

    def delete_import_file(self, file_id: str) -> dict[str, Any]:
        """删除导入文件记录和本地原件，数据库级联清理切片与候选 FAQ。

        把要展示给用户的提示文案在后端组装好放在 `messages` 数组中返回；
        前端不解析业务字段，只对 messages 数组逐条调通用 toast，保持 UI 与业务解耦。
        """
        record = self.database().delete_import_file(file_id)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        stored_path = Path(record.get("stored_path") or "")
        if stored_path.exists():
            stored_path.unlink()
        chunk_count = int(record.get("_deleted_chunk_count") or 0)
        vector_count = int(record.get("_deleted_vector_count") or 0)
        messages = ["已删除文件原件"]
        if chunk_count > 0:
            messages.append(f"已清理文档切片 {chunk_count} 个")
        if vector_count > 0:
            messages.append(f"已清理向量索引 {vector_count} 条")
        return {
            "deleted": True,
            "id": file_id,
            "messages": messages,
        }

    def set_import_file_disabled(
        self, file_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """切换文件级禁用开关，RAG 检索立即跳过该文件下所有切片。"""
        if "is_disabled" not in payload:
            raise AdminValidationError("is_disabled is required")
        is_disabled = bool(payload.get("is_disabled"))
        record = self.database().set_import_file_disabled(file_id, is_disabled)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        return {"item": record}

    def set_import_chunk_disabled(
        self, chunk_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """切换切片级禁用开关，仅作用于单个切片。"""
        if "is_disabled" not in payload:
            raise AdminValidationError("is_disabled is required")
        is_disabled = bool(payload.get("is_disabled"))
        record = self.database().set_import_chunk_disabled(chunk_id, is_disabled)
        if record is None:
            raise AdminNotFoundError(f"Import chunk not found: {chunk_id}")
        return {"item": record}

    def generate_import_file_questions(
        self, file_id: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """批量为文档每个切片生成假设性用户问题，落库 questions 字段。

        - 默认跳过已 `ready` 的切片；payload 传 `force=True` 时全部重算
        - 单条失败写 `questions_error` 不阻塞其余切片
        - 返回 messages 数组供前端 toast 逐条弹出（按通用 UI 约定，后端拼好文案）
        """
        force = bool((payload or {}).get("force"))
        record = self.database().get_import_file(file_id)
        if record is None:
            raise AdminNotFoundError(f"Import file not found: {file_id}")
        chunks = self.database().list_import_chunks(file_id)
        if not chunks:
            raise AdminValidationError("parsed document has no chunks")

        assistant = ImportQuestionAssistant(self.chat_client())
        ready = 0
        skipped = 0
        failed = 0
        source_title = str(record.get("original_name") or "").strip()
        for chunk in chunks:
            if not force and str(chunk.get("questions_status") or "") == "ready":
                skipped += 1
                continue
            source_text = str(chunk.get("source_text") or "").strip()
            if not source_text:
                # 没有正文（极端：只有图片资产的切片）— 标记 skipped 不算失败
                self.database().set_import_chunk_questions(
                    chunk["id"], [], model=assistant.model, status="skipped"
                )
                skipped += 1
                continue
            existing_questions = list(chunk.get("questions") or [])
            # pending 只表示生成进度，必须保留现有问题，避免状态切换误伤可用向量。
            self.database().set_import_chunk_questions(
                chunk["id"],
                existing_questions,
                model=assistant.model,
                status="pending",
            )
            try:
                questions = assistant.generate_questions(
                    source_text=source_text,
                    section_path=list(chunk.get("section_path") or []),
                    source_title=source_title,
                    block_type=str(chunk.get("block_type") or "") or None,
                )
            except ImportQuestionError as exc:
                self.database().set_import_chunk_questions(
                    chunk["id"],
                    existing_questions,
                    model=assistant.model,
                    status="failed",
                    error=str(exc),
                )
                failed += 1
                continue
            self.database().set_import_chunk_questions(
                chunk["id"], questions, model=assistant.model, status="ready"
            )
            ready += 1

        messages: list[str] = []
        if ready:
            messages.append(f"已为 {ready} 个切片生成假设问题")
        if skipped:
            messages.append(f"跳过 {skipped} 个切片（已生成或无正文）")
        if failed:
            messages.append(f"{failed} 个切片生成失败")
        if not messages:
            messages.append("没有需要处理的切片")
        return {
            "file_id": file_id,
            "ready": ready,
            "skipped": skipped,
            "failed": failed,
            "messages": messages,
        }

    def list_import_candidates(self, chunk_id: str) -> dict[str, Any]:
        """返回某个切块下的候选 FAQ 列表。"""
        return {"items": self.database().list_import_candidates(chunk_id)}

    def list_import_file_candidates(self, file_id: str) -> dict[str, Any]:
        """返回某个导入文件下的全部候选 FAQ，供文件级审核视图使用。"""
        return {"items": self.database().list_import_file_candidates(file_id)}

    def generate_import_candidates(self, chunk_id: str) -> dict[str, Any]:
        """调用 AI 为切块生成候选 FAQ，结果仍需人工审核。"""
        chunk = self.database().get_import_chunk(chunk_id)
        if chunk is None:
            raise AdminNotFoundError(f"Import chunk not found: {chunk_id}")
        suggestions = ImportAiAssistant(self.chat_client()).generate_candidates(chunk["source_text"])
        rows = []
        for suggestion in suggestions:
            duplicate = compare_candidate_duplicate(
                {
                    "question": suggestion.question,
                    "answer": suggestion.answer,
                },
                self.database().list_import_dedupe_references(chunk["id"]),
            )
            rows.append(
                {
                    "id": f"cand_{uuid.uuid4().hex[:12]}",
                    "file_id": chunk["file_id"],
                    "chunk_id": chunk["id"],
                    "question": suggestion.question,
                    "answer": suggestion.answer,
                    "similar_questions": json.dumps(suggestion.similar_questions, ensure_ascii=False),
                    "category": suggestion.category,
                    "tags": json.dumps(suggestion.tags, ensure_ascii=False),
                    "confidence": suggestion.confidence,
                    "internal_note": suggestion.internal_note,
                    "source_excerpt": str(chunk["source_text"])[:1200],
                    "duplicate_level": duplicate.level,
                    "duplicate_score": duplicate.score,
                    "duplicate_target_id": duplicate.target_id,
                    "duplicate_reason": duplicate.reason,
                    "status": "pending",
                }
            )
        return {"items": self.database().create_import_candidates(chunk, rows)}

    def create_import_generation_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建批量候选生成任务，去重切块 id 后交给数据库做幂等判断。"""
        raw_ids = payload.get("chunk_ids", [])
        if not isinstance(raw_ids, list):
            raise AdminValidationError("chunk_ids must be a list")
        chunk_ids = list(dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip()))
        if not chunk_ids:
            raise AdminValidationError("chunk_ids is required")
        return self.database().create_import_generation_job(chunk_ids)

    def iter_import_generation_events(self, job_id: str):
        """顺序执行生成任务并产出可用于 SSE 的进度事件。"""
        job = self.database().get_import_generation_job(job_id)
        if job is None:
            raise AdminNotFoundError(f"Import generation job not found: {job_id}")
        for item in self.database().list_import_generation_job_items(job_id):
            if item["status"] == "skipped":
                yield {
                    "type": "skipped",
                    "job_id": job_id,
                    "chunk_id": item["chunk_id"],
                    "reason": item.get("reason"),
                }
                continue
            if item["status"] != "queued":
                continue
            self.database().update_import_generation_job_item(item["id"], status="processing")
            yield {"type": "processing", "job_id": job_id, "chunk_id": item["chunk_id"]}
            try:
                result = self.generate_import_candidates(item["chunk_id"])
                candidate_count = len(result["items"])
                self.database().update_import_generation_job_item(
                    item["id"],
                    status="generated",
                    candidate_count=candidate_count,
                    error=None,
                )
                yield {
                    "type": "generated",
                    "job_id": job_id,
                    "chunk_id": item["chunk_id"],
                    "candidate_count": candidate_count,
                }
            except Exception as exc:
                self.database().update_import_generation_job_item(
                    item["id"],
                    status="failed",
                    error=str(exc),
                )
                yield {
                    "type": "failed",
                    "job_id": job_id,
                    "chunk_id": item["chunk_id"],
                    "error": str(exc),
                }
        self.database().update_import_generation_job_summary(job_id, "completed")
        yield {"type": "done", "job_id": job_id}

    def update_import_candidate(self, candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """保存用户对候选 FAQ 的人工编辑。"""
        row = {
            "question": str(payload.get("question", "")).strip(),
            "answer": str(payload.get("answer", "")).strip(),
            "similar_questions": split_text_list(payload.get("similar_questions")),
            "category": str(payload.get("category", "") or "").strip() or None,
            "tags": split_text_list(payload.get("tags")),
            "confidence": str(payload.get("confidence", "medium")).strip() or "medium",
            "internal_note": str(payload.get("internal_note", "") or "").strip() or None,
        }
        if not row["question"] or not row["answer"]:
            raise AdminValidationError("candidate question and answer are required")
        return self.database().update_import_candidate(candidate_id, row)

    def save_import_candidate(self, candidate_id: str) -> dict[str, Any]:
        """将候选保存为待审核 FAQ，关键约束是向量生成保持独立显式步骤。"""
        candidate = self.database().get_import_candidate(candidate_id)
        if candidate is None:
            raise AdminNotFoundError(f"Import candidate not found: {candidate_id}")
        faq_row = build_import_candidate_faq_row(candidate)
        saved = self.database().save_faq_text(faq_row)
        marked = self.database().mark_import_candidate_saved(candidate_id, saved["id"])
        return {
            **marked,
            "embedding_status": saved["embedding_status"],
            "embedding_error": saved["embedding_error"],
        }

    def ignore_import_candidate(self, candidate_id: str) -> dict[str, Any]:
        """忽略不适合沉淀为知识库的候选 FAQ。"""
        return self.database().mark_import_candidate_ignored(candidate_id)

    def iter_assistant_chat_events(self, payload: dict[str, Any]):
        """带并发保护的问答流；关键约束是饱和时不进入检索或模型调用。"""
        guard = self._assistant_stream_guard()
        if not guard.acquire(blocking=False):
            yield {"type": "error", "error": "当前问答服务繁忙，请稍后重试。"}
            return
        try:
            yield from self._iter_assistant_chat_events_unlimited(payload)
        finally:
            guard.release()

    def _assistant_stream_guard(self) -> threading.BoundedSemaphore:
        """返回问答流并发信号量；关键约束是按 Settings 懒加载，便于测试注入。"""
        if self.assistant_stream_semaphore is None:
            limit = max(1, int(getattr(self.settings, "assistant_max_concurrent_streams", 4) or 4))
            self.assistant_stream_semaphore = threading.BoundedSemaphore(value=limit)
        return self.assistant_stream_semaphore

    def _iter_assistant_chat_events_unlimited(self, payload: dict[str, Any]):
        """执行基础 RAG 问答并产出流式事件，当前链路包含意图识别和混合召回。"""
        question = str(payload.get("question", "")).strip()
        if not question:
            raise AdminValidationError("question is required")
        flow_id = str(payload.get("flow_id", "basic_rag")).strip() or "basic_rag"
        if flow_id != "basic_rag":
            raise AdminValidationError("only basic_rag flow is supported")

        available_nodes = [
            "input_question",
            "query_embedding",
            "intent_detection",
            "vector_search",
            "keyword_search",
            "hybrid_retrieval",
            "kg_query",
            "rerank",
            "source_context",
            "answer_generation",
            "quality_check",
        ]
        yield {
            "type": "meta",
            "flow_id": "basic_rag",
            "flow_name": "基础 RAG",
            "stream": True,
            "available_nodes": available_nodes,
            "enabled_nodes": [
                "input_question",
                "intent_detection",
                "query_embedding",
                "vector_search",
                "keyword_search",
                "hybrid_retrieval",
                "source_context",
                "answer_generation",
            ],
        }

        started = time.perf_counter()
        chat = self._chat_client_for_payload(payload)
        yield assistant_step_event(
            "input_question",
            "输入问题",
            "completed",
            started,
            summary=question,
        )

        intent_started = time.perf_counter()
        analysis = analyze_query(question, chat)
        yield assistant_step_event(
            "intent_detection",
            "意图识别",
            "completed",
            intent_started,
            summary=f"{analysis.intent} / {analysis.confidence}",
            analysis=analysis.to_dict(),
        )
        if analysis.safety_action == "refuse":
            answer_started = time.perf_counter()
            yield {"type": "delta", "text": SENSITIVE_REFUSAL_MESSAGE}
            yield assistant_step_event(
                "answer_generation",
                "生成回答",
                "completed",
                answer_started,
                summary=f"敏感问题拒答，输出 {len(SENSITIVE_REFUSAL_MESSAGE)} 个字符",
            )
            yield {
                "type": "done",
                "flow_id": "basic_rag",
                "question": question,
                "answer_draft": SENSITIVE_REFUSAL_MESSAGE,
                "documents": [],
            }
            self._record_assistant_chat_event(
                question=question,
                analysis=analysis,
                documents=[],
                payload=payload,
                started=started,
                rerank_used=False,
            )
            return

        embedding_started = time.perf_counter()
        retrieval_query = analysis.query_rewrite or question
        retrieval_result = self.hybrid_retrieval_service().retrieve(
            retrieval_query,
            include_parent_context=True,
            use_kg=False,
        )
        yield assistant_step_event(
            "query_embedding",
            "向量化",
            "completed",
            embedding_started,
            summary=f"{retrieval_result.query_embedding_dimensions} 维查询向量",
            dimensions=retrieval_result.query_embedding_dimensions,
            query=retrieval_query,
        )

        search_started = time.perf_counter()
        top_k = self.hybrid_retrieval_service().top_k
        min_score = self.hybrid_retrieval_service().min_score
        vector_docs = retrieval_result.vector_documents
        keyword_docs = retrieval_result.keyword_documents
        candidates = retrieval_result.candidates
        vector_started = time.perf_counter()
        yield assistant_step_event(
            "vector_search",
            "向量召回",
            "completed",
            vector_started,
            summary=f"向量召回 {len(vector_docs)} 条候选",
            top_k=retrieval_result.candidate_limit,
            min_score=min_score,
            count=len(vector_docs),
        )

        keyword_started = time.perf_counter()
        yield assistant_step_event(
            "keyword_search",
            "关键词召回",
            "completed",
            keyword_started,
            summary=f"关键词召回 {len(keyword_docs)} 条候选",
            top_k=retrieval_result.candidate_limit,
            query_terms=retrieval_result.query_terms,
            count=len(keyword_docs),
        )

        if retrieval_result.rerank_used:
            rerank_started = time.perf_counter()
            yield assistant_step_event(
                "rerank",
                "重排",
                "completed",
                rerank_started,
                summary=f"rerank 从候选池截取 top {top_k}",
                top_k=top_k,
                input_size=retrieval_result.candidate_limit,
                model=getattr(self.hybrid_retrieval_service().rerank, "model", None),
            )

        docs = [candidate.document for candidate in candidates]
        ranked_documents = []
        for candidate in candidates:
            payload_doc = assistant_document_payload(candidate.document)
            payload_doc.update(
                {
                    "retrieval_channels": list(candidate.channels),
                    "fused_score": candidate.fused_score,
                    "vector_score": candidate.vector_score,
                    "keyword_score": candidate.keyword_score,
                }
            )
            ranked_documents.append(payload_doc)
        context_documents = list(ranked_documents)
        if retrieval_result.parent_documents:
            docs.extend(retrieval_result.parent_documents)
            for parent_doc in retrieval_result.parent_documents:
                payload_doc = assistant_document_payload(parent_doc)
                payload_doc.update(
                    {
                        "retrieval_channels": ["parent_context"],
                        "fused_score": None,
                        "vector_score": None,
                        "keyword_score": None,
                    }
                )
                context_documents.append(payload_doc)
        yield assistant_step_event(
            "hybrid_retrieval",
            "混合召回",
            "completed",
            search_started,
            summary=f"向量 {len(vector_docs)} 条，关键词 {len(keyword_docs)} 条，融合后 {len(ranked_documents)} 条",
            top_k=top_k,
            min_score=min_score,
            candidate_limit=retrieval_result.candidate_limit,
            vector_count=len(vector_docs),
            keyword_count=len(keyword_docs),
            query_terms=retrieval_result.query_terms,
            documents=ranked_documents,
        )

        context_started = time.perf_counter()
        top_score = ranked_documents[0]["score"] if ranked_documents else None
        yield assistant_step_event(
            "source_context",
            "命中来源",
            "completed",
            context_started,
            summary=f"最高分 {top_score:.2f}" if top_score is not None else "未检索到可用来源",
            documents=context_documents,
        )

        answer_started = time.perf_counter()
        yield assistant_step_event(
            "answer_generation",
            "生成回答",
            "running",
            answer_started,
            summary="模型正在流式生成回答",
        )
        conversation_context = normalize_conversation_context(payload.get("conversation_context"))
        prompt = build_user_prompt(question, docs, conversation_context=conversation_context)
        answer_parts: list[str] = []
        system_prompt = self.assistant_system_prompt_from_payload(payload)
        try:
            for text in chat.stream_complete(system_prompt, prompt):
                answer_parts.append(text)
                yield {"type": "delta", "text": text}
        except Exception as exc:
            # 生成阶段属于外部模型依赖；以 SSE error 结束当前回答，避免冒泡成 internal error。
            error_message = assistant_model_error_message(exc)
            logger.warning("assistant answer generation failed: %s", exc, exc_info=True)
            yield assistant_step_event(
                "answer_generation",
                "生成回答",
                "failed",
                answer_started,
                summary=error_message,
            )
            yield {"type": "error", "error": error_message}
            return

        answer_draft = "".join(answer_parts).strip()
        if not answer_draft:
            answer_draft = "模型服务暂时没有返回有效内容，请稍后重试或转人工处理。"
            yield {"type": "delta", "text": answer_draft}
        yield assistant_step_event(
            "answer_generation",
            "生成回答",
            "completed",
            answer_started,
            summary=f"输出 {len(answer_draft)} 个字符",
        )
        yield {
            "type": "done",
            "flow_id": "basic_rag",
            "question": question,
            "answer_draft": answer_draft,
            "documents": context_documents,
        }
        self._record_assistant_chat_event(
            question=question,
            analysis=analysis,
            documents=ranked_documents,
            payload=payload,
            started=started,
            rerank_used=retrieval_result.rerank_used,
        )

    def _record_assistant_chat_event(
        self,
        *,
        question: str,
        analysis: QueryAnalysis,
        documents: list[dict[str, Any]],
        payload: dict[str, Any],
        started: float,
        rerank_used: bool,
    ) -> None:
        """把 RAG 主路径的一次查询写入 query_analytics_events，失败不影响主流程。"""
        hit_count = len(documents)
        top_score = documents[0].get("score") if documents else None
        chunk_ids = [str(doc.get("id") or "") for doc in documents if doc.get("id")]
        requester_type = str(payload.get("requester_type") or "unknown").strip() or "unknown"
        requester_id_raw = payload.get("requester_id")
        requester_id = str(requester_id_raw).strip() if requester_id_raw else None
        latency_ms = int((time.perf_counter() - started) * 1000)
        event = {
            "query": question,
            "intent": analysis.intent,
            "retrieved_chunk_ids": chunk_ids,
            "top_score": top_score,
            "hit_count": hit_count,
            "rerank_used": bool(rerank_used),
            "latency_ms": latency_ms,
            "requester_type": requester_type,
            "requester_id": requester_id,
            "metadata": {"flow": "basic_rag"},
        }
        try:
            self.database().record_query_event(event)
        except Exception as exc:
            logger.warning("query analytics record failed: %s", exc, exc_info=True)


def static_path(path: str) -> Path:
    """把允许访问的管理页静态路径映射到本地文件。

    管理后台是 React SPA（HashRouter），仅放行两类资源：
    1. `/` —— React 入口 `static/dist/index.html`
    2. `/static/dist/<任意子路径>` —— Vite 产物（含 hash 文件名 + 子目录），路径必须落在 `static/dist/` 之内
    """
    static_dir = Path(__file__).with_name("static")
    dist_dir = static_dir / "dist"
    clean = unquote(path).lstrip("/")
    if path in {"", "/"}:
        return dist_dir / "index.html"
    if clean.startswith("static/dist/"):
        rel = clean[len("static/dist/") :]
        candidate = (dist_dir / rel).resolve()
        try:
            candidate.relative_to(dist_dir.resolve())
        except ValueError:
            raise AdminNotFoundError(path)
        if candidate.exists() and candidate.is_file():
            return candidate
        raise AdminNotFoundError(path)
    raise AdminNotFoundError(path)
