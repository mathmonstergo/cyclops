from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

from cyclops.kg import (
    KnowledgeGraphExtractionError,
    canonical_kg_entity_id,
    canonical_kg_relation_id,
    kg_source_fingerprint,
)


_DOCUMENT_EVIDENCE_FIELDS = frozenset(
    {
        "source_type",
        "source_id",
        "source_chunk_id",
        "source_title",
        "section_path",
        "page_start",
        "page_end",
        "excerpt",
        "char_start",
        "char_end",
    }
)
_MAP_RESULT_FIELDS = frozenset({"chunk_id", "chunk_order", "entities", "relations"})
_MAP_ENTITY_FIELDS = frozenset(
    {
        "local_entity_id",
        "name",
        "entity_type",
        "aliases",
        "description",
        "confidence",
        "evidence",
    }
)
_MAP_RELATION_FIELDS = frozenset(
    {
        "local_relation_id",
        "head_local_entity_id",
        "relation_type",
        "tail_local_entity_id",
        "description",
        "confidence",
        "evidence",
    }
)
_EXTRACTION_FIELDS = frozenset({"entities", "relations"})
_EXTRACTION_ENTITY_FIELDS = frozenset(
    {
        "id",
        "name",
        "entity_type",
        "aliases",
        "description",
        "status",
        "confidence",
        "evidence",
    }
)
_EXTRACTION_RELATION_FIELDS = frozenset(
    {
        "id",
        "head_entity_id",
        "head_entity_name",
        "head_entity_type",
        "relation_type",
        "tail_entity_id",
        "tail_entity_name",
        "tail_entity_type",
        "description",
        "status",
        "confidence",
        "evidence",
    }
)


