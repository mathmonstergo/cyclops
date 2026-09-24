import pytest

from cyclops.kg import (
    KnowledgeGraphExtractionError,
    build_kg_entity_knowledge_chunk_row,
    build_kg_relation_knowledge_chunk_row,
    kg_source_fingerprint,
    parse_kg_extraction_response,
)
from cyclops.kg_ai import KnowledgeGraphAiAssistant


def test_document_kg_source_fingerprint_includes_canonical_locator():
    """文档正文相同但文件名、章节或页码变化时必须得到不同抽取来源指纹。"""
    source = {
        "source_type": "document",
        "source_id": "imp_1",
        "source_chunk_id": "chunk_1",
        "source_title": "旧手册.pdf",
        "section_path": ["报告", "导出"],
        "page_start": 3,
        "page_end": 4,
    }

    original = kg_source_fingerprint(source_text="相同正文", source=source)
    relocated = kg_source_fingerprint(
        source_text="相同正文",
        source={
            **source,
            "source_title": "新手册.pdf",
            "section_path": ["报告", "故障处理"],
            "page_start": 8,
            "page_end": 8,
        },
    )

    assert original != relocated


def test_knowledge_graph_ai_assistant_parses_model_output_with_source():
    """KG AI 助手应调用 Chat 模型并把结果解析为待审核候选。"""

    class FakeChat:
        def __init__(self):
            self.calls = []

        def complete(self, system_prompt, user_prompt):
            """记录模型提示词并返回固定 KG 候选，避免依赖真实 Chat 服务。"""
            self.calls.append((system_prompt, user_prompt))
            return """
            {"entities":[{"name":"报告导出","entity_type":"feature_ui_action","aliases":[],"description":"","confidence":null,"evidence":[{"excerpt":"报告导出失败时，先检查账号权限。"}]}],
             "relations":[{"head":"报告导出","head_type":"feature_ui_action","relation_type":"requires","tail":"账号权限","tail_type":"role_permission_channel","description":"","confidence":null,"evidence":[{"excerpt":"先检查账号权限。"}]}]}
            """

    chat = FakeChat()
    assistant = KnowledgeGraphAiAssistant(chat)

    result = assistant.extract(
        source_text="报告导出失败时，先检查账号权限。",
        source={
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
            "source_title": "平台手册.pdf",
        },
    )

    assert result["entities"][0]["status"] == "needs_review"
    assert result["relations"][0]["relation_type"] == "requires"
    entity_evidence = result["entities"][0]["evidence"][0]
    relation_evidence = result["relations"][0]["evidence"][0]
    assert (entity_evidence["char_start"], entity_evidence["char_end"]) == (0, 16)
    assert (relation_evidence["char_start"], relation_evidence["char_end"]) == (8, 16)
    assert "product_platform_module" in chat.calls[0][0]
    assert "逐字复制来源文本中的连续子串" in chat.calls[0][0]
    assert "报告导出失败时，先检查账号权限。" in chat.calls[0][1]


def test_parse_kg_extraction_response_defaults_to_review_and_preserves_evidence():
    """KG 抽取解析必须让模型结果进入待审核，并保留 FAQ/文档来源证据。"""
    payload = {
        "entities": [
            {
                "name": "报告导出",
                "entity_type": "feature_ui_action",
                "aliases": ["导出报告"],
                "description": "后台导出团体报告的功能入口。",
                "confidence": 0.86,
                "evidence": [{"excerpt": "报告导出失败时，先检查账号权限。"}],
            }
        ],
        "relations": [
            {
                "head": "报告导出",
                "head_type": "feature_ui_action",
                "relation_type": "requires",
                "tail": "账号权限",
                "tail_type": "role_permission_channel",
                "description": "导出报告需要账号具备报告权限。",
                "confidence": 0.8,
                "evidence": [{"excerpt": "先检查账号权限。"}],
            }
        ],
    }
    source = {
        "source_type": "document",
        "source_id": "imp_1",
        "source_chunk_id": "chunk_1",
        "source_title": "平台使用手册.pdf",
        "section_path": ["报告", "导出"],
        "page_start": 3,
        "page_end": 4,
    }

    result = parse_kg_extraction_response(
        payload,
        source_text="报告导出失败时，先检查账号权限。",
        source=source,
    )

    assert len(result["entities"]) == 2
    entity = result["entities"][0]
    relation = result["relations"][0]
    assert entity["id"].startswith("kg_ent_")
    assert entity["status"] == "needs_review"
    assert entity["entity_type"] == "feature_ui_action"
    assert entity["aliases"] == ["导出报告"]
    assert entity["evidence"][0]["source_id"] == "imp_1"
    assert entity["evidence"][0]["source_chunk_id"] == "chunk_1"
    assert entity["evidence"][0]["section_path"] == ["报告", "导出"]
    assert relation["id"].startswith("kg_rel_")
    assert relation["head_entity_id"] == entity["id"]
    assert relation["tail_entity_id"].startswith("kg_ent_")
    assert relation["status"] == "needs_review"
    assert relation["evidence"][0]["excerpt"] == "先检查账号权限。"


