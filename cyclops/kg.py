from __future__ import annotations

import hashlib
import json
import math
from typing import Any


ENTITY_TYPES = frozenset(
    {
        "product_platform_module",
        "feature_ui_action",
        "error_symptom",
        "process_task_object",
        "role_permission_channel",
        "condition_policy",
    }
)
RELATION_TYPES = frozenset(
    {
        "belongs_to",
        "requires",
        "causes",
        "resolves_by",
        "blocked_by",
        "available_for",
        "escalate_when",
    }
)
KG_PAYLOAD_FIELDS = frozenset({"entities", "relations"})
KG_ENTITY_FIELDS = frozenset(
    {"name", "entity_type", "aliases", "description", "confidence", "evidence"}
)
KG_RELATION_FIELDS = frozenset(
    {
        "head",
        "head_type",
        "relation_type",
        "tail",
        "tail_type",
        "description",
        "confidence",
        "evidence",
    }
)
KG_EVIDENCE_FIELDS = frozenset({"excerpt"})
KG_SOURCE_GUARD_FIELDS = frozenset(
    {
        "source_type",
        "source_id",
        "source_chunk_id",
        "source_title",
        "section_path",
        "page_start",
        "page_end",
        "fingerprint",
    }
)


class KnowledgeGraphExtractionError(ValueError):
    """KG 抽取结果不符合固定 schema 时抛出，避免脏候选进入审核池。"""


def build_faq_kg_source_text(faq: dict[str, Any]) -> str:
    """构造 FAQ 的 KG 抽取原文，关键约束是抽取与落库前版本校验使用同一口径。"""
    parts = [f"问题：{faq.get('question') or ''}", f"答案：{faq.get('answer') or ''}"]
    if faq.get("category"):
        parts.append(f"分类：{faq['category']}")
    tags = _clean_list(faq.get("tags"))
    if tags:
        parts.append(f"标签：{'，'.join(tags)}")
    return "\n".join(part for part in parts if part.strip())


