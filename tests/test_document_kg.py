import copy

import pytest

from cyclops.document_kg import (
    build_document_kg_manifest,
    localize_document_kg_map_result,
    parse_document_entity_resolution_response,
    premerge_document_kg_map_results,
    reduce_document_kg,
)
from cyclops.kg import (
    KnowledgeGraphExtractionError,
    canonical_kg_entity_id,
    canonical_kg_relation_id,
)
from cyclops.kg_ai import KnowledgeGraphAiAssistant


class _FakeChat:
    """记录 KG AI 调用并返回测试指定的单个模型响应。"""

    def __init__(self, response):
        """保存固定响应，关键约束是测试不访问真实模型服务。"""
        self.response = response
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        """记录两段提示词并返回固定响应，供调用边界断言。"""
        self.calls.append((system_prompt, user_prompt))
        return self.response


def _import_file(*, file_id="imp_1", title="产品手册.pdf"):
    """构造文档 manifest 测试文件，字段保持数据库当前命名。"""
    return {"id": file_id, "original_name": title, "is_disabled": False}


def _chunk(
    chunk_id,
    *,
    index,
    text,
    section_path=None,
    page_start=None,
    page_end=None,
    disabled=False,
):
    """构造文档 manifest 测试切片，显式保留所有 fingerprint locator。"""
    return {
        "id": chunk_id,
        "file_id": "imp_1",
        "chunk_index": index,
        "source_text": text,
        "section_path": list(section_path or []),
        "page_start": page_start,
        "page_end": page_end,
        "is_disabled": disabled,
    }


def _evidence(
    excerpt,
    *,
    chunk_id,
    char_start=0,
    source_title="产品手册.pdf",
    section_path=None,
    page_start=None,
    page_end=None,
):
    """构造带精确 code-point offset 的文档 KG evidence。"""
    return {
        "source_type": "document",
        "source_id": "imp_1",
        "source_chunk_id": chunk_id,
        "source_title": source_title,
        "section_path": list(section_path or []),
        "page_start": page_start,
        "page_end": page_end,
        "excerpt": excerpt,
        "char_start": char_start,
        "char_end": char_start + len(excerpt),
    }


def _map_entity(
    local_id,
    name,
    *,
    entity_type="product_platform_module",
    chunk_id="chunk_1",
    aliases=None,
    description="",
    confidence=None,
    excerpt=None,
    char_start=0,
):
    """构造 premerge 输入实体，local ID 与证据位置均由测试显式控制。"""
    evidence_excerpt = excerpt or name
    return {
        "local_entity_id": local_id,
        "name": name,
        "entity_type": entity_type,
        "aliases": list(aliases or []),
        "description": description,
        "confidence": confidence,
        "evidence": [
            _evidence(
                evidence_excerpt,
                chunk_id=chunk_id,
                char_start=char_start,
            )
        ],
    }


def _map_result(*, chunk_id, chunk_order, entities, relations=None):
    """构造单切片 localized Map 结果，供纯函数归并测试复用。"""
    return {
        "chunk_id": chunk_id,
        "chunk_order": chunk_order,
        "entities": entities,
        "relations": list(relations or []),
    }


def _map_relation(
    local_id,
    head_id,
    relation_type,
    tail_id,
    *,
    chunk_id,
    description="",
    confidence=None,
    excerpt="关系证据",
    char_start=0,
):
    """构造只引用 Map 端点的 localized relation。"""
    return {
        "local_relation_id": local_id,
        "head_local_entity_id": head_id,
        "relation_type": relation_type,
        "tail_local_entity_id": tail_id,
        "description": description,
        "confidence": confidence,
        "evidence": [
            _evidence(excerpt, chunk_id=chunk_id, char_start=char_start)
        ],
    }


def _resolution_entities():
    """构造 resolution 校验矩阵使用的同类型与跨类型候选。"""
    return [
        {
            "local_entity_id": "kg_doc_local_console",
            "name": "控制台",
            "entity_type": "product_platform_module",
            "aliases": ["Console"],
            "description": "客服控制台入口",
            "confidence": 0.91,
            "evidence": [{"excerpt": "禁止泄漏的控制台证据"}],
            "_supports": [{"private": "禁止泄漏的支持项"}],
            "relations": [{"private": "禁止泄漏的关系"}],
        },
        {
            "local_entity_id": "kg_doc_local_admin",
            "name": "管理后台",
            "entity_type": "product_platform_module",
            "aliases": ["后台"],
            "description": "管理员使用的后台入口",
        },
        {
            "local_entity_id": "kg_doc_local_permission",
            "name": "管理员权限",
            "entity_type": "role_permission_channel",
            "aliases": [],
            "description": "进入后台所需权限",
        },
    ]