def test_parse_kg_extraction_response_matches_exact_excerpt_offsets():
    """证据 offset 必须指向模型实际看见的同一份 Unicode 原文。"""
    source_text = "报告导出失败。先检查权限；再次检查权限。"
    result = parse_kg_extraction_response(
        {
            "entities": [
                {
                    "name": "报告导出",
                    "entity_type": "feature_ui_action",
                    "aliases": [],
                    "description": "导出入口",
                    "confidence": 0.9,
                    "evidence": [{"excerpt": "检查权限"}],
                }
            ],
            "relations": [],
        },
        source_text=source_text,
        source={
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
        },
    )

    evidence = result["entities"][0]["evidence"][0]
    assert (evidence["char_start"], evidence["char_end"]) == (8, 12)
    assert source_text[evidence["char_start"] : evidence["char_end"]] == evidence["excerpt"]


def test_parse_kg_extraction_response_rejects_excerpt_absent_from_source():
    """模型改写或臆造的 excerpt 不能进入 staging。"""
    with pytest.raises(KnowledgeGraphExtractionError, match="exact source substring"):
        parse_kg_extraction_response(
            {
                "entities": [
                    {
                        "name": "报告导出",
                        "entity_type": "feature_ui_action",
                        "aliases": [],
                        "description": "导出入口",
                        "confidence": 0.9,
                        "evidence": [{"excerpt": "不存在的证据"}],
                    }
                ],
                "relations": [],
            },
            source_text="只包含真实原文。",
            source={
                "source_type": "document",
                "source_id": "imp_1",
                "source_chunk_id": "chunk_1",
            },
        )


def test_parse_kg_extraction_response_uses_unicode_code_point_offsets():
    """astral 字符只占一个 Python code point，不能按 UTF-8 或 UTF-16 长度计数。"""
    source_text = "🙂前检查权限；检查权限"
    result = parse_kg_extraction_response(
        {
            "entities": [
                {
                    "name": "检查权限",
                    "entity_type": "feature_ui_action",
                    "aliases": [],
                    "description": "",
                    "confidence": None,
                    "evidence": [{"excerpt": "检查权限"}],
                }
            ],
            "relations": [],
        },
        source_text=source_text,
        source={
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
        },
    )

    evidence = result["entities"][0]["evidence"][0]
    assert (evidence["char_start"], evidence["char_end"]) == (2, 6)
    assert source_text[evidence["char_start"] : evidence["char_end"]] == "检查权限"


def test_parse_kg_extraction_response_rejects_model_reported_offsets():
    """模型只能返回 excerpt，不能用自报 offset 绕过后端精确定位。"""
    with pytest.raises(KnowledgeGraphExtractionError, match="unsupported evidence"):
        parse_kg_extraction_response(
            {
                "entities": [
                    {
                        "name": "检查权限",
                        "entity_type": "feature_ui_action",
                        "aliases": [],
                        "description": "",
                        "confidence": None,
                        "evidence": [
                            {
                                "excerpt": "检查权限",
                                "char_start": 999,
                                "char_end": 1003,
                            }
                        ],
                    }
                ],
                "relations": [],
            },
            source_text="检查权限",
            source={"source_type": "faq", "source_id": "faq_1"},
        )