def build_document_kg_manifest(
    import_file: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成不可变文档 manifest；只纳入未禁用且有正文的当前切片。"""
    if not isinstance(import_file, dict):
        raise TypeError("import_file must be an object")
    if not isinstance(chunks, list):
        raise TypeError("chunks must be an array")
    file_id = _required_text(import_file.get("id"), "import_file.id")
    source_title = _required_text(
        import_file.get("original_name"),
        "import_file.original_name",
    )
    ordered_items: list[tuple[int, str, dict[str, Any]]] = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            raise TypeError(f"chunks[{index}] must be an object")
        is_disabled = chunk.get("is_disabled")
        if not isinstance(is_disabled, bool):
            raise TypeError(f"chunks[{index}].is_disabled must be a boolean")
        if is_disabled:
            continue
        chunk_id = _required_text(chunk.get("id"), f"chunks[{index}].id")
        chunk_file_id = _required_text(
            chunk.get("file_id"),
            f"chunks[{index}].file_id",
        )
        if chunk_file_id != file_id:
            raise ValueError(f"chunks[{index}].file_id must match import_file.id")
        chunk_index = _nonnegative_int(
            chunk.get("chunk_index"),
            f"chunks[{index}].chunk_index",
        )
        source_text = chunk.get("source_text")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError(f"chunks[{index}].source_text is required")
        section_path = _string_list(
            chunk.get("section_path"),
            f"chunks[{index}].section_path",
        )
        page_start = _optional_nonnegative_int(
            chunk.get("page_start"),
            f"chunks[{index}].page_start",
        )
        page_end = _optional_nonnegative_int(
            chunk.get("page_end"),
            f"chunks[{index}].page_end",
        )
        if page_start is not None and page_end is not None and page_end < page_start:
            raise ValueError(f"chunks[{index}] page_end must not precede page_start")
        source = {
            "source_type": "document",
            "source_id": file_id,
            "source_chunk_id": chunk_id,
            "source_title": source_title,
            "section_path": section_path,
            "page_start": page_start,
            "page_end": page_end,
        }
        item = {
            "chunk_id": chunk_id,
            "chunk_order": -1,
            "source_fingerprint": kg_source_fingerprint(
                source_text=source_text,
                source=source,
            ),
            "section_path": section_path,
            "page_start": page_start,
            "page_end": page_end,
        }
        ordered_items.append((chunk_index, chunk_id, item))

    if not ordered_items:
        raise ValueError("document KG manifest requires at least one enabled chunk")
    items: list[dict[str, Any]] = []
    for chunk_order, (_chunk_index, _chunk_id, item) in enumerate(
        sorted(ordered_items, key=lambda value: (value[0], value[1]))
    ):
        items.append({**item, "chunk_order": chunk_order})
    manifest_payload = {
        "file_id": file_id,
        "source_title": source_title,
        "items": items,
    }
    return {
        **manifest_payload,
        "fingerprint": _canonical_sha256(manifest_payload),
    }


def localize_document_kg_map_result(
    extraction: dict[str, Any],
    *,
    job_id: str,
    chunk_id: str,
    chunk_order: int,
) -> dict[str, Any]:
    """把单片 provisional canonical ID 改为 job/chunk 域内 local ID。"""
    if not isinstance(extraction, dict):
        raise TypeError("extraction must be an object")
    _validate_exact_fields(extraction, _EXTRACTION_FIELDS, "extraction")
    normalized_job_id = _required_text(job_id, "job_id")
    normalized_chunk_id = _required_text(chunk_id, "chunk_id")
    normalized_chunk_order = _nonnegative_int(chunk_order, "chunk_order")
    entity_items = _object_list(extraction.get("entities"), "entities")
    relation_items = _object_list(extraction.get("relations"), "relations")
    provisional_to_local: dict[str, str] = {}
    localized_entities: list[dict[str, Any]] = []
    for index, entity in enumerate(entity_items):
        _validate_exact_fields(
            entity,
            _EXTRACTION_ENTITY_FIELDS,
            f"entities[{index}]",
        )
        provisional_id = _required_text(entity.get("id"), f"entities[{index}].id")
        if provisional_id in provisional_to_local:
            raise KnowledgeGraphExtractionError(
                f"duplicate provisional entity ID: {provisional_id}"
            )
        local_id = _document_local_id(
            "entity",
            job_id=normalized_job_id,
            chunk_id=normalized_chunk_id,
            provisional_id=provisional_id,
        )
        provisional_to_local[provisional_id] = local_id
        localized_entities.append(
            {
                "local_entity_id": local_id,
                "name": _required_text(entity.get("name"), f"entities[{index}].name"),
                "entity_type": _required_text(
                    entity.get("entity_type"),
                    f"entities[{index}].entity_type",
                ),
                "aliases": _string_list(
                    entity.get("aliases"),
                    f"entities[{index}].aliases",
                ),
                "description": _text_field(
                    entity.get("description"),
                    f"entities[{index}].description",
                ),
                "confidence": _optional_confidence(
                    entity.get("confidence"),
                    f"entities[{index}].confidence",
                ),
                "evidence": _evidence_list(
                    entity.get("evidence"),
                    f"entities[{index}].evidence",
                    expected_chunk_id=normalized_chunk_id,
                ),
            }
        )

    localized_relations: list[dict[str, Any]] = []
    seen_relation_ids: set[str] = set()
    for index, relation in enumerate(relation_items):
        _validate_exact_fields(
            relation,
            _EXTRACTION_RELATION_FIELDS,
            f"relations[{index}]",
        )
        provisional_id = _required_text(relation.get("id"), f"relations[{index}].id")
        if provisional_id in seen_relation_ids:
            raise KnowledgeGraphExtractionError(
                f"duplicate provisional relation ID: {provisional_id}"
            )
        seen_relation_ids.add(provisional_id)
        head_id = _required_text(
            relation.get("head_entity_id"),
            f"relations[{index}].head_entity_id",
        )
        tail_id = _required_text(
            relation.get("tail_entity_id"),
            f"relations[{index}].tail_entity_id",
        )
        if head_id not in provisional_to_local or tail_id not in provisional_to_local:
            raise KnowledgeGraphExtractionError(
                f"relations[{index}] must reference entities from the same Map result"
            )
        localized_relations.append(
            {
                "local_relation_id": _document_local_id(
                    "relation",
                    job_id=normalized_job_id,
                    chunk_id=normalized_chunk_id,
                    provisional_id=provisional_id,
                ),
                "head_local_entity_id": provisional_to_local[head_id],
                "relation_type": _required_text(
                    relation.get("relation_type"),
                    f"relations[{index}].relation_type",
                ),
                "tail_local_entity_id": provisional_to_local[tail_id],
                "description": _text_field(
                    relation.get("description"),
                    f"relations[{index}].description",
                ),
                "confidence": _optional_confidence(
                    relation.get("confidence"),
                    f"relations[{index}].confidence",
                ),
                "evidence": _evidence_list(
                    relation.get("evidence"),
                    f"relations[{index}].evidence",
                    expected_chunk_id=normalized_chunk_id,
                ),
            }
        )
    return {
        "chunk_id": normalized_chunk_id,
        "chunk_order": normalized_chunk_order,
        "entities": localized_entities,
        "relations": localized_relations,
    }


def premerge_document_kg_map_results(
    map_results: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """按规范化 name/type 预归并实体，并只归并已有 Map relations。"""
    if not isinstance(map_results, list):
        raise TypeError("map_results must be an array")
    entity_groups: dict[str, list[dict[str, Any]]] = {}
    seen_entity_ids: set[str] = set()
    seen_relation_ids: set[str] = set()
    entity_chunk_by_id: dict[str, str] = {}
    normalized_results: list[dict[str, Any]] = []
    seen_chunks: set[str] = set()
    seen_orders: set[int] = set()
    for result_index, result in enumerate(map_results):
        if not isinstance(result, dict):
            raise TypeError(f"map_results[{result_index}] must be an object")
        _validate_exact_fields(result, _MAP_RESULT_FIELDS, f"map_results[{result_index}]")
        chunk_id = _required_text(
            result.get("chunk_id"),
            f"map_results[{result_index}].chunk_id",
        )
        chunk_order = _nonnegative_int(
            result.get("chunk_order"),
            f"map_results[{result_index}].chunk_order",
        )
        if chunk_id in seen_chunks or chunk_order in seen_orders:
            raise KnowledgeGraphExtractionError("duplicate Map chunk_id or chunk_order")
        seen_chunks.add(chunk_id)
        seen_orders.add(chunk_order)
        entities = _object_list(
            result.get("entities"),
            f"map_results[{result_index}].entities",
        )
        relations = _object_list(
            result.get("relations"),
            f"map_results[{result_index}].relations",
        )
        normalized_results.append(
            {
                "chunk_id": chunk_id,
                "chunk_order": chunk_order,
                "entities": entities,
                "relations": relations,
            }
        )
        for entity_index, entity in enumerate(entities):
            label = f"map_results[{result_index}].entities[{entity_index}]"
            _validate_exact_fields(entity, _MAP_ENTITY_FIELDS, label)
            local_id = _required_text(entity.get("local_entity_id"), f"{label}.local_entity_id")
            if local_id in seen_entity_ids:
                raise KnowledgeGraphExtractionError(f"duplicate local entity ID: {local_id}")
            seen_entity_ids.add(local_id)
            entity_chunk_by_id[local_id] = chunk_id
            name = _required_text(entity.get("name"), f"{label}.name")
            entity_type = _required_text(entity.get("entity_type"), f"{label}.entity_type")
            support = {
                "local_entity_id": local_id,
                "chunk_order": chunk_order,
                "name": name,
                "entity_type": entity_type,
                "aliases": _string_list(entity.get("aliases"), f"{label}.aliases"),
                "description": _text_field(entity.get("description"), f"{label}.description"),
                "confidence": _optional_confidence(
                    entity.get("confidence"),
                    f"{label}.confidence",
                ),
                "evidence": _evidence_list(
                    entity.get("evidence"),
                    f"{label}.evidence",
                    expected_chunk_id=chunk_id,
                ),
            }
            group_key = canonical_kg_entity_id(name, entity_type)
            entity_groups.setdefault(group_key, []).append(support)

    local_to_premerged: dict[str, str] = {}
    premerged_entities: list[dict[str, Any]] = []
    for supports in entity_groups.values():
        ordered_supports = sorted(supports, key=_entity_support_anchor)
        representative_id = ordered_supports[0]["local_entity_id"]
        for support in ordered_supports:
            local_to_premerged[support["local_entity_id"]] = representative_id
        premerged_entities.append(
            _premerged_entity(representative_id, ordered_supports)
        )
    premerged_entities.sort(key=_premerged_entity_anchor)

    relation_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for result_index, result in enumerate(
        sorted(normalized_results, key=lambda item: (item["chunk_order"], item["chunk_id"]))
    ):
        for relation_index, relation in enumerate(result["relations"]):
            label = f"map_results[{result_index}].relations[{relation_index}]"
            _validate_exact_fields(relation, _MAP_RELATION_FIELDS, label)
            local_relation_id = _required_text(
                relation.get("local_relation_id"),
                f"{label}.local_relation_id",
            )
            if local_relation_id in seen_relation_ids:
                raise KnowledgeGraphExtractionError(
                    f"duplicate local relation ID: {local_relation_id}"
                )
            seen_relation_ids.add(local_relation_id)
            head_id = _required_text(
                relation.get("head_local_entity_id"),
                f"{label}.head_local_entity_id",
            )
            tail_id = _required_text(
                relation.get("tail_local_entity_id"),
                f"{label}.tail_local_entity_id",
            )
            if head_id not in local_to_premerged or tail_id not in local_to_premerged:
                raise KnowledgeGraphExtractionError(
                    f"{label} must reference an existing local entity"
                )
            if (
                entity_chunk_by_id[head_id] != result["chunk_id"]
                or entity_chunk_by_id[tail_id] != result["chunk_id"]
            ):
                raise KnowledgeGraphExtractionError(
                    f"{label} endpoints must come from the same Map result"
                )
            relation_type = _required_text(
                relation.get("relation_type"),
                f"{label}.relation_type",
            )
            support = {
                "local_relation_id": local_relation_id,
                "chunk_order": result["chunk_order"],
                "head_local_entity_id": local_to_premerged[head_id],
                "relation_type": relation_type,
                "tail_local_entity_id": local_to_premerged[tail_id],
                "description": _text_field(
                    relation.get("description"),
                    f"{label}.description",
                ),
                "confidence": _optional_confidence(
                    relation.get("confidence"),
                    f"{label}.confidence",
                ),
                "evidence": _evidence_list(
                    relation.get("evidence"),
                    f"{label}.evidence",
                    expected_chunk_id=result["chunk_id"],
                ),
            }
            key = (
                support["head_local_entity_id"],
                relation_type,
                support["tail_local_entity_id"],
            )
            relation_groups.setdefault(key, []).append(support)

    premerged_relations: list[dict[str, Any]] = []
    for supports in relation_groups.values():
        ordered_supports = sorted(supports, key=_relation_support_anchor)
        premerged_relations.append(
            _premerged_relation(
                ordered_supports[0]["local_relation_id"],
                ordered_supports,
            )
        )
    premerged_relations.sort(
        key=lambda item: (
            item["head_local_entity_id"],
            item["relation_type"],
            item["tail_local_entity_id"],
        )
    )
    return {"entities": premerged_entities, "relations": premerged_relations}


def parse_document_entity_resolution_response(
    payload: str | dict[str, Any],
    *,
    entities: list[dict[str, Any]],
) -> dict[str, list[list[str]]]:
    """校验模型只返回同类型 local entity ID groups。"""
    data = _load_json_object(payload, "resolution")
    _validate_exact_fields(data, frozenset({"groups"}), "resolution")
    if not isinstance(entities, list):
        raise TypeError("entities must be an array")
    entity_types: dict[str, str] = {}
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            raise TypeError(f"entities[{index}] must be an object")
        local_id = _required_text(
            entity.get("local_entity_id"),
            f"entities[{index}].local_entity_id",
        )
        if local_id in entity_types:
            raise KnowledgeGraphExtractionError(f"duplicate local entity ID: {local_id}")
        entity_types[local_id] = _required_text(
            entity.get("entity_type"),
            f"entities[{index}].entity_type",
        )
    groups = data.get("groups")
    if not isinstance(groups, list):
        raise KnowledgeGraphExtractionError("groups must be an array")
    parsed_groups: list[list[str]] = []
    seen_ids: set[str] = set()
    for group_index, group in enumerate(groups):
        if not isinstance(group, list):
            raise KnowledgeGraphExtractionError(f"groups[{group_index}] must be an array")
        if len(group) < 2:
            raise KnowledgeGraphExtractionError(
                f"groups[{group_index}] must contain at least two IDs"
            )
        parsed_group: list[str] = []
        group_types: set[str] = set()
        for member_index, member in enumerate(group):
            if not isinstance(member, str):
                raise KnowledgeGraphExtractionError(
                    f"groups[{group_index}][{member_index}] must be a string"
                )
            local_id = member.strip()
            if not local_id:
                raise KnowledgeGraphExtractionError(
                    f"groups[{group_index}][{member_index}] is required"
                )
            if local_id not in entity_types:
                raise KnowledgeGraphExtractionError(
                    f"unknown local entity ID: {local_id}"
                )
            if local_id in seen_ids:
                raise KnowledgeGraphExtractionError(
                    f"local entity ID appears more than once: {local_id}"
                )
            seen_ids.add(local_id)
            group_types.add(entity_types[local_id])
            parsed_group.append(local_id)
        if len(group_types) != 1:
            raise KnowledgeGraphExtractionError(
                f"groups[{group_index}] members must have the same entity_type"
            )
        parsed_groups.append(parsed_group)
    return {"groups": parsed_groups}


def reduce_document_kg(
    premerged: dict[str, list[dict[str, Any]]],
    resolution: dict[str, list[list[str]]],
) -> dict[str, list[dict[str, Any]]]:
    """由代码选 canonical entity 并重映射、去重 Map 中已有关系。"""
    if not isinstance(premerged, dict):
        raise TypeError("premerged must be an object")
    _validate_exact_fields(
        premerged,
        frozenset({"entities", "relations"}),
        "premerged",
    )
    entities = _object_list(premerged.get("entities"), "premerged.entities")
    relations = _object_list(premerged.get("relations"), "premerged.relations")
    parsed_resolution = parse_document_entity_resolution_response(
        resolution,
        entities=entities,
    )
    entity_by_local_id: dict[str, dict[str, Any]] = {}
    for index, entity in enumerate(entities):
        local_id = _required_text(
            entity.get("local_entity_id"),
            f"premerged.entities[{index}].local_entity_id",
        )
        if local_id in entity_by_local_id:
            raise KnowledgeGraphExtractionError(f"duplicate local entity ID: {local_id}")
        supports = entity.get("_supports")
        if not isinstance(supports, list) or not supports:
            raise KnowledgeGraphExtractionError(
                f"premerged.entities[{index}] is missing deterministic supports"
            )
        entity_by_local_id[local_id] = entity

    grouped_ids = {
        local_id
        for group in parsed_resolution["groups"]
        for local_id in group
    }
    equivalence_classes = [list(group) for group in parsed_resolution["groups"]]
    equivalence_classes.extend(
        [local_id]
        for local_id in entity_by_local_id
        if local_id not in grouped_ids
    )
    equivalence_classes.sort(
        key=lambda group: min(
            _premerged_entity_anchor(entity_by_local_id[local_id])
            for local_id in group
        )
    )

    local_to_canonical: dict[str, str] = {}
    final_entities: list[dict[str, Any]] = []
    for group in equivalence_classes:
        supports = [
            copy.deepcopy(support)
            for local_id in group
            for support in entity_by_local_id[local_id]["_supports"]
        ]
        ordered_supports = sorted(supports, key=_entity_support_anchor)
        entity_type = ordered_supports[0]["entity_type"]
        if any(support["entity_type"] != entity_type for support in ordered_supports):
            raise KnowledgeGraphExtractionError(
                "resolved entity supports must have the same entity_type"
            )
        canonical_name = ordered_supports[0]["name"]
        canonical_id = canonical_kg_entity_id(canonical_name, entity_type)
        for local_id in group:
            local_to_canonical[local_id] = canonical_id
        final_entities.append(
            {
                "id": canonical_id,
                "name": canonical_name,
                "entity_type": entity_type,
                "aliases": _merge_aliases(ordered_supports, canonical_name),
                "description": _select_description(ordered_supports),
                "status": "needs_review",
                "confidence": _max_confidence(ordered_supports),
                "evidence": _merge_evidence(ordered_supports),
            }
        )

    final_entity_by_id = {entity["id"]: entity for entity in final_entities}
    relation_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for index, relation in enumerate(relations):
        head_local_id = _required_text(
            relation.get("head_local_entity_id"),
            f"premerged.relations[{index}].head_local_entity_id",
        )
        tail_local_id = _required_text(
            relation.get("tail_local_entity_id"),
            f"premerged.relations[{index}].tail_local_entity_id",
        )
        if head_local_id not in local_to_canonical or tail_local_id not in local_to_canonical:
            raise KnowledgeGraphExtractionError(
                f"premerged.relations[{index}] references an unknown local entity"
            )
        supports = relation.get("_supports")
        if not isinstance(supports, list) or not supports:
            raise KnowledgeGraphExtractionError(
                f"premerged.relations[{index}] is missing deterministic supports"
            )
        relation_type = _required_text(
            relation.get("relation_type"),
            f"premerged.relations[{index}].relation_type",
        )
        key = (
            local_to_canonical[head_local_id],
            relation_type,
            local_to_canonical[tail_local_id],
        )
        relation_groups.setdefault(key, []).extend(copy.deepcopy(supports))

    final_relations: list[dict[str, Any]] = []
    for (head_id, relation_type, tail_id), supports in sorted(relation_groups.items()):
        ordered_supports = sorted(supports, key=_relation_support_anchor)
        head = final_entity_by_id[head_id]
        tail = final_entity_by_id[tail_id]
        final_relations.append(
            {
                "id": canonical_kg_relation_id(head_id, relation_type, tail_id),
                "head_entity_id": head_id,
                "head_entity_name": head["name"],
                "head_entity_type": head["entity_type"],
                "relation_type": relation_type,
                "tail_entity_id": tail_id,
                "tail_entity_name": tail["name"],
                "tail_entity_type": tail["entity_type"],
                "description": _select_description(ordered_supports),
                "status": "needs_review",
                "confidence": _max_confidence(ordered_supports),
                "evidence": _merge_evidence(ordered_supports),
            }
        )
    return {"entities": final_entities, "relations": final_relations}


def _premerged_entity(
    representative_id: str,
    supports: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成 resolution 候选，并保留 Reduce 所需的隐藏确定性支持项。"""
    canonical_name = supports[0]["name"]
    return {
        "local_entity_id": representative_id,
        "name": canonical_name,
        "entity_type": supports[0]["entity_type"],
        "aliases": _merge_aliases(supports, canonical_name),
        "description": _select_description(supports),
        "confidence": _max_confidence(supports),
        "evidence": _merge_evidence(supports),
        "_supports": copy.deepcopy(supports),
    }


def _premerged_relation(
    representative_id: str,
    supports: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成 premerge relation，并保留所有 Map 边的确定性支持项。"""
    return {
        "local_relation_id": representative_id,
        "head_local_entity_id": supports[0]["head_local_entity_id"],
        "relation_type": supports[0]["relation_type"],
        "tail_local_entity_id": supports[0]["tail_local_entity_id"],
        "description": _select_description(supports),
        "confidence": _max_confidence(supports),
        "evidence": _merge_evidence(supports),
        "_supports": copy.deepcopy(supports),
    }


def _merge_aliases(supports: list[dict[str, Any]], canonical_name: str) -> list[str]:
    """按支持项顺序合并别名，并把非 canonical 名称保留为可审核 alias。"""
    aliases: list[str] = []
    seen = {_normalized_text(canonical_name)}
    for support in sorted(supports, key=_entity_support_anchor):
        values = [support["name"], *support["aliases"]]
        for value in values:
            key = _normalized_text(value)
            if key in seen:
                continue
            seen.add(key)
            aliases.append(value)
    return aliases


def _select_description(supports: list[dict[str, Any]]) -> str:
    """按置信度降序、长度降序、文本升序选择唯一描述。"""
    candidates = [
        (support["description"], support["confidence"])
        for support in supports
        if support["description"]
    ]
    if not candidates:
        return ""
    return min(
        candidates,
        key=lambda item: (
            -(item[1] if item[1] is not None else -1.0),
            -len(item[0]),
            item[0],
        ),
    )[0]


def _max_confidence(supports: list[dict[str, Any]]) -> float | None:
    """取支持项最大置信度；全部为空时保持空值。"""
    values = [support["confidence"] for support in supports if support["confidence"] is not None]
    return max(values) if values else None


def _merge_evidence(supports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 chunk/order/offset 稳定并集 evidence，最终不泄露内部 chunk_order。"""
    unique: dict[str, tuple[int, dict[str, Any]]] = {}
    for support in supports:
        chunk_order = _nonnegative_int(support.get("chunk_order"), "support.chunk_order")
        for evidence in support["evidence"]:
            copied = copy.deepcopy(evidence)
            key = _canonical_json(copied)
            previous = unique.get(key)
            if previous is None or chunk_order < previous[0]:
                unique[key] = (chunk_order, copied)
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item[0],
            item[1]["char_start"],
            item[1]["char_end"],
            item[1]["excerpt"],
            _canonical_json(item[1]),
        ),
    )
    return [evidence for _chunk_order, evidence in ordered]