def test_document_manifest_is_ordered_and_fingerprints_every_locator():
    """manifest 顺序与 fingerprint 必须覆盖 ID、正文、页码、章节和文件标题。"""
    manifest = build_document_kg_manifest(
        _import_file(),
        [
            _chunk("b", index=2, text="B", page_start=2),
            _chunk("a", index=1, text="A", section_path=["旧章节"]),
        ],
    )

    assert [item["chunk_id"] for item in manifest["items"]] == ["a", "b"]
    assert [item["chunk_order"] for item in manifest["items"]] == [0, 1]
    changed = build_document_kg_manifest(
        _import_file(),
        [
            _chunk("a", index=1, text="A", section_path=["新章节"]),
            _chunk("b", index=2, text="B", page_start=2),
        ],
    )
    assert changed["fingerprint"] != manifest["fingerprint"]


def test_document_manifest_rejects_enabled_blank_chunk():
    """启用但无正文的切片不能被静默跳过。"""
    with pytest.raises(ValueError, match="source_text"):
        build_document_kg_manifest(
            _import_file(),
            [_chunk("chunk_1", index=0, text="  ")],
        )


def test_document_manifest_ignores_disabled_chunks_but_requires_one_enabled_chunk():
    """禁用切片不进入 generation，且空 generation 不能生成无效父任务。"""
    enabled = _chunk("enabled", index=1, text="正文")
    disabled = _chunk("disabled", index=0, text="旧正文", disabled=True)

    manifest = build_document_kg_manifest(_import_file(), [disabled, enabled])

    assert [item["chunk_id"] for item in manifest["items"]] == ["enabled"]
    with pytest.raises(ValueError, match="enabled chunk"):
        build_document_kg_manifest(_import_file(), [disabled])


def test_localize_map_result_is_stable_job_scoped_and_rewrites_relation_endpoints():
    """local ID 必须稳定且隔离 job/chunk，并让关系只引用同一 Map 的 local 实体。"""
    console_id = canonical_kg_entity_id("控制台", "product_platform_module")
    permission_id = canonical_kg_entity_id("管理员权限", "role_permission_channel")
    relation_id = canonical_kg_relation_id(console_id, "requires", permission_id)
    extraction = {
        "entities": [
            {
                "id": console_id,
                "name": "控制台",
                "entity_type": "product_platform_module",
                "aliases": [],
                "description": "",
                "status": "needs_review",
                "confidence": 0.8,
                "evidence": [_evidence("控制台", chunk_id="chunk_1")],
            },
            {
                "id": permission_id,
                "name": "管理员权限",
                "entity_type": "role_permission_channel",
                "aliases": [],
                "description": "",
                "status": "needs_review",
                "confidence": 0.7,
                "evidence": [_evidence("管理员权限", chunk_id="chunk_1", char_start=4)],
            },
        ],
        "relations": [
            {
                "id": relation_id,
                "head_entity_id": console_id,
                "head_entity_name": "控制台",
                "head_entity_type": "product_platform_module",
                "relation_type": "requires",
                "tail_entity_id": permission_id,
                "tail_entity_name": "管理员权限",
                "tail_entity_type": "role_permission_channel",
                "description": "",
                "status": "needs_review",
                "confidence": 0.75,
                "evidence": [_evidence("需要管理员权限", chunk_id="chunk_1", char_start=8)],
            }
        ],
    }

    localized = localize_document_kg_map_result(
        extraction,
        job_id="kg_job_1",
        chunk_id="chunk_1",
        chunk_order=0,
    )
    repeated = localize_document_kg_map_result(
        extraction,
        job_id="kg_job_1",
        chunk_id="chunk_1",
        chunk_order=0,
    )
    other_job = localize_document_kg_map_result(
        extraction,
        job_id="kg_job_2",
        chunk_id="chunk_1",
        chunk_order=0,
    )

    assert localized == repeated
    assert localized["entities"][0]["local_entity_id"] != other_job["entities"][0][
        "local_entity_id"
    ]
    local_entity_ids = {item["local_entity_id"] for item in localized["entities"]}
    relation = localized["relations"][0]
    assert relation["head_local_entity_id"] in local_entity_ids
    assert relation["tail_local_entity_id"] in local_entity_ids
    assert set(relation) == {
        "local_relation_id",
        "head_local_entity_id",
        "relation_type",
        "tail_local_entity_id",
        "description",
        "confidence",
        "evidence",
    }