def test_parse_kg_extraction_response_does_not_emit_source_count():
    """模型解析只产生事实与证据，实时来源数必须留给数据库按有效 locator 重算。"""
    parsed = parse_kg_extraction_response(
        {
            "entities": [
                {
                    "name": "报告导出",
                    "entity_type": "feature_ui_action",
                    "aliases": [],
                    "description": "导出入口",
                    "confidence": 0.9,
                    "evidence": [{"excerpt": "点击报告页的导出按钮。"}],
                }
            ],
            "relations": [],
        },
        source_text="点击报告页的导出按钮。",
        source={
            "source_type": "faq",
            "source_id": "faq_1",
            "source_chunk_id": None,
            "source_title": "如何导出报告",
            "section_path": [],
            "page_start": None,
            "page_end": None,
        },
    )

    assert "source_count" not in parsed["entities"][0]


def test_parse_kg_extraction_response_rejects_duplicate_canonical_entity_ids():
    """同一规范化名称和类型生成的实体 ID 不得重复，避免候选计数与持久化行数漂移。"""
    entity = {
        "name": "报告导出",
        "entity_type": "feature_ui_action",
        "aliases": [],
        "description": "导出入口",
        "confidence": 0.8,
        "evidence": [{"excerpt": "报告导出入口。"}],
    }

    with pytest.raises(KnowledgeGraphExtractionError, match="duplicate entity id"):
        parse_kg_extraction_response(
            {"entities": [entity, dict(entity)], "relations": []},
            source_text="报告导出入口。",
            source={
                "source_type": "faq",
                "source_id": "faq_1",
                "source_chunk_id": None,
            },
        )


def test_parse_kg_extraction_response_rejects_duplicate_stable_relation_ids():
    """相同端点和关系类型生成的稳定关系 ID 不得重复，避免完成计数虚高。"""
    relation = {
        "head": "报告导出",
        "head_type": "feature_ui_action",
        "relation_type": "requires",
        "tail": "账号权限",
        "tail_type": "role_permission_channel",
        "description": "导出需要权限",
        "confidence": 0.8,
        "evidence": [{"excerpt": "先检查账号权限。"}],
    }

    with pytest.raises(KnowledgeGraphExtractionError, match="duplicate relation id"):
        parse_kg_extraction_response(
            {"entities": [], "relations": [relation, dict(relation)]},
            source_text="先检查账号权限。",
            source={
                "source_type": "faq",
                "source_id": "faq_1",
                "source_chunk_id": None,
            },
        )


def test_parse_kg_extraction_response_rejects_duplicate_evidence_entries():
    """同一候选内完全相同的证据不得重复，否则完成计数会高于唯一持久化证据数。"""
    evidence = {"excerpt": "报告导出入口。"}

    with pytest.raises(KnowledgeGraphExtractionError, match="duplicate evidence"):
        parse_kg_extraction_response(
            {
                "entities": [
                    {
                        "name": "报告导出",
                        "entity_type": "feature_ui_action",
                        "aliases": [],
                        "description": "导出入口",
                        "confidence": 0.8,
                        "evidence": [evidence, dict(evidence)],
                    }
                ],
                "relations": [],
            },
            source_text="报告导出入口。",
            source={
                "source_type": "faq",
                "source_id": "faq_1",
                "source_chunk_id": None,
            },
        )


def test_parse_kg_extraction_response_rejects_freeform_types():
    """KG 抽取解析必须拒绝枚举外类型，避免模型自由造 schema。"""
    with pytest.raises(KnowledgeGraphExtractionError, match="entity_type"):
        parse_kg_extraction_response(
            {
                "entities": [
                    {
                        "name": "报告导出",
                        "entity_type": "random_type",
                        "aliases": [],
                        "description": "",
                        "confidence": None,
                        "evidence": [{"excerpt": "证据"}],
                    }
                ],
                "relations": [],
            },
            source_text="证据",
            source={"source_type": "faq", "source_id": "faq_1"},
        )


def test_parse_kg_extraction_response_requires_evidence():
    """实体和关系候选必须有证据，避免不可追溯事实进入审核池。"""
    with pytest.raises(KnowledgeGraphExtractionError, match="evidence"):
        parse_kg_extraction_response(
            {
                "entities": [
                    {
                        "name": "报告导出",
                        "entity_type": "feature_ui_action",
                        "aliases": [],
                        "description": "",
                        "confidence": None,
                    }
                ],
                "relations": [],
            },
            source_text="报告导出",
            source={"source_type": "faq", "source_id": "faq_1"},
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"entities": {}, "relations": []}, "entities must be an array"),
        ({"entities": ["bad"], "relations": []}, r"entities\[0\] must be an object"),
        ({"entities": [], "relations": None}, "relations must be an array"),
        ({"entities": [], "relations": [7]}, r"relations\[0\] must be an object"),
    ],
)
def test_parse_kg_extraction_response_rejects_noncanonical_collections(payload, message):
    """模型输出必须严格符合唯一数组 schema，不能把错类型静默当成空结果。"""
    with pytest.raises(KnowledgeGraphExtractionError, match=message):
        parse_kg_extraction_response(
            payload,
            source_text="测试原文",
            source={"source_type": "faq", "source_id": "faq_1"},
        )