def _entity_support_anchor(support: dict[str, Any]) -> tuple[int, int, str]:
    """生成 canonical name 的稳定选择键。"""
    first_offset = min(evidence["char_start"] for evidence in support["evidence"])
    return (
        _nonnegative_int(support.get("chunk_order"), "support.chunk_order"),
        first_offset,
        _required_text(support.get("local_entity_id"), "support.local_entity_id"),
    )


def _relation_support_anchor(support: dict[str, Any]) -> tuple[int, int, str]:
    """生成重复 Map relation 的稳定归并顺序键。"""
    first_offset = min(evidence["char_start"] for evidence in support["evidence"])
    return (
        _nonnegative_int(support.get("chunk_order"), "support.chunk_order"),
        first_offset,
        _required_text(support.get("local_relation_id"), "support.local_relation_id"),
    )


def _premerged_entity_anchor(entity: dict[str, Any]) -> tuple[int, int, str]:
    """读取 premerge 实体最早支持项，供输出与等价类稳定排序。"""
    supports = entity.get("_supports")
    if not isinstance(supports, list) or not supports:
        raise KnowledgeGraphExtractionError("premerged entity has no supports")
    return min(_entity_support_anchor(support) for support in supports)


def _evidence_list(
    value: Any,
    label: str,
    *,
    expected_chunk_id: str,
) -> list[dict[str, Any]]:
    """复制固定 document evidence shape，并校验 offset 半开区间。"""
    items = _object_list(value, label)
    if not items:
        raise KnowledgeGraphExtractionError(f"{label} is required")
    return [
        _document_evidence(item, f"{label}[{index}]", expected_chunk_id=expected_chunk_id)
        for index, item in enumerate(items)
    ]