def kg_source_fingerprint(*, source_text: str, source: dict[str, Any]) -> str:
    """计算 KG 来源指纹；FAQ 使用完整模型原文，文档还绑定 canonical locator。"""
    source_type = source.get("source_type")
    if source_type == "faq":
        encoded = str(source_text or "").encode("utf-8")
    elif source_type == "document":
        payload = {
            "source_text": str(source_text or ""),
            "source_type": source_type,
            "source_id": source.get("source_id"),
            "source_chunk_id": source.get("source_chunk_id"),
            "source_title": source.get("source_title"),
            "section_path": _clean_list(source.get("section_path")),
            "page_start": _clean_int(source.get("page_start")),
            "page_end": _clean_int(source.get("page_end")),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        raise ValueError("KG source fingerprint requires faq or document source_type")
    return hashlib.sha256(encoded).hexdigest()


def build_kg_source_guard(*, source_text: str, source: dict[str, Any]) -> dict[str, Any]:
    """构造唯一 KG 完成门禁，关键约束是显式携带所有证据 locator 与来源指纹。"""
    source_type = source.get("source_type")
    source_id = source.get("source_id")
    if not isinstance(source_type, str) or not source_type.strip():
        raise ValueError("KG source guard requires source_type")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("KG source guard requires source_id")
    locator = {
        "source_type": source_type,
        "source_id": source_id,
        "source_chunk_id": source.get("source_chunk_id"),
        "source_title": source.get("source_title"),
        "section_path": _clean_list(source.get("section_path")),
        "page_start": _clean_int(source.get("page_start")),
        "page_end": _clean_int(source.get("page_end")),
    }
    return {
        **locator,
        "fingerprint": kg_source_fingerprint(
            source_text=source_text,
            source=locator,
        ),
    }


def parse_kg_extraction_response(
    payload: str | dict[str, Any],
    *,
    source_text: str,
    source: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """解析唯一 KG schema，并把每条证据绑定到同一份原文的精确位置。"""
    data = _load_payload(payload)
    _validate_exact_fields(data, KG_PAYLOAD_FIELDS, "KG payload")
    source_evidence = _source_evidence_base(source)
    entity_map: dict[tuple[str, str], dict[str, Any]] = {}

    for index, raw_entity in enumerate(_object_list(data.get("entities"), "entities")):
        _validate_exact_fields(raw_entity, KG_ENTITY_FIELDS, f"entities[{index}]")
        entity = _normalize_entity(raw_entity, source_evidence, source_text)
        entity_key = (_entity_key(entity["name"]), entity["entity_type"])
        if entity_key in entity_map:
            raise KnowledgeGraphExtractionError(f"duplicate entity id: {entity['id']}")
        entity_map[entity_key] = entity

    relations: list[dict[str, Any]] = []
    relation_ids: set[str] = set()
    for index, raw_relation in enumerate(_object_list(data.get("relations"), "relations")):
        _validate_exact_fields(raw_relation, KG_RELATION_FIELDS, f"relations[{index}]")
        relation = _normalize_relation(raw_relation, source_evidence, source_text, entity_map)
        if relation["id"] in relation_ids:
            raise KnowledgeGraphExtractionError(f"duplicate relation id: {relation['id']}")
        relation_ids.add(relation["id"])
        relations.append(relation)

    return {"entities": list(entity_map.values()), "relations": relations}


def build_kg_entity_knowledge_chunk_row(
    entity: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """把已确认实体投影为可检索 KG chunk，关键约束是稳定 source_id 等于实体 ID。"""
    aliases = _clean_list(entity.get("aliases"))
    name = _required_text(entity.get("name"), "name")
    entity_type = _validate_entity_type(entity.get("entity_type"))
    description = str(entity.get("description") or "").strip()
    content_parts = [f"实体：{name}", f"类型：{entity_type}"]
    if aliases:
        content_parts.append(f"别名：{'，'.join(aliases)}")
    if description:
        content_parts.append(f"描述：{description}")
    content = "\n".join(content_parts)
    metadata = {
        "kg_kind": "entity",
        "entity_id": entity["id"],
        "entity_type": entity_type,
        "aliases": aliases,
        "evidence": evidence,
    }
    chunk = {
        "id": f"kc_kg_entity_{entity['id']}",
        "source_type": "kg_entity",
        "source_id": entity["id"],
        "source_chunk_id": None,
        "parent_chunk_id": None,
        "chunk_level": "chunk",
        "source_title": name,
        "chunk_index": 0,
        "section_path": [],
        "page_start": None,
        "page_end": None,
        "block_type": "kg_entity",
        "source_offsets": {},
        "content": content,
        "embedding_text": content,
        "search_text": _join_search_text([name, aliases, entity_type, description, _evidence_text(evidence)]),
        "metadata": metadata,
        "tags": [entity_type, *aliases],
        "confidence": _confidence_text(entity.get("confidence")),
        "status": entity.get("status", "usable"),
    }
    chunk["content_hash"] = _content_hash(chunk["embedding_text"])
    return chunk


def build_kg_relation_knowledge_chunk_row(
    relation: dict[str, Any],
    head: dict[str, Any],
    tail: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """把已确认关系投影为可检索 KG chunk，关键约束是保留头尾实体稳定 ID。"""
    relation_type = _validate_relation_type(relation.get("relation_type"))
    head_name = _required_text(head.get("name"), "head.name")
    tail_name = _required_text(tail.get("name"), "tail.name")
    description = str(relation.get("description") or "").strip()
    title = f"{head_name} {relation_type} {tail_name}"
    content_parts = [
        f"关系：{relation_type}",
        f"头实体：{head_name}",
        f"尾实体：{tail_name}",
    ]
    if description:
        content_parts.append(f"描述：{description}")
    content = "\n".join(content_parts)
    metadata = {
        "kg_kind": "relation",
        "relation_id": relation["id"],
        "relation_type": relation_type,
        "head_entity_id": relation["head_entity_id"],
        "tail_entity_id": relation["tail_entity_id"],
        "head_entity_type": head.get("entity_type"),
        "tail_entity_type": tail.get("entity_type"),
        "evidence": evidence,
    }
    chunk = {
        "id": f"kc_kg_relation_{relation['id']}",
        "source_type": "kg_relation",
        "source_id": relation["id"],
        "source_chunk_id": None,
        "parent_chunk_id": None,
        "chunk_level": "chunk",
        "source_title": title,
        "chunk_index": 0,
        "section_path": [],
        "page_start": None,
        "page_end": None,
        "block_type": "kg_relation",
        "source_offsets": {},
        "content": content,
        "embedding_text": content,
        "search_text": _join_search_text(
            [head_name, tail_name, relation_type, description, _evidence_text(evidence)]
        ),
        "metadata": metadata,
        "tags": [relation_type, str(head.get("entity_type") or ""), str(tail.get("entity_type") or "")],
        "confidence": _confidence_text(relation.get("confidence")),
        "status": relation.get("status", "usable"),
    }
    chunk["content_hash"] = _content_hash(chunk["embedding_text"])
    return chunk


def _normalize_entity(
    raw: dict[str, Any],
    source_evidence: dict[str, Any],
    source_text: str,
) -> dict[str, Any]:
    """整理单个实体候选，关键约束是实体类型必须来自固定枚举。"""
    name = _required_text(raw.get("name"), "name")
    entity_type = _validate_entity_type(raw.get("entity_type"))
    evidence = _normalize_evidence(raw.get("evidence"), source_evidence, source_text)
    return {
        "id": canonical_kg_entity_id(name, entity_type),
        "name": name,
        "entity_type": entity_type,
        "aliases": _model_string_list(raw.get("aliases"), "aliases"),
        "description": _model_optional_text(raw.get("description"), "description"),
        "status": "needs_review",
        "confidence": _confidence_number(raw.get("confidence")),
        "evidence": evidence,
    }


def _normalize_relation(
    raw: dict[str, Any],
    source_evidence: dict[str, Any],
    source_text: str,
    entity_map: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """整理单个关系候选，关键约束是端点实体自动归并到同一稳定 ID。"""
    head_name = _required_text(raw.get("head"), "head")
    tail_name = _required_text(raw.get("tail"), "tail")
    head_type = _validate_entity_type(raw.get("head_type"))
    tail_type = _validate_entity_type(raw.get("tail_type"))
    relation_type = _validate_relation_type(raw.get("relation_type"))
    evidence = _normalize_evidence(raw.get("evidence"), source_evidence, source_text)
    head = _ensure_endpoint_entity(entity_map, head_name, head_type, evidence)
    tail = _ensure_endpoint_entity(entity_map, tail_name, tail_type, evidence)
    relation_id = canonical_kg_relation_id(head["id"], relation_type, tail["id"])
    return {
        "id": relation_id,
        "head_entity_id": head["id"],
        "head_entity_name": head_name,
        "head_entity_type": head_type,
        "relation_type": relation_type,
        "tail_entity_id": tail["id"],
        "tail_entity_name": tail_name,
        "tail_entity_type": tail_type,
        "description": _model_optional_text(raw.get("description"), "description"),
        "status": "needs_review",
        "confidence": _confidence_number(raw.get("confidence")),
        "evidence": evidence,
    }


def _ensure_endpoint_entity(
    entity_map: dict[tuple[str, str], dict[str, Any]],
    name: str,
    entity_type: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """确保关系端点也有实体候选，避免孤立边进入数据库。"""
    key = (_entity_key(name), entity_type)
    if key not in entity_map:
        entity_map[key] = {
            "id": canonical_kg_entity_id(name, entity_type),
            "name": name,
            "entity_type": entity_type,
            "aliases": [],
            "description": "",
            "status": "needs_review",
            "confidence": None,
            "evidence": evidence,
        }
    return entity_map[key]


def _load_payload(payload: str | dict[str, Any]) -> dict[str, Any]:
    """读取模型 JSON 输出，关键约束是顶层必须为对象。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise KnowledgeGraphExtractionError("invalid KG JSON payload") from exc
    if not isinstance(payload, dict):
        raise KnowledgeGraphExtractionError("KG payload must be a JSON object")
    return payload


def _source_evidence_base(source: dict[str, Any]) -> dict[str, Any]:
    """整理来源证据公共字段，关键约束是必须能追溯到来源 ID。"""
    source_type = _required_text(source.get("source_type"), "source_type")
    source_id = _required_text(source.get("source_id"), "source_id")
    return {
        "source_type": source_type,
        "source_id": source_id,
        "source_chunk_id": source.get("source_chunk_id"),
        "source_title": source.get("source_title"),
        "section_path": _clean_list(source.get("section_path")),
        "page_start": _clean_int(source.get("page_start")),
        "page_end": _clean_int(source.get("page_end")),
    }


def _normalize_evidence(
    value: Any,
    source_evidence: dict[str, Any],
    source_text: str,
) -> list[dict[str, Any]]:
    """整理候选证据，关键约束是 excerpt 必须精确存在于同一份来源原文。"""
    items = _object_list(value, "evidence")
    if not items:
        raise KnowledgeGraphExtractionError("evidence is required")
    evidence: list[dict[str, Any]] = []
    seen_excerpts: set[str] = set()
    for index, item in enumerate(items):
        _validate_exact_fields(item, KG_EVIDENCE_FIELDS, f"evidence[{index}]")
        excerpt = _required_text(item.get("excerpt"), "evidence.excerpt")
        if excerpt in seen_excerpts:
            raise KnowledgeGraphExtractionError("duplicate evidence entry")
        seen_excerpts.add(excerpt)
        char_start, char_end = locate_kg_evidence_excerpt(source_text, excerpt)
        evidence.append(
            {
                **source_evidence,
                "excerpt": excerpt,
                "char_start": char_start,
                "char_end": char_end,
            }
        )
    return evidence


def locate_kg_evidence_excerpt(source_text: str, excerpt: str) -> tuple[int, int]:
    """定位证据原文；重复文本取首次精确命中，不做模糊修复。"""
    start = source_text.find(excerpt)
    if start < 0:
        raise KnowledgeGraphExtractionError(
            "evidence excerpt must be an exact source substring"
        )
    return start, start + len(excerpt)


def _validate_entity_type(value: Any) -> str:
    """校验实体类型，关键约束是禁止模型自由扩展类型。"""
    entity_type = _required_text(value, "entity_type")
    if entity_type not in ENTITY_TYPES:
        raise KnowledgeGraphExtractionError(f"invalid entity_type: {entity_type}")
    return entity_type


def _validate_relation_type(value: Any) -> str:
    """校验关系类型，关键约束是禁止模型自由扩展类型。"""
    relation_type = _required_text(value, "relation_type")
    if relation_type not in RELATION_TYPES:
        raise KnowledgeGraphExtractionError(f"invalid relation_type: {relation_type}")
    return relation_type


def _required_text(value: Any, field: str) -> str:
    """读取必填文本字段，关键约束是空字符串视为缺失。"""
    if not isinstance(value, str):
        raise KnowledgeGraphExtractionError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise KnowledgeGraphExtractionError(f"{field} is required")
    return text


def _model_optional_text(value: Any, field: str) -> str:
    """读取模型可为空文本，关键约束是字段存在时仍必须保持字符串类型。"""
    if not isinstance(value, str):
        raise KnowledgeGraphExtractionError(f"{field} must be a string")
    return value.strip()


def _model_string_list(value: Any, field: str) -> list[str]:
    """读取模型字符串数组，关键约束是不接受逗号文本等第二种表示。"""
    if not isinstance(value, list):
        raise KnowledgeGraphExtractionError(f"{field} must be an array")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise KnowledgeGraphExtractionError(f"{field}[{index}] must be a string")
        text = item.strip()
        if text:
            items.append(text)
    return items


def _validate_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    """校验模型对象字段集合，关键约束是缺失和额外字段都不能静默通过。"""
    actual = set(value)
    missing = sorted(expected.difference(actual))
    if missing:
        raise KnowledgeGraphExtractionError(f"missing {label} fields: {', '.join(missing)}")
    unsupported = sorted(actual.difference(expected))
    if unsupported:
        raise KnowledgeGraphExtractionError(
            f"unsupported {label} fields: {', '.join(unsupported)}"
        )


def _object_list(value: Any, field: str) -> list[dict[str, Any]]:
    """读取模型对象数组，关键约束是错类型和非对象元素都显式失败。"""
    if not isinstance(value, list):
        raise KnowledgeGraphExtractionError(f"{field} must be an array")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise KnowledgeGraphExtractionError(f"{field}[{index}] must be an object")
        items.append(dict(item))
    return items


def _clean_list(value: Any) -> list[str]:
    """把 JSON 数组或逗号分隔文本整理为字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            value = parsed
        else:
            value = value.replace("，", ",").split(",")
    return [str(item).strip() for item in value if str(item).strip()]


def _clean_int(value: Any) -> int | None:
    """整理页码字段，无法转换时保持空值。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _confidence_number(value: Any) -> float | None:
    """整理模型置信度，关键约束是只接受 0-1 数值。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KnowledgeGraphExtractionError("confidence must be numeric")
    confidence = float(value)
    if not math.isfinite(confidence):
        raise KnowledgeGraphExtractionError("confidence must be finite")
    if confidence < 0 or confidence > 1:
        raise KnowledgeGraphExtractionError("confidence must be between 0 and 1")
    return confidence


def _confidence_text(value: Any) -> str | None:
    """把 KG 浮点置信度转成 knowledge_chunks 当前使用的文本字段。"""
    if value is None or value == "":
        return None
    return str(value)


def _entity_key(name: str) -> str:
    """生成实体去重 key，关键约束是同名同类型稳定归并。"""
    return " ".join(name.strip().lower().split())


def canonical_kg_entity_id(name: str, entity_type: str) -> str:
    """生成稳定实体 ID，关键约束是后续可视化和投影复用同一 ID。"""
    return "kg_ent_" + _digest({"name": _entity_key(name), "entity_type": entity_type})


def canonical_kg_relation_id(head_id: str, relation_type: str, tail_id: str) -> str:
    """生成稳定关系 ID，关键约束是同一事实重复抽取时可幂等合并。"""
    return "kg_rel_" + _digest({"head": head_id, "relation_type": relation_type, "tail": tail_id})


def _digest(payload: dict[str, Any]) -> str:
    """按规范化 JSON 生成短哈希 ID。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _join_search_text(parts: list[Any]) -> str:
    """拼接 KG 检索文本，关键约束是保留中文原文并跳过空值。"""
    values: list[str] = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, list):
            values.extend(str(item).strip() for item in part if str(item).strip())
        else:
            text = str(part).strip()
            if text:
                values.append(text)
    return "\n".join(values)


def _evidence_text(evidence: list[dict[str, Any]]) -> list[str]:
    """提取证据摘要给全文检索使用。"""
    return [str(item.get("excerpt") or "").strip() for item in evidence if item.get("excerpt")]


def _content_hash(embedding_text: str) -> str:
    """计算投影 chunk 指纹，关键约束是 embedding_text 变化才触发 stale。"""
    encoded = json.dumps(
        {"embedding_text": embedding_text}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