def test_parse_kg_extraction_response_rejects_relation_field_aliases():
    """关系只接受 prompt 声明的 head/head_type/tail/tail_type，不维护别名 schema。"""
    with pytest.raises(KnowledgeGraphExtractionError, match=r"unsupported relations\[0\] fields"):
        parse_kg_extraction_response(
            {
                "entities": [],
                "relations": [
                    {
                        "head": "报告导出",
                        "head_type": "feature_ui_action",
                        "relation_type": "requires",
                        "tail": "账号权限",
                        "tail_type": "role_permission_channel",
                        "description": "导出需要账号权限",
                        "confidence": 0.8,
                        "evidence": [{"excerpt": "先检查账号权限。"}],
                        "head_name": "报告导出",
                    }
                ],
            },
            source_text="先检查账号权限。",
            source={"source_type": "faq", "source_id": "faq_1"},
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "entities": [
                    {
                        "name": "报告导出",
                        "entity_type": "feature_ui_action",
                        "evidence": [{"excerpt": "证据"}],
                    }
                ],
                "relations": [],
            },
            r"missing entities\[0\] fields",
        ),
        (
            {"entities": [], "relations": [], "summary": "extra"},
            "unsupported KG payload fields",
        ),
        (
            {
                "entities": [
                    {
                        "name": "报告导出",
                        "entity_type": "feature_ui_action",
                        "aliases": [],
                        "description": "导出入口",
                        "confidence": 0.8,
                        "evidence": [{"excerpt": "证据", "quote": "extra"}],
                    }
                ],
                "relations": [],
            },
            r"unsupported evidence\[0\] fields",
        ),
    ],
)
def test_parse_kg_extraction_response_requires_exact_canonical_schema(payload, message):
    """模型输出缺字段或多字段都必须失败，避免默认值和额外字段形成隐式 schema。"""
    with pytest.raises(KnowledgeGraphExtractionError, match=message):
        parse_kg_extraction_response(
            payload,
            source_text="证据",
            source={"source_type": "faq", "source_id": "faq_1"},
        )


def test_parse_kg_extraction_response_rejects_nonfinite_confidence():
    """NaN/Infinity 不是合法 JSON 置信度，不能绕过 0-1 范围判断进入候选表。"""
    payload = """
    {
      "entities": [{
        "name": "报告导出",
        "entity_type": "feature_ui_action",
        "aliases": [],
        "description": "导出入口",
        "confidence": NaN,
        "evidence": [{"excerpt": "证据"}]
      }],
      "relations": []
    }
    """

    with pytest.raises(KnowledgeGraphExtractionError, match="finite"):
        parse_kg_extraction_response(
            payload,
            source_text="证据",
            source={"source_type": "faq", "source_id": "faq_1"},
        )


def test_build_kg_entity_knowledge_chunk_row_uses_stable_projected_source():
    """已确认实体投影到 knowledge_chunks 时必须使用稳定 KG source_type/source_id。"""
    entity = {
        "id": "kg_ent_abc",
        "name": "报告导出",
        "entity_type": "feature_ui_action",
        "aliases": ["导出报告"],
        "description": "后台导出团体报告的功能入口。",
        "confidence": 0.86,
        "status": "usable",
    }
    evidence = [{"source_id": "imp_1", "source_chunk_id": "chunk_1", "excerpt": "检查账号权限"}]

    chunk = build_kg_entity_knowledge_chunk_row(entity, evidence)

    assert chunk["id"] == "kc_kg_entity_kg_ent_abc"
    assert chunk["source_type"] == "kg_entity"
    assert chunk["source_id"] == "kg_ent_abc"
    assert chunk["source_title"] == "报告导出"
    assert "类型：feature_ui_action" in chunk["content"]
    assert "别名：导出报告" in chunk["content"]
    assert chunk["metadata"]["evidence"] == evidence
    assert chunk["status"] == "usable"