def test_local_id_tie_break_keeps_canonical_name_stable_across_generations():
    """job 域隔离不能改变同一 provisional 实体之间的稳定排序。"""
    shared_evidence = _evidence("控制台与管理后台是同一入口", chunk_id="chunk_1")
    extraction = {
        "entities": [
            {
                "id": canonical_kg_entity_id("控制台", "product_platform_module"),
                "name": "控制台",
                "entity_type": "product_platform_module",
                "aliases": [],
                "description": "",
                "status": "needs_review",
                "confidence": None,
                "evidence": [shared_evidence],
            },
            {
                "id": canonical_kg_entity_id("管理后台", "product_platform_module"),
                "name": "管理后台",
                "entity_type": "product_platform_module",
                "aliases": [],
                "description": "",
                "status": "needs_review",
                "confidence": None,
                "evidence": [shared_evidence],
            },
        ],
        "relations": [],
    }
    canonical_names = set()

    for generation in range(1, 21):
        localized = localize_document_kg_map_result(
            extraction,
            job_id=f"kg_job_{generation}",
            chunk_id="chunk_1",
            chunk_order=0,
        )
        premerged = premerge_document_kg_map_results([localized])
        local_ids = [item["local_entity_id"] for item in premerged["entities"]]
        reduced = reduce_document_kg(premerged, {"groups": [local_ids]})
        canonical_names.add(reduced["entities"][0]["name"])

    assert len(canonical_names) == 1


def test_premerge_is_exact_name_and_type_before_resolution():
    """精确同名同类型先合并，别名实体保留 local ID 给 resolution。"""
    premerged = premerge_document_kg_map_results(
        [
            _map_result(
                chunk_id="chunk_1",
                chunk_order=0,
                entities=[_map_entity("local_console_1", "控制台", chunk_id="chunk_1")],
            ),
            _map_result(
                chunk_id="chunk_2",
                chunk_order=1,
                entities=[_map_entity("local_console_2", "控制台", chunk_id="chunk_2")],
            ),
            _map_result(
                chunk_id="chunk_3",
                chunk_order=2,
                entities=[_map_entity("local_admin", "管理后台", chunk_id="chunk_3")],
            ),
        ]
    )

    assert [item["name"] for item in premerged["entities"]] == ["控制台", "管理后台"]


def test_premerge_rejects_relation_endpoints_from_another_map_result():
    """单片 Map relation 不能引用另一切片才出现的 local entity。"""
    with pytest.raises(KnowledgeGraphExtractionError, match="same Map result"):
        premerge_document_kg_map_results(
            [
                _map_result(
                    chunk_id="chunk_1",
                    chunk_order=0,
                    entities=[
                        _map_entity("local_console", "控制台", chunk_id="chunk_1")
                    ],
                    relations=[
                        _map_relation(
                            "rel_cross_map",
                            "local_console",
                            "requires",
                            "local_permission",
                            chunk_id="chunk_1",
                        )
                    ],
                ),
                _map_result(
                    chunk_id="chunk_2",
                    chunk_order=1,
                    entities=[
                        _map_entity(
                            "local_permission",
                            "管理员权限",
                            entity_type="role_permission_channel",
                            chunk_id="chunk_2",
                        )
                    ],
                ),
            ]
        )


def test_resolution_prompt_only_contains_local_entity_candidates():
    """resolution 输入只投影实体候选，不泄漏关系、证据或内部支持项。"""
    prompt = KnowledgeGraphAiAssistant.entity_resolution_user_prompt(
        _resolution_entities()
    )

    for field in (
        "local_entity_id",
        "name",
        "entity_type",
        "aliases",
        "description",
    ):
        assert f'"{field}"' in prompt
    for field in ("confidence", "relations", "evidence", "_supports"):
        assert f'"{field}"' not in prompt
    assert "kg_doc_local_console" in prompt
    assert "禁止泄漏" not in prompt


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ('{"groups": [], "summary": "extra"}', "unsupported resolution fields"),
        (
            '{"groups": [["kg_doc_local_console", "kg_doc_local_unknown"]]}',
            "unknown local entity ID",
        ),
        (
            '{"groups": [["kg_doc_local_console", "kg_doc_local_permission"]]}',
            "same entity_type",
        ),
    ],
)
def test_resolution_model_output_is_validated_before_db_stage(response, message):
    """模型额外字段、未知 ID 与跨类型分组必须在进入持久化前失败。"""
    chat = _FakeChat(response)

    with pytest.raises(KnowledgeGraphExtractionError, match=message):
        KnowledgeGraphAiAssistant(chat).resolve_document_entities(
            entities=_resolution_entities()
        )

    assert len(chat.calls) == 1


