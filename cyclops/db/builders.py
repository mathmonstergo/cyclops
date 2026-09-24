from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from cyclops.chunking import normalize_children_delimiter, split_with_pattern


DOCUMENT_CHILD_INDEX_OFFSET = 1


def count_job_item_statuses(items: list[dict[str, Any]]) -> dict[str, int]:
    """统计生成任务子项状态，写入任务摘要字段。"""
    return {
        "queued_count": sum(1 for item in items if item["status"] == "queued"),
        "processing_count": sum(1 for item in items if item["status"] == "processing"),
        "generated_count": sum(1 for item in items if item["status"] == "generated"),
        "skipped_count": sum(1 for item in items if item["status"] == "skipped"),
        "failed_count": sum(1 for item in items if item["status"] == "failed"),
    }


def clean_list(value: Any) -> list[str]:
    """把 JSON 数组、逗号分隔字符串或列表统一整理为字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def clean_dict(value: Any) -> dict[str, Any]:
    """把 JSON 字符串或字典整理为字典，避免结构化来源字段丢失。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def clean_block_list(value: Any) -> list[dict[str, Any]]:
    """把解析器来源块整理为字典列表，避免 child 派生依赖渲染文本。"""
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def document_child_sources(
    chunk: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """选择文档 child 来源，关键约束是结构块、delimiter、整段正文依次择一。"""
    blocks = [
        block
        for block in clean_block_list(chunk.get("source_blocks"))
        if str(block.get("text") or "").strip()
    ]
    if blocks:
        return [], blocks

    pattern = normalize_children_delimiter(chunk.get("children_delimiter"))
    if pattern:
        delimiter_children = split_with_pattern(
            str(chunk.get("source_text") or ""),
            pattern,
        )
        if delimiter_children:
            return delimiter_children, []
    return [str(chunk.get("source_text") or "")], []


def document_embedding_source_fingerprint(
    import_file: dict[str, Any],
    chunk: dict[str, Any],
) -> str:
    """计算文档向量来源指纹，关键约束是覆盖所有会改变投影或来源生命周期的字段。"""
    payload = {
        "file": {
            "id": str(import_file.get("id") or ""),
            "original_name": str(import_file.get("original_name") or ""),
            "file_type": str(import_file.get("file_type") or ""),
            "parser": str(import_file.get("parser") or ""),
            "chunker_type": str(import_file.get("chunker_type") or ""),
            "status": str(import_file.get("status") or ""),
            "is_disabled": bool(import_file.get("is_disabled")),
        },
        "chunk": {
            "id": str(chunk.get("id") or ""),
            "file_id": str(chunk.get("file_id") or ""),
            "chunk_index": int(chunk.get("chunk_index", 0)),
            "section_path": clean_list(chunk.get("section_path")),
            "page_start": clean_int(chunk.get("page_start")),
            "page_end": clean_int(chunk.get("page_end")),
            "block_type": str(chunk.get("block_type") or ""),
            "source_offsets": clean_dict(chunk.get("source_offsets")),
            "source_blocks": clean_block_list(chunk.get("source_blocks")),
            "children_delimiter": str(chunk.get("children_delimiter") or ""),
            "start_at": str(chunk.get("start_at") or ""),
            "end_at": str(chunk.get("end_at") or ""),
            "message_count": int(chunk.get("message_count", 0)),
            "keywords": clean_list(chunk.get("keywords")),
            "source_text": str(chunk.get("source_text") or ""),
            "status": str(chunk.get("status") or ""),
            "questions": clean_list(chunk.get("questions")),
            "is_disabled": bool(chunk.get("is_disabled")),
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def expected_document_knowledge_count(chunk: dict[str, Any]) -> int:
    """计算 parent + child 预期行数，必须与实际 child 生成共用同一选择结果。"""
    child_texts, child_blocks = document_child_sources(chunk)
    return 1 + len(child_texts or child_blocks)


def child_knowledge_chunk_index(parent_index: int, child_index: int) -> int:
    """为文档 child 生成确定性负索引，关键约束是不与 parent 正编号冲突。"""
    parent = max(int(parent_index), 0)
    child = max(int(child_index), 0)
    paired = (parent + child) * (parent + child + 1) // 2 + child
    return -(paired + DOCUMENT_CHILD_INDEX_OFFSET)


def clean_int(value: Any) -> int | None:
    """把可选页码字段整理为整数，无法转换时保持为空。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_embedding_text(row: dict[str, Any]) -> str:
    """把 FAQ 问题、答案和标签拼成单条向量文本，保持一条 FAQ 一个向量。"""
    question = str(row.get("question", "")).strip()
    answer = str(row.get("answer", "")).strip()
    variants = clean_list(row.get("question_variants"))
    tags = clean_list(row.get("tags"))
    category = str(row.get("category", "") or "").strip()

    parts = [f"标准问题：{question}"]
    if variants:
        parts.append(f"相似问法：{'；'.join(variants)}")
    parts.append(f"答案：{answer}")
    if category:
        parts.append(f"分类：{category}")
    if tags:
        parts.append(f"标签：{'，'.join(tags)}")
    return "\n".join(parts)


def join_search_text(parts: Iterable[Any]) -> str:
    """拼接全文检索文本，关键约束是跳过空值并保留中文原文。"""
    values: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, list):
            values.extend(str(item).strip() for item in part if str(item).strip())
            continue
        text = str(part).strip()
        if text:
            values.append(text)
    return "\n".join(values)


def format_section_path(section_path: list[str]) -> str:
    """把章节路径格式化为适合 embedding 和搜索的可读文本。"""
    return " > ".join(section_path)


def format_page_range(page_start: int | None, page_end: int | None) -> str:
    """把页码范围格式化为人可读文本，缺失页码时返回空字符串。"""
    if page_start is None and page_end is None:
        return ""
    if page_start is not None and page_end is not None and page_start != page_end:
        return f"{page_start}-{page_end}"
    return str(page_start if page_start is not None else page_end)


def build_document_embedding_text(
    *,
    source_title: str,
    section_path: list[str],
    page_start: int | None,
    page_end: int | None,
    block_type: str | None,
    keywords: list[str],
    source_text: str,
    questions: list[str] | None = None,
) -> str:
    """构造文档向量文本，关键约束是给孤立切片补足文件、章节和页码上下文。

    `questions` 是 LLM 生成的假设性用户问题，拼在正文前面让向量更贴近真实提问。
    """
    parts = []
    if source_title:
        parts.append(f"文件：{source_title}")
    section = format_section_path(section_path)
    if section:
        parts.append(f"章节：{section}")
    page_range = format_page_range(page_start, page_end)
    if page_range:
        parts.append(f"页码：{page_range}")
    if block_type:
        parts.append(f"块类型：{block_type}")
    if keywords:
        parts.append(f"关键词：{'，'.join(keywords)}")
    if questions:
        parts.append(f"可能问题：{' / '.join(questions)}")
    parts.append(f"正文：{source_text}")
    return "\n".join(parts)


def compute_knowledge_chunk_hash(row: dict[str, Any]) -> str:
    """按统一知识单元的向量文本计算指纹，用于后续判断 embedding 是否过期。"""
    payload = {"embedding_text": row["embedding_text"]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empty_import_file_embedding_summary() -> dict[str, Any]:
    """构造文档向量空摘要，关键约束是字段稳定供前端直接渲染。"""
    return {
        "status": "none",
        "total_chunks": 0,
        "knowledge_count": 0,
        "ready_count": 0,
        "stale_count": 0,
        "failed_count": 0,
        "pending_count": 0,
        "missing_count": 0,
    }


def build_faq_knowledge_chunk_row(row: dict[str, Any]) -> dict[str, Any]:
    """把正式 FAQ 映射为统一知识单元，保持一条 FAQ 对应一个 chunk。"""
    question = str(row.get("question", "")).strip()
    answer = str(row.get("answer", "")).strip()
    variants = clean_list(row.get("question_variants"))
    tags = clean_list(row.get("tags"))
    category = str(row.get("category", "") or "").strip()
    content_parts = [f"问题：{question}"]
    if variants:
        content_parts.append(f"相似问法：{'；'.join(variants)}")
    content_parts.append(f"答案：{answer}")
    embedding_text = row.get("embedding_text") or build_embedding_text(row)
    metadata = {
        "category": category or None,
        "question_variants": variants,
        "evidence": row.get("evidence", []),
        "source_file": row.get("source_file"),
        "source_group": row.get("source_group"),
        "source_date": row.get("source_date"),
    }
    chunk = {
        "id": f"kc_faq_{row['id']}",
        "source_type": "faq",
        "source_id": row["id"],
        "source_chunk_id": None,
        "parent_chunk_id": None,
        "chunk_level": "chunk",
        "source_title": question,
        "chunk_index": 0,
        "section_path": [],
        "page_start": None,
        "page_end": None,
        "block_type": "faq",
        "source_offsets": {},
        "content": "\n".join(content_parts),
        "embedding_text": embedding_text,
        "search_text": join_search_text([question, variants, answer, category, tags]),
        "metadata": metadata,
        "tags": tags,
        "confidence": row.get("confidence"),
        "status": row.get("status", "usable"),
    }
    chunk["content_hash"] = compute_knowledge_chunk_hash(chunk)
    return chunk


def build_document_knowledge_chunk_row(
    chunk: dict[str, Any],
    import_file: dict[str, Any],
    *,
    knowledge_chunk_id: str,
) -> dict[str, Any]:
    """把文档来源映射为知识行，文件、来源切片与知识行三类 ID 必须明确分离。"""
    chunk_file_id = str(chunk["file_id"]).strip()
    source_id = str(import_file["id"]).strip()
    if chunk_file_id != source_id:
        raise ValueError("chunk.file_id must match import_file.id")
    source_text_raw = str(chunk.get("source_text", ""))
    source_text = source_text_raw if source_text_raw.strip() else ""
    keywords = clean_list(chunk.get("keywords"))
    source_title = str(import_file.get("original_name") or chunk.get("file_id") or "").strip()
    section_path = clean_list(chunk.get("section_path"))
    page_start = clean_int(chunk.get("page_start"))
    page_end = clean_int(chunk.get("page_end"))
    block_type = str(chunk.get("block_type") or "").strip() or None
    source_offsets = clean_dict(chunk.get("source_offsets"))
    source_blocks = clean_block_list(chunk.get("source_blocks"))
    chunk_level = chunk.get("chunk_level")
    if chunk_level not in {"parent", "child"}:
        raise ValueError("document chunk_level must be parent or child")
    questions = clean_list(chunk.get("questions"))
    embedding_text = build_document_embedding_text(
        source_title=source_title,
        section_path=section_path,
        page_start=page_start,
        page_end=page_end,
        block_type=block_type,
        keywords=keywords,
        source_text=source_text,
        questions=questions,
    )
    metadata = {
        "file_id": chunk.get("file_id"),
        "file_name": import_file.get("original_name"),
        "file_type": import_file.get("file_type"),
        "parser": import_file.get("parser"),
        "chunk_id": chunk.get("id"),
        "start_at": str(chunk.get("start_at")) if chunk.get("start_at") else None,
        "end_at": str(chunk.get("end_at")) if chunk.get("end_at") else None,
        "message_count": chunk.get("message_count", 0),
        "section_path": section_path,
        "page_start": page_start,
        "page_end": page_end,
        "block_type": block_type,
        "source_offsets": source_offsets,
        "source_blocks": source_blocks,
        "parent_content": chunk.get("parent_content"),
        "questions": questions,
    }
    row = {
        "id": f"kc_document_{knowledge_chunk_id}",
        "source_type": "document",
        "source_id": source_id,
        "source_chunk_id": chunk.get("id"),
        "parent_chunk_id": chunk.get("parent_chunk_id"),
        "chunk_level": chunk_level,
        "source_title": source_title or None,
        "chunk_index": int(chunk.get("chunk_index", 0)),
        "section_path": section_path,
        "page_start": page_start,
        "page_end": page_end,
        "block_type": block_type,
        "source_offsets": source_offsets,
        "source_blocks": source_blocks,
        "content": source_text,
        "embedding_text": embedding_text,
        "search_text": join_search_text(
            [
                source_title,
                format_section_path(section_path),
                format_page_range(page_start, page_end),
                block_type,
                keywords,
                questions,
                source_text,
            ]
        ),
        "metadata": metadata,
        "tags": keywords,
        "confidence": None,
        "status": chunk.get("retrieval_status", "needs_review"),
    }
    row["content_hash"] = compute_knowledge_chunk_hash(row)
    return row


def compute_content_hash(row: dict[str, Any]) -> str:
    """只按会进入 embedding 的文本计算内容指纹。"""
    payload = {"embedding_text": row.get("embedding_text") or build_embedding_text(row)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def next_embedding_status(
    previous_status: str | None,
    previous_hash: str | None,
    new_hash: str,
) -> str:
    if previous_status == "ready" and previous_hash == new_hash:
        return "ready"
    if previous_status == "ready":
        return "stale"
    if previous_status in {"stale", "failed"} and previous_hash == new_hash:
        return previous_status
    return "pending"


def build_import_candidate_faq_row(candidate: dict[str, Any]) -> dict[str, Any]:
    """把导入候选 FAQ 转成正式 FAQ 保存载荷，默认仍需人工审核。"""
    evidence = [
        {
            "source_file": candidate.get("file_name"),
            "chunk_id": candidate.get("chunk_id"),
            "excerpt": candidate.get("source_excerpt"),
        }
    ]
    row = {
        "id": f"faq_{candidate['id']}",
        "doc_type": "faq_qa",
        "source_file": candidate.get("file_name"),
        "source_group": "import_review",
        "category": candidate.get("category"),
        "question": candidate["question"],
        "question_variants": candidate.get("similar_questions") or [],
        "answer": candidate["answer"],
        "tags": candidate.get("tags") or [],
        "evidence": evidence,
        "confidence": candidate.get("confidence") or "medium",
        "status": "needs_review",
        "sensitivity": None,
    }
    row["embedding_text"] = build_embedding_text(row)
    return row