def _document_evidence(
    value: dict[str, Any],
    label: str,
    *,
    expected_chunk_id: str,
) -> dict[str, Any]:
    """校验单条 Map evidence 只引用当前文档切片及精确 code-point offset。"""
    _validate_exact_fields(value, _DOCUMENT_EVIDENCE_FIELDS, label)
    source_type = _required_text(value.get("source_type"), f"{label}.source_type")
    if source_type != "document":
        raise KnowledgeGraphExtractionError(f"{label}.source_type must be document")
    source_chunk_id = _required_text(
        value.get("source_chunk_id"),
        f"{label}.source_chunk_id",
    )
    if source_chunk_id != expected_chunk_id:
        raise KnowledgeGraphExtractionError(f"{label} must reference the current chunk")
    excerpt = _required_text(value.get("excerpt"), f"{label}.excerpt")
    char_start = _nonnegative_int(value.get("char_start"), f"{label}.char_start")
    char_end = _nonnegative_int(value.get("char_end"), f"{label}.char_end")
    if char_end <= char_start or char_end - char_start != len(excerpt):
        raise KnowledgeGraphExtractionError(f"{label} has invalid char offsets")
    return {
        "source_type": "document",
        "source_id": _required_text(value.get("source_id"), f"{label}.source_id"),
        "source_chunk_id": source_chunk_id,
        "source_title": _required_text(
            value.get("source_title"),
            f"{label}.source_title",
        ),
        "section_path": _string_list(
            value.get("section_path"),
            f"{label}.section_path",
        ),
        "page_start": _optional_nonnegative_int(
            value.get("page_start"),
            f"{label}.page_start",
        ),
        "page_end": _optional_nonnegative_int(
            value.get("page_end"),
            f"{label}.page_end",
        ),
        "excerpt": excerpt,
        "char_start": char_start,
        "char_end": char_end,
    }