def test_resolution_model_returns_validated_groups():
    """合法模型输出经过统一 parser 校验后原样返回互斥实体分组。"""
    chat = _FakeChat(
        '{"groups": [["kg_doc_local_console", "kg_doc_local_admin"]]}'
    )

    result = KnowledgeGraphAiAssistant(chat).resolve_document_entities(
        entities=_resolution_entities()
    )

    assert result == {
        "groups": [["kg_doc_local_console", "kg_doc_local_admin"]]
    }
    assert len(chat.calls) == 1


def test_extract_preserves_unicode_source_text_offsets_alongside_resolution_api():
    """新增 resolution 阶段不能绕过 Map parser 的 Unicode 原文 offset 匹配。"""
    source_text = "🔧先检查权限，再确认权限。"
    chat = _FakeChat(
        """
        {"entities":[{"name":"管理员权限","entity_type":"role_permission_channel","aliases":[],"description":"后台访问权限","confidence":0.9,"evidence":[{"excerpt":"权限"}]}],"relations":[]}
        """
    )

    result = KnowledgeGraphAiAssistant(chat).extract(
        source_text=source_text,
        source={
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
        },
    )

    evidence = result["entities"][0]["evidence"][0]
    assert (evidence["char_start"], evidence["char_end"]) == (4, 6)
    assert source_text[evidence["char_start"] : evidence["char_end"]] == "权限"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"groups": [], "summary": "extra"}, "unsupported resolution fields"),
        ({"groups": {}}, "groups must be an array"),
        ({"groups": [["kg_doc_local_console"]]}, "at least two"),
        (
            {"groups": [["kg_doc_local_console", "kg_doc_local_unknown"]]},
            "unknown local entity ID",
        ),
        (
            {
                "groups": [
                    ["kg_doc_local_console", "kg_doc_local_admin"],
                    ["kg_doc_local_console", "kg_doc_local_admin"],
                ]
            },
            "more than once",
        ),
        (
            {"groups": [["kg_doc_local_console", "kg_doc_local_permission"]]},
            "same entity_type",
        ),
        (
            {"groups": [["kg_doc_local_console", 7]]},
            r"groups\[0\]\[1\] must be a string",
        ),
    ],
)
def test_resolution_rejects_unknown_duplicate_and_cross_type_ids(payload, message):
    """模型只能对已存在且同类型 local ID 做互斥分组。"""
    with pytest.raises(KnowledgeGraphExtractionError, match=message):
        parse_document_entity_resolution_response(
            payload,
            entities=_resolution_entities(),
        )


def test_resolution_accepts_disjoint_same_type_groups_and_keeps_singletons_implicit():
    """合法 resolution 只保存互斥等价组，未列实体保持隐式 singleton。"""
    result = parse_document_entity_resolution_response(
        {"groups": [["kg_doc_local_admin", "kg_doc_local_console"]]},
        entities=_resolution_entities(),
    )

    assert result == {
        "groups": [["kg_doc_local_admin", "kg_doc_local_console"]]
    }


def test_reduce_only_rewrites_relations_found_by_map():
    """resolution 只能改端点等价类，不能提供或制造新关系。"""
    console = _map_entity(
        "local_console",
        "控制台",
        chunk_id="chunk_1",
        aliases=["Console"],
        description="控制台入口说明",
        confidence=0.8,
        char_start=0,
    )
    permission_1 = _map_entity(
        "local_permission_1",
        "管理员权限",
        entity_type="role_permission_channel",
        chunk_id="chunk_1",
        confidence=0.7,
        char_start=8,
    )
    admin = _map_entity(
        "local_admin",
        "管理后台",
        chunk_id="chunk_2",
        aliases=["后台"],
        description="后台",
        confidence=0.95,
        char_start=2,
    )
    permission_2 = _map_entity(
        "local_permission_2",
        "管理员权限",
        entity_type="role_permission_channel",
        chunk_id="chunk_2",
        confidence=0.9,
        char_start=10,
    )
    cache = _map_entity(
        "local_cache",
        "刷新缓存",
        entity_type="feature_ui_action",
        chunk_id="chunk_2",
        confidence=0.6,
        char_start=15,
    )
    premerged = premerge_document_kg_map_results(
        [
            _map_result(
                chunk_id="chunk_1",
                chunk_order=0,
                entities=[console, permission_1],
                relations=[
                    _map_relation(
                        "rel_1",
                        "local_console",
                        "requires",
                        "local_permission_1",
                        chunk_id="chunk_1",
                        excerpt="控制台需要管理员权限",
                        confidence=0.7,
                    )
                ],
            ),
            _map_result(
                chunk_id="chunk_2",
                chunk_order=1,
                entities=[admin, permission_2, cache],
                relations=[
                    _map_relation(
                        "rel_2",
                        "local_admin",
                        "requires",
                        "local_permission_2",
                        chunk_id="chunk_2",
                        excerpt="后台需要管理员权限",
                        confidence=0.9,
                    ),
                    _map_relation(
                        "rel_3",
                        "local_admin",
                        "resolves_by",
                        "local_cache",
                        chunk_id="chunk_2",
                        excerpt="后台通过刷新缓存恢复",
                        confidence=0.8,
                        char_start=20,
                    ),
                ],
            ),
        ]
    )

    reduced = reduce_document_kg(
        premerged,
        {"groups": [["local_console", "local_admin"]]},
    )

    entity_names = {item["id"]: item["name"] for item in reduced["entities"]}
    triples = {
        (
            entity_names[item["head_entity_id"]],
            item["relation_type"],
            entity_names[item["tail_entity_id"]],
        )
        for item in reduced["relations"]
    }
    assert triples == {
        ("控制台", "requires", "管理员权限"),
        ("控制台", "resolves_by", "刷新缓存"),
    }
    requires = next(item for item in reduced["relations"] if item["relation_type"] == "requires")
    assert [item["excerpt"] for item in requires["evidence"]] == [
        "控制台需要管理员权限",
        "后台需要管理员权限",
    ]