def test_build_kg_relation_knowledge_chunk_row_keeps_node_ids_and_evidence():
    """已确认关系投影必须保留头尾实体 ID，供后续 2D/3D 子图复用。"""
    relation = {
        "id": "kg_rel_abc",
        "head_entity_id": "kg_ent_head",
        "tail_entity_id": "kg_ent_tail",
        "relation_type": "requires",
        "description": "导出报告需要账号具备报告权限。",
        "confidence": 0.8,
        "status": "usable",
    }
    head = {"name": "报告导出", "entity_type": "feature_ui_action"}
    tail = {"name": "账号权限", "entity_type": "role_permission_channel"}
    evidence = [{"source_id": "imp_1", "source_chunk_id": "chunk_1", "excerpt": "先检查账号权限"}]

    chunk = build_kg_relation_knowledge_chunk_row(relation, head, tail, evidence)

    assert chunk["id"] == "kc_kg_relation_kg_rel_abc"
    assert chunk["source_type"] == "kg_relation"
    assert chunk["source_id"] == "kg_rel_abc"
    assert chunk["source_title"] == "报告导出 requires 账号权限"
    assert "头实体：报告导出" in chunk["content"]
    assert "尾实体：账号权限" in chunk["content"]
    assert chunk["metadata"]["head_entity_id"] == "kg_ent_head"
    assert chunk["metadata"]["tail_entity_id"] == "kg_ent_tail"
    assert chunk["metadata"]["evidence"] == evidence


def test_strip_json_fence_handles_nested_json():
    """完整外层围栏必须保留嵌套 JSON 对象，实际 JSON 边界交给解析器判断。"""
    kg_json = '{"entities":[{"name":"x","evidence":[{"excerpt":"..."}]}],"relations":[]}'
    fenced = f"```json\n{kg_json}\n```"
    result = KnowledgeGraphAiAssistant._strip_json_fence(fenced)
    assert result == kg_json


def test_strip_json_fence_preserves_unfenced_json_with_paired_backticks():
    """未围栏 JSON 的字符串即使包含成对反引号，也必须原样交给 JSON 解析器。"""
    kg_json = (
        '{"entities":[],"relations":[],"description":"原文包含 ```code``` 标记"}'
    )

    result = KnowledgeGraphAiAssistant._strip_json_fence(kg_json)

    assert result == kg_json


def test_strip_json_fence_uses_last_closing_fence_after_internal_backticks():
    """围栏 JSON 字符串内的反引号不是结束标记，必须使用响应末尾的 closing fence。"""
    kg_json = (
        '{"entities":[{"description":"执行 ```sh``` 命令",'
        '"evidence":[{"excerpt":"输出 ```ok```"}]}],"relations":[]}'
    )
    fenced = f"```json\n{kg_json}\n```"

    result = KnowledgeGraphAiAssistant._strip_json_fence(fenced)

    assert result == kg_json


@pytest.mark.parametrize(
    "text",
    [
        '说明：\n```json\n{"entities":[],"relations":[]}\n```',
        '```json\n{"entities":[],"relations":[]}\n```\n以上为结果',
    ],
)
def test_strip_json_fence_preserves_fenced_json_with_surrounding_prose(text):
    """围栏前后存在 prose 时不是 canonical 响应，必须原样留给 json.loads 拒绝。"""
    assert KnowledgeGraphAiAssistant._strip_json_fence(text) == text


def test_strip_json_fence_preserves_braces_inside_strings():
    """JSON 字符串里的右花括号不能被误判为 fenced 对象结束。"""
    kg_json = (
        '{"entities":[{"name":"提示 } 不代表结束","evidence":[{"excerpt":"原文 }"}]}],'
        '"relations":[]}'
    )
    fenced = f"```json\n{kg_json}\n```"

    result = KnowledgeGraphAiAssistant._strip_json_fence(fenced)

    assert result == kg_json


def test_strip_json_fence_unfenced_text_returned_as_is():
    """非围栏文本应原样返回。"""
    text = '{"entities":[],"relations":[]}'
    assert KnowledgeGraphAiAssistant._strip_json_fence(text) == text


def test_strip_json_fence_empty_text():
    """空文本和纯空白应原样返回。"""
    assert KnowledgeGraphAiAssistant._strip_json_fence("") == ""