def _document_local_id(
    kind: str,
    *,
    job_id: str,
    chunk_id: str,
    provisional_id: str,
) -> str:
    """生成 job/chunk 域内 local ID，不把 provisional canonical ID 当最终事实 ID。"""
    stable_digest = _canonical_sha256(
        {
            "kind": kind,
            "provisional_id": provisional_id,
        }
    )[:16]
    scope_digest = _canonical_sha256(
        {
            "kind": kind,
            "job_id": job_id,
            "chunk_id": chunk_id,
            "provisional_id": provisional_id,
        }
    )[:16]
    prefix = "kg_doc_local" if kind == "entity" else "kg_doc_local_rel"
    return f"{prefix}_{stable_digest}_{scope_digest}"


def _load_json_object(payload: str | dict[str, Any], label: str) -> dict[str, Any]:
    """读取严格 JSON object，不把非对象或坏 JSON 降级为空结果。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise KnowledgeGraphExtractionError(f"invalid {label} JSON payload") from exc
    if not isinstance(payload, dict):
        raise KnowledgeGraphExtractionError(f"{label} payload must be a JSON object")
    return dict(payload)


def _validate_exact_fields(
    value: dict[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    """校验固定内部 shape，缺失或额外字段都显式失败。"""
    actual = set(value)
    missing = sorted(expected.difference(actual))
    if missing:
        raise KnowledgeGraphExtractionError(
            f"missing {label} fields: {', '.join(missing)}"
        )
    unsupported = sorted(actual.difference(expected))
    if unsupported:
        raise KnowledgeGraphExtractionError(
            f"unsupported {label} fields: {', '.join(unsupported)}"
        )


def _object_list(value: Any, label: str) -> list[dict[str, Any]]:
    """读取对象数组，禁止静默丢弃错类型元素。"""
    if not isinstance(value, list):
        raise KnowledgeGraphExtractionError(f"{label} must be an array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise KnowledgeGraphExtractionError(f"{label}[{index}] must be an object")
        result.append(dict(item))
    return result


def _required_text(value: Any, label: str) -> str:
    """读取必填文本并只裁剪边界空白。"""
    if not isinstance(value, str):
        raise KnowledgeGraphExtractionError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise KnowledgeGraphExtractionError(f"{label} is required")
    return text


def _text_field(value: Any, label: str) -> str:
    """读取允许为空的固定文本字段。"""
    if not isinstance(value, str):
        raise KnowledgeGraphExtractionError(f"{label} must be a string")
    return value.strip()


def _string_list(value: Any, label: str) -> list[str]:
    """读取字符串数组，不接受逗号文本或非字符串元素。"""
    if not isinstance(value, list):
        raise KnowledgeGraphExtractionError(f"{label} must be an array")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise KnowledgeGraphExtractionError(f"{label}[{index}] must be a string")
        text = item.strip()
        if text:
            result.append(text)
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    """读取非负整数，布尔值不能冒充数字。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeGraphExtractionError(f"{label} must be an integer")
    if value < 0:
        raise KnowledgeGraphExtractionError(f"{label} must be non-negative")
    return value


def _optional_nonnegative_int(value: Any, label: str) -> int | None:
    """读取可空非负整数，保持页码零值。"""
    if value is None:
        return None
    return _nonnegative_int(value, label)


def _optional_confidence(value: Any, label: str) -> float | None:
    """读取可空 0-1 置信度，拒绝 bool、NaN 和 Infinity。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KnowledgeGraphExtractionError(f"{label} must be numeric")
    confidence = float(value)
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise KnowledgeGraphExtractionError(f"{label} must be between 0 and 1")
    return confidence


def _normalized_text(value: str) -> str:
    """生成 name/alias 稳定去重键，不做 Unicode 或模糊归一化。"""
    return " ".join(value.strip().lower().split())


def _canonical_json(payload: Any) -> str:
    """生成 manifest/local ID 使用的 canonical JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(payload: Any) -> str:
    """对 canonical JSON 计算完整 SHA-256 指纹。"""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