def test_reduce_uses_deterministic_entity_fields_and_public_canonical_ids():
    """最终实体字段必须按已确认排序规则选取，并复用公开 canonical ID。"""
    premerged = premerge_document_kg_map_results(
        [
            _map_result(
                chunk_id="chunk_1",
                chunk_order=0,
                entities=[
                    _map_entity(
                        "local_console",
                        "控制台",
                        chunk_id="chunk_1",
                        aliases=["Console", "后台"],
                        description="更长但置信度较低的控制台描述",
                        confidence=0.8,
                        char_start=5,
                    )
                ],
            ),
            _map_result(
                chunk_id="chunk_2",
                chunk_order=1,
                entities=[
                    _map_entity(
                        "local_admin",
                        "管理后台",
                        chunk_id="chunk_2",
                        aliases=["后台", "Admin"],
                        description="短描述",
                        confidence=0.95,
                        char_start=1,
                    )
                ],
            ),
        ]
    )

    reduced = reduce_document_kg(
        premerged,
        {"groups": [["local_admin", "local_console"]]},
    )

    assert len(reduced["entities"]) == 1
    entity = reduced["entities"][0]
    assert entity["name"] == "控制台"
    assert entity["aliases"] == ["Console", "后台", "管理后台", "Admin"]
    assert entity["description"] == "短描述"
    assert entity["confidence"] == 0.95
    assert entity["id"] == canonical_kg_entity_id(
        "控制台", "product_platform_module"
    )
    assert [item["source_chunk_id"] for item in entity["evidence"]] == [
        "chunk_1",
        "chunk_2",
    ]


def test_reduce_is_independent_of_resolution_group_and_member_order():
    """resolution JSON 排列不能改变代码生成的 canonical snapshot。"""
    map_results = [
        _map_result(
            chunk_id="chunk_1",
            chunk_order=0,
            entities=[_map_entity("local_a", "控制台", chunk_id="chunk_1")],
        ),
        _map_result(
            chunk_id="chunk_2",
            chunk_order=1,
            entities=[_map_entity("local_b", "管理后台", chunk_id="chunk_2")],
        ),
    ]
    premerged = premerge_document_kg_map_results(map_results)

    forward = reduce_document_kg(
        copy.deepcopy(premerged),
        {"groups": [["local_a", "local_b"]]},
    )
    reversed_result = reduce_document_kg(
        copy.deepcopy(premerged),
        {"groups": [["local_b", "local_a"]]},
    )

    assert reversed_result == forward


def test_reduce_does_not_create_relation_when_map_found_none():
    """共享 resolved entity 只能连接已有局部图，不能触发跨切片关系推理。"""
    premerged = premerge_document_kg_map_results(
        [
            _map_result(
                chunk_id="chunk_1",
                chunk_order=0,
                entities=[_map_entity("local_a", "控制台", chunk_id="chunk_1")],
            ),
            _map_result(
                chunk_id="chunk_2",
                chunk_order=1,
                entities=[_map_entity("local_b", "管理后台", chunk_id="chunk_2")],
            ),
        ]
    )

    reduced = reduce_document_kg(
        premerged,
        {"groups": [["local_a", "local_b"]]},
    )

    assert reduced["relations"] == []
