import json
import threading
from types import SimpleNamespace

import pytest

from cyclops.admin_server import (
    AdminApp,
    AdminConflictError,
    AdminNotFoundError,
    AdminValidationError,
    assistant_document_payload,
    classify_error_response,
    document_knowledge_rows_for_embedding,
    format_sse_event,
    kg_fact_analysis_payload,
    parse_sse_event,
    normalize_faq_payload,
    retrieval_eval_item_payload,
    settings_payload_to_env,
    settings_to_tenant_settings,
    static_path,
)
from cyclops.config import Settings
from cyclops.db import (
    KgExpandedCandidate,
    KgFactHit,
    KgReviewConflictError,
    RetrievedKnowledgeChunk,
    compute_content_hash,
)
from cyclops.document_kg import (
    premerge_document_kg_map_results,
    reduce_document_kg,
)
from cyclops.import_questions import ImportQuestionError
from cyclops.document_parser import ParsedBlock
from cyclops.retrieval import FusedCandidate, HybridRetrievalResult


def test_normalize_faq_payload_sets_defaults_and_splits_lists():
    row = normalize_faq_payload(
        {
            "question": " 商品可以退货吗？ ",
            "answer": " 可以退。 ",
            "category": "售后服务",
            "tags": "退货, 退款",
            "question_variants": "退货条件是什么？\n多久内可以退？",
        }
    )

    assert row["id"].startswith("faq_")
    assert row["question"] == "商品可以退货吗？"
    assert row["answer"] == "可以退。"
    assert row["status"] == "usable"
    assert row["confidence"] == "high"
    assert row["tags"] == ["退货", "退款"]
    assert row["question_variants"] == ["退货条件是什么？", "多久内可以退？"]


def test_normalize_faq_payload_rejects_missing_question():
    with pytest.raises(AdminValidationError, match="question"):
        normalize_faq_payload({"answer": "可以退。"})


def test_normalize_faq_payload_rejects_missing_answer():
    with pytest.raises(AdminValidationError, match="answer"):
        normalize_faq_payload({"question": "商品可以退货吗？"})


def test_admin_app_batch_update_status_requires_ids():
    """批量状态更新必须明确选择 FAQ，避免空选择误操作。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    with pytest.raises(AdminValidationError, match="ids"):
        app.batch_update_status({"ids": [], "status": "disabled"})


def test_admin_app_batch_update_status_calls_database():
    """批量状态更新只把合法 id 和状态交给数据库层。"""
    calls = []

    class FakeDatabase:
        def update_faq_statuses(self, ids, status):
            calls.append((ids, status))
            return [{"id": ids[0], "status": status}]

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    assert app.batch_update_status({"ids": ["faq_1"], "status": "disabled"}) == {
        "count": 1,
        "items": [{"id": "faq_1", "status": "disabled"}],
    }
    assert calls == [(["faq_1"], "disabled")]


def test_admin_app_assistant_stream_returns_error_when_concurrency_limit_reached():
    """问答流超过并发上限时应直接返回 SSE error，不进入 embedding 或数据库检索。"""
    settings = SimpleNamespace(
        database_url="postgresql://unused",
        assistant_max_concurrent_streams=1,
        rag_top_k=3,
        rag_min_score=0.35,
    )

    class FakeEmbedding:
        def embed(self, _text):
            raise AssertionError("embedding must not be called when stream is saturated")

    class FakeChat:
        def complete(self, _system_prompt, _user_prompt):
            return '{"intent":"faq_exact","confidence":"medium","query_rewrite":"","preferred_sources":[]}'

    app = AdminApp(settings, embeddings=FakeEmbedding(), chat=FakeChat())
    app.assistant_stream_semaphore = threading.BoundedSemaphore(value=1)
    assert app.assistant_stream_semaphore.acquire(blocking=False)

    events = list(app.iter_assistant_chat_events({"question": "报告怎么生成？"}))

    assert events == [{"type": "error", "error": "当前问答服务繁忙，请稍后重试。"}]


def test_admin_app_create_retrieval_eval_case_stores_expected_hits():
    """检索评测用例接口应保存问题、意图和期望命中口径。"""
    calls = []

    class FakeDatabase:
        def create_retrieval_eval_case(self, row):
            calls.append(row)
            return row

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.create_retrieval_eval_case(
        {
            "question": "报告没有生成怎么办？",
            "intent": "troubleshooting",
            "expected_source_ids": ["faq_1"],
            "expected_chunk_ids": ["kc_faq_1"],
            "tags": "报告,失败",
            "note": "真实客服高频问题",
        }
    )

    assert result["id"].startswith("eval_")
    assert calls[0]["question"] == "报告没有生成怎么办？"
    assert calls[0]["intent"] == "troubleshooting"
    assert calls[0]["expected_source_ids"] == ["faq_1"]
    assert calls[0]["expected_chunk_ids"] == ["kc_faq_1"]
    assert calls[0]["tags"] == ["报告", "失败"]
    assert calls[0]["status"] == "active"


def test_admin_app_create_retrieval_eval_case_requires_question():
    """检索评测用例必须有问题，避免沉淀不可执行样本。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    with pytest.raises(AdminValidationError, match="question"):
        app.create_retrieval_eval_case({"question": ""})


def test_admin_app_list_retrieval_eval_cases_passes_filters():
    """检索评测列表接口应把分页和状态筛选交给数据库。"""
    calls = []

    class FakeDatabase:
        def list_retrieval_eval_cases(self, *, status, limit, offset):
            calls.append((status, limit, offset))
            return {"items": [], "total": 0}

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    assert app.list_retrieval_eval_cases({"status": ["active"], "limit": ["20"], "offset": ["5"]}) == {
        "items": [],
        "total": 0,
    }
    assert calls == [("active", 20, 5)]


def _eval_retrieved_chunk(
    chunk_id: str,
    source_id: str,
    *,
    score: float,
) -> RetrievedKnowledgeChunk:
    """构造评测用 canonical FAQ 候选，禁止测试继续使用旧匿名对象。"""
    return RetrievedKnowledgeChunk(
        id=chunk_id,
        source_type="faq",
        source_id=source_id,
        source_chunk_id=None,
        parent_chunk_id=None,
        chunk_level="chunk",
        source_title="报告导出失败怎么办？",
        section_path=[],
        page_start=None,
        page_end=None,
        block_type="faq",
        source_offsets={},
        content="问题：报告导出失败怎么办？\n答案：请检查导出权限。",
        metadata={"category": "报告"},
        tags=[],
        confidence="high",
        status="usable",
        score=score,
    )


def test_admin_app_run_retrieval_eval_case_records_hybrid_result():
    """运行单条检索评测时应保存意图、候选、指标和命中结果。"""
    calls = []

    class FakeEmbedding:
        def embed(self, text):
            calls.append(("embed", text))
            return [0.1, 0.2, 0.3]

    class FakeChat:
        def complete(self, system_prompt, user_prompt):
            return '{"intent":"faq_exact","confidence":"medium","query_rewrite":"报告导出失败","preferred_sources":["faq","document"]}'

    class FakeDatabase:
        def get_retrieval_eval_case(self, case_id):
            assert case_id == "eval_1"
            return {
                "id": "eval_1",
                "question": "报告导出失败怎么办？",
                "expected_chunk_ids": ["kc_faq_1"],
                "expected_source_ids": [],
            }

        def list_retrieval_aliases(self, status="active"):
            return [{"canonical": "报告", "aliases": ["团体报告"]}]

        def search_knowledge(self, query_embedding, *, top_k, min_score):
            """返回评测用向量候选，并校验统一检索参数。"""
            assert query_embedding == [0.1, 0.2, 0.3]
            assert top_k == 6
            assert min_score == 0.4
            return [_eval_retrieved_chunk("kc_noise", "faq_noise", score=0.91)]

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            """返回评测用关键词候选，并校验查询词扩展参数。"""
            assert query_text == "报告导出失败怎么办？"
            assert top_k == 6
            assert "报告" in query_terms
            assert "导出" in query_terms
            return [_eval_retrieved_chunk("kc_faq_1", "faq_1", score=0.8)]

        def record_retrieval_eval_run(self, row):
            calls.append(("run", row))
            return {**row, "id": "eval_run_1"}

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=FakeChat(),
    )

    result = app.run_retrieval_eval_case("eval_1", {})

    assert result["id"] == "eval_run_1"
    assert result["metrics"]["recall_at_k"] == 1.0
    assert "kc_faq_1" in [item["id"] for item in result["retrieved_items"]]
    assert result["strategy"] == "retrieval_hybrid_v1"
    assert result["analysis"]["contract_version"] == 2
    assert result["analysis"]["intent"] == "troubleshooting"
    assert calls[0] == ("embed", "报告导出失败怎么办？")
    assert calls[-1][0] == "run"


def test_admin_app_run_retrieval_eval_case_uses_kg_only_when_enabled():
    """KG debug 必须展开为原始证据参与 metrics，孤儿 fact 只保留在诊断。"""
    calls = []

    class FakeEmbedding:
        def embed(self, text):
            calls.append(("embed", text))
            return [0.1, 0.2, 0.3]

    class FakeChat:
        def complete(self, system_prompt, user_prompt):
            return '{"intent":"faq_exact","confidence":"medium","query_rewrite":"报告导出","preferred_sources":["faq","document"]}'

    class FakeDatabase:
        def get_retrieval_eval_case(self, case_id):
            """返回 KG 调试评测使用的固定样本。"""
            return {
                "id": case_id,
                "question": "报告导出失败怎么办？",
                "expected_chunk_ids": ["kc_faq_1"],
                "expected_source_ids": [],
            }

        def list_retrieval_aliases(self, status="active"):
            return []

        def search_knowledge(self, query_embedding, *, top_k, min_score):
            return []

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            return []

        def search_kg_knowledge_text(self, query_text, *, top_k, query_terms):
            """记录 KG fact 查询并返回固定命中。"""
            calls.append(("kg", query_text, top_k, tuple(query_terms)))
            return [
                KgFactHit(
                    fact_chunk_id="kc_kg_relation_1",
                    fact_id="kg_rel_1",
                    fact_type="kg_relation",
                    fact_rank=1,
                    fact_score=0.77,
                ),
                KgFactHit(
                    fact_chunk_id="kc_kg_entity_orphan",
                    fact_id="kg_ent_orphan",
                    fact_type="kg_entity",
                    fact_rank=2,
                    fact_score=0.63,
                ),
            ]

        def expand_kg_fact_hits(self, fact_hits):
            """把首条 KG fact 展开为原始 FAQ 候选。"""
            calls.append(("expand", tuple(hit.fact_chunk_id for hit in fact_hits)))
            document = _eval_retrieved_chunk(
                "kc_faq_1",
                "faq_1",
                score=0.77,
            )
            return [KgExpandedCandidate(document=document, kg_matches=(fact_hits[0],))]

        def record_retrieval_eval_run(self, row):
            calls.append(("run", row))
            return {**row, "id": "eval_run_kg"}

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=FakeChat(),
    )

    result = app.run_retrieval_eval_case("eval_kg", {"use_kg": True})

    assert any(call[0] == "kg" for call in calls)
    assert any(call[0] == "expand" for call in calls)
    assert result["strategy"] == "retrieval_hybrid_v1_kg_debug"
    assert result["metrics"]["recall_at_k"] == 1.0
    assert result["analysis"]["contract_version"] == 2
    assert result["analysis"]["use_kg"] is True
    assert result["analysis"]["kg_fact_count"] == 2
    assert result["analysis"]["kg_expanded_candidate_count"] == 1
    assert result["analysis"]["kg_facts"][0]["expanded_candidate_ids"] == ["kc_faq_1"]
    assert result["analysis"]["kg_facts"][1]["expanded_candidate_ids"] == []
    assert result["retrieved_items"][0]["id"] == "kc_faq_1"
    assert result["retrieved_items"][0]["source_type"] == "faq"
    assert "kg" in result["retrieved_items"][0]["channels"]
    assert result["retrieved_items"][0]["kg_score"] == 0.77
    assert result["retrieved_items"][0]["kg_matches"][0]["fact_id"] == "kg_rel_1"
    assert "kc_kg_relation_1" not in [item["id"] for item in result["retrieved_items"]]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"use_kg": False},
        {"use_kg": "true"},
        {"use_kg": "false"},
        {"use_kg": 1},
        {"use_kg": 1.0},
        {"unexpected": True},
        {"use_kg": False, "unexpected": True},
    ],
)
def test_admin_app_run_retrieval_eval_case_rejects_noncanonical_payload(payload):
    """评测运行只接受精确空对象或 use_kg=true，None、false 和额外字段都拒绝。"""

    class FakeDatabase:
        def get_retrieval_eval_case(self, case_id):
            """非法 payload 必须在读取评测用例前被拒绝。"""
            raise AssertionError(f"invalid eval payload reached database: {case_id}")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminValidationError, match="payload|use_kg"):
        app.run_retrieval_eval_case("eval_1", payload)


def test_retrieval_eval_delegates_to_hybrid_service_with_explicit_kg_mode():
    """评测必须委托唯一混合服务，并把 KG debug 模式显式传入。"""
    calls = []
    retrieval_result = _admin_hybrid_result()

    class FakeRetrieval:
        """记录评测调用形状，候选来自统一服务结果。"""

        def retrieve(self, query, *, include_parent_context, use_kg):
            """记录统一检索调用并返回固定结果。"""
            calls.append((query, include_parent_context, use_kg))
            return retrieval_result

    class FakeDatabase:
        """提供评测样本并原样返回待记录运行。"""

        def get_retrieval_eval_case(self, case_id):
            """返回统一检索委托测试使用的评测样本。"""
            return {
                "id": case_id,
                "question": "报告导出失败怎么办？",
                "expected_chunk_ids": [retrieval_result.candidates[0].document.id],
                "expected_source_ids": [],
            }

        def record_retrieval_eval_run(self, row):
            """模拟保存评测运行并补充固定 ID。"""
            return {**row, "id": "eval_run_1"}

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        chat=SimpleNamespace(),
        retrieval=FakeRetrieval(),
    )

    result = app.run_retrieval_eval_case("eval_1", {"use_kg": True})

    assert calls == [("报告导出失败怎么办？", False, True)]
    assert result["metrics"]["recall_at_k"] == 1.0
    assert result["analysis"]["use_kg"] is True


def test_admin_app_kg_subgraph_passes_filters_to_database():
    """局部子图只透传中心、跳数、类型和数量，不暴露 status 参数。"""
    calls = []

    class FakeDatabase:
        def get_kg_subgraph(
            self,
            *,
            center_entity_id,
            hops,
            entity_types,
            relation_types,
            limit,
        ):
            """记录子图过滤参数并返回孤立中心。"""
            calls.append(
                {
                    "center_entity_id": center_entity_id,
                    "hops": hops,
                    "entity_types": entity_types,
                    "relation_types": relation_types,
                    "limit": limit,
                }
            )
            return {
                "state": "isolated",
                "center": {"id": center_entity_id},
                "nodes": [{"id": center_entity_id}],
                "edges": [],
            }

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.kg_subgraph(
        {
            "center_entity_id": ["kg_ent_abc"],
            "hops": ["1"],
            "entity_type": ["feature_ui_action,error_symptom"],
            "relation_type": ["requires"],
            "limit": ["80"],
        }
    )

    assert result["nodes"][0]["id"] == "kg_ent_abc"
    assert calls == [
        {
            "center_entity_id": "kg_ent_abc",
            "hops": 1,
            "entity_types": ["feature_ui_action", "error_symptom"],
            "relation_types": ["requires"],
            "limit": 80,
        }
    ]


@pytest.mark.parametrize("field", ["status", "unknown"])
def test_admin_app_kg_subgraph_rejects_removed_or_unknown_query_fields(field):
    """子图查询只接受唯一新契约，旧 status 和未知字段都必须明确 400。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    with pytest.raises(AdminValidationError, match="query fields"):
        app.kg_subgraph({"center_entity_id": ["kg_ent_1"], field: ["usable"]})


def test_admin_app_kg_subgraph_maps_missing_center_to_not_found():
    """DB 无 usable center 时必须映射 404，不能与 isolated 混为一谈。"""

    class FakeDatabase:
        def get_kg_subgraph(self, **_kwargs):
            """模拟数据库找不到 usable 中心实体。"""
            return None

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    with pytest.raises(AdminNotFoundError, match="kg_ent_missing"):
        app.kg_subgraph({"center_entity_id": ["kg_ent_missing"]})


def test_admin_app_kg_subgraph_does_not_mask_database_errors():
    """子图数据库异常必须留给统一 500 处理，不能伪装成不存在。"""

    class FakeDatabase:
        def get_kg_subgraph(self, **_kwargs):
            """模拟数据库子图查询故障。"""
            raise RuntimeError("database unavailable")

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    with pytest.raises(RuntimeError, match="database unavailable"):
        app.kg_subgraph({"center_entity_id": ["kg_ent_1"]})


def test_admin_app_list_kg_entities_passes_review_filters():
    """KG 实体审核列表接口应把状态、类型和分页过滤交给数据库。"""
    calls = []

    class FakeDatabase:
        def list_kg_entities(self, *, status, entity_type, limit, offset):
            """记录实体审核筛选参数并返回固定分页结果。"""
            calls.append((status, entity_type, limit, offset))
            return {"items": [{"id": "kg_ent_1", "review_revision": 1}], "total": 1}

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.list_kg_entities(
        {"status": ["needs_review"], "entity_type": ["feature_ui_action"], "limit": ["20"], "offset": ["5"]}
    )

    assert result["items"][0]["id"] == "kg_ent_1"
    assert calls == [("needs_review", "feature_ui_action", 20, 5)]


def test_admin_app_list_kg_relations_passes_review_filters():
    """KG 关系审核列表接口应把状态、类型和分页过滤交给数据库。"""
    calls = []

    class FakeDatabase:
        def list_kg_relations(self, *, status, relation_type, limit, offset):
            """记录关系审核筛选参数并返回固定分页结果。"""
            calls.append((status, relation_type, limit, offset))
            return {"items": [{"id": "kg_rel_1", "review_revision": 1}], "total": 1}

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.list_kg_relations(
        {"status": ["needs_review"], "relation_type": ["requires"], "limit": ["30"], "offset": ["0"]}
    )

    assert result["items"][0]["id"] == "kg_rel_1"
    assert calls == [("needs_review", "requires", 30, 0)]


def test_admin_app_confirm_kg_entity_delegates_to_database():
    """确认 KG 实体接口应调用数据库确认投影，并返回 item 包装。"""

    class FakeDatabase:
        def confirm_kg_entity(self, entity_id, *, expected_revision):
            """校验实体确认参数并返回当前审核快照。"""
            assert entity_id == "kg_ent_1"
            assert expected_revision == 1
            return {"id": entity_id, "status": "usable", "review_revision": 1}

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    assert app.confirm_kg_entity("kg_ent_1", {"expected_revision": 1}) == {
        "item": {"id": "kg_ent_1", "status": "usable", "review_revision": 1}
    }


def test_admin_app_kg_confirm_requires_exact_expected_revision_payload():
    """确认请求必须且只能提交正整数 expected_revision，并原样传给数据库。"""
    calls = []

    class FakeDatabase:
        def confirm_kg_entity(self, entity_id, *, expected_revision):
            """记录实体和 revision，验证管理层不从 ID 或其他字段推断版本。"""
            calls.append((entity_id, expected_revision))
            return {
                "id": entity_id,
                "status": "usable",
                "review_revision": expected_revision,
            }

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.confirm_kg_entity("kg_ent_1", {"expected_revision": 3})

    assert result["item"]["review_revision"] == 3
    assert calls == [("kg_ent_1", 3)]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"expected_revision": 1, "extra": True},
        {"expected_revision": True},
        {"expected_revision": 0},
        {"expected_revision": "1"},
    ],
)
def test_admin_app_kg_confirm_rejects_noncanonical_revision_payload(payload):
    """缺失、额外或非正整数 revision 都必须在数据库调用前被拒绝。"""

    class FakeDatabase:
        def confirm_kg_entity(self, entity_id, *, expected_revision):
            """非法 payload 不得到达数据库确认入口。"""
            raise AssertionError(
                f"invalid KG confirm payload reached DB: {entity_id} {expected_revision}"
            )

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    with pytest.raises(AdminValidationError, match="expected_revision"):
        app.confirm_kg_entity("kg_ent_1", payload)


def test_admin_app_maps_stale_kg_review_revision_to_http_conflict():
    """审核快照已替换时返回 409 冲突，提示用户刷新而不是伪装成字段校验失败。"""

    class FakeDatabase:
        def confirm_kg_entity(self, entity_id, *, expected_revision):
            """模拟锁内发现页面 revision 已落后于数据库候选。"""
            raise KgReviewConflictError(
                f"KG entity review revision changed: {entity_id} {expected_revision}"
            )

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    with pytest.raises(AdminConflictError) as exc_info:
        app.confirm_kg_entity("kg_ent_1", {"expected_revision": 1})

    status, body = classify_error_response(exc_info.value)
    assert status.value == 409
    assert "review revision changed" in body["error"]


def test_admin_app_confirm_kg_entity_maps_database_validation_error():
    """实体缺少有效证据时应返回可操作的管理端校验错误，而不是内部错误。"""

    class FakeDatabase:
        def confirm_kg_entity(self, entity_id, *, expected_revision):
            """模拟数据库确认门禁拒绝缺少有效证据的实体。"""
            assert expected_revision == 1
            raise ValueError(f"KG entity requires valid evidence: {entity_id}")

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    with pytest.raises(AdminValidationError, match="requires valid evidence"):
        app.confirm_kg_entity("kg_ent_1", {"expected_revision": 1})


def test_admin_app_confirm_kg_relation_maps_database_validation_error():
    """关系端点未确认时应返回可操作的管理端校验错误，而不是内部错误。"""

    class FakeDatabase:
        def confirm_kg_relation(self, relation_id, *, expected_revision):
            """模拟数据库拒绝端点实体未 usable 的关系。"""
            assert expected_revision == 1
            raise ValueError(
                f"KG relation endpoint entities must both be usable: {relation_id}"
            )

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    with pytest.raises(AdminValidationError, match="endpoint entities must both be usable"):
        app.confirm_kg_relation("kg_rel_1", {"expected_revision": 1})


def test_admin_app_set_kg_relation_status_validates_status():
    """KG 状态接口只允许退回待审或停用，usable 必须走显式 confirm。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    with pytest.raises(AdminValidationError, match="status"):
        app.set_kg_relation_status("kg_rel_1", {"status": "archived"})

    with pytest.raises(AdminValidationError, match="confirm"):
        app.set_kg_relation_status("kg_rel_1", {"status": "usable"})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "disabled", "reason": "manual"},
    ],
)
def test_admin_app_kg_review_status_requires_exact_payload_fields(payload):
    """KG 状态请求必须且只能包含 status，不能静默忽略缺失或额外字段。"""

    class FakeDatabase:
        def set_kg_entity_status(self, entity_id, status):
            """非法 payload 不得进入数据库状态更新。"""
            raise AssertionError(f"invalid review payload reached DB: {entity_id} {status}")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminValidationError, match="exactly status"):
        app.set_kg_entity_status("kg_ent_1", payload)


@pytest.mark.parametrize("status", [7, True, None])
def test_admin_app_kg_review_status_requires_string_value(status):
    """KG status 必须是真实字符串，数字、布尔和 null 都不能做字符串 coercion。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    with pytest.raises(AdminValidationError, match="status must be a string"):
        app.set_kg_relation_status("kg_rel_1", {"status": status})


def test_admin_app_set_kg_entity_status_delegates_to_database():
    """禁用 KG 实体接口应调用数据库同步实体和投影状态。"""
    calls = []

    class FakeDatabase:
        def set_kg_entity_status(self, entity_id, status):
            calls.append((entity_id, status))
            return {"id": entity_id, "status": status}

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.set_kg_entity_status("kg_ent_1", {"status": "disabled"})

    assert result == {"item": {"id": "kg_ent_1", "status": "disabled"}}
    assert calls == [("kg_ent_1", "disabled")]


class _KgStepChat:
    """记录单步 KG orchestration 的 Chat 调用并返回预置响应。"""

    model = "mimo-v2.5-pro"

    def __init__(self, responses=None, *, error=None):
        """保存有限响应；关键约束是额外 Chat 调用会立即暴露。"""
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def complete(self, system_prompt, user_prompt):
        """记录一次 Chat；预置异常用于验证同 lease 失败收口。"""
        self.calls.append((system_prompt, user_prompt))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("KG job step must make at most one expected Chat call")
        return self.responses.pop(0)


def _kg_step_job(*, source_type="document", phase="mapping", **overrides):
    """构造已由 worker claim 的内部 job，包含 phase runner 必需 lease。"""
    row = {
        "id": "kg_job_1",
        "source_type": source_type,
        "source_id": "faq_1" if source_type == "faq" else "imp_1",
        "phase": phase,
        "lease_token": "lease_1",
        "resolution_result": None,
    }
    row.update(overrides)
    return row


def _kg_step_extraction_response(
    *,
    name="控制台",
    entity_type="product_platform_module",
    excerpt="控制台",
):
    """构造单实体模型响应，excerpt 必须能在测试来源原文中精确命中。"""
    return json.dumps(
        {
            "entities": [
                {
                    "name": name,
                    "entity_type": entity_type,
                    "aliases": [],
                    "description": "",
                    "confidence": None,
                    "evidence": [{"excerpt": excerpt}],
                }
            ],
            "relations": [],
        },
        ensure_ascii=False,
    )


def _kg_step_document_evidence(
    *,
    chunk_id="chunk_1",
    excerpt="控制台",
    char_start=0,
):
    """构造已精确定位的文档 evidence，供 persisted Map/Reduce 测试使用。"""
    return {
        "source_type": "document",
        "source_id": "imp_1",
        "source_chunk_id": chunk_id,
        "source_title": "平台使用手册.pdf",
        "section_path": ["后台"],
        "page_start": 2,
        "page_end": 2,
        "excerpt": excerpt,
        "char_start": char_start,
        "char_end": char_start + len(excerpt),
    }


def _kg_step_map_entity(
    local_id,
    name,
    *,
    chunk_id="chunk_1",
    entity_type="product_platform_module",
    char_start=0,
):
    """构造带 local ID 的单片实体，避免 phase 测试依赖模型生成 ID。"""
    return {
        "local_entity_id": local_id,
        "name": name,
        "entity_type": entity_type,
        "aliases": [],
        "description": f"{name}说明",
        "confidence": 0.8,
        "evidence": [
            _kg_step_document_evidence(
                chunk_id=chunk_id,
                excerpt=name,
                char_start=char_start,
            )
        ],
    }


def _kg_step_map_relation(
    local_id,
    head_id,
    tail_id,
    *,
    chunk_id="chunk_1",
):
    """构造只引用同一 Map local entity 的关系。"""
    excerpt = "控制台需要管理员权限"
    return {
        "local_relation_id": local_id,
        "head_local_entity_id": head_id,
        "relation_type": "requires",
        "tail_local_entity_id": tail_id,
        "description": "进入控制台前需要管理员权限",
        "confidence": 0.9,
        "evidence": [
            _kg_step_document_evidence(
                chunk_id=chunk_id,
                excerpt=excerpt,
            )
        ],
    }


def _kg_step_map_result(
    *,
    chunk_id="chunk_1",
    chunk_order=0,
    entities=None,
    relations=None,
):
    """构造数据库返回的完整 localized Map staging object。"""
    return {
        "chunk_id": chunk_id,
        "chunk_order": chunk_order,
        "entities": list(entities or []),
        "relations": list(relations or []),
    }


def test_admin_app_queue_resource_kg_jobs_require_strict_empty_object():
    """FAQ/文档资源级排队只接受 {}，并调用各自显式数据库入口。"""
    calls = []

    class FakeDatabase:
        def create_faq_kg_extraction_job(self, faq_id, *, model):
            """记录 FAQ owner 与模型，不读取或推断通用 source_type。"""
            calls.append(("faq", faq_id, model))
            return {"id": "kg_job_faq", "source_type": "faq", "phase": "queued"}

        def create_document_kg_extraction_job(self, file_id, *, model):
            """记录整篇文档 owner，公开入口不接受 chunk ID。"""
            calls.append(("document", file_id, model))
            return {
                "id": "kg_job_document",
                "source_type": "document",
                "phase": "queued",
            }

    chat = _KgStepChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    faq_job = app.queue_faq_kg_extraction_job("faq_1", {})
    document_job = app.queue_document_kg_extraction_job("imp_1", {})

    assert faq_job["source_type"] == "faq"
    assert document_job["source_type"] == "document"
    assert calls == [
        ("faq", "faq_1", "mimo-v2.5-pro"),
        ("document", "imp_1", "mimo-v2.5-pro"),
    ]
    assert chat.calls == []


@pytest.mark.parametrize(
    ("method_name", "owner_id"),
    [
        ("queue_faq_kg_extraction_job", "faq_1"),
        ("queue_document_kg_extraction_job", "imp_1"),
    ],
)
def test_admin_app_queue_resource_kg_jobs_reject_nonempty_payload(
    method_name,
    owner_id,
):
    """资源级 KG POST 的任何请求字段都必须在数据库调用前被拒绝。"""

    class FakeDatabase:
        def create_faq_kg_extraction_job(self, faq_id, *, model):
            """非空 FAQ payload 不得进入数据库。"""
            raise AssertionError(f"invalid FAQ payload reached DB: {faq_id} {model}")

        def create_document_kg_extraction_job(self, file_id, *, model):
            """非空文档 payload 不得进入数据库。"""
            raise AssertionError(f"invalid document payload reached DB: {file_id} {model}")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model="mimo-v2.5-pro"),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminValidationError, match="empty|fields|payload"):
        getattr(app, method_name)(owner_id, {"source_type": "faq"})


def test_admin_app_latest_resource_kg_jobs_use_explicit_owner():
    """FAQ latest 固定查询 FAQ owner，文档 latest 固定查询 file owner。"""
    calls = []

    class FakeDatabase:
        def get_latest_kg_extraction_job(self, *, source_type, source_id):
            """记录 latest 的明确 owner 二元组并返回公开 DTO。"""
            calls.append((source_type, source_id))
            return {
                "id": f"kg_job_{source_type}",
                "source_type": source_type,
                "source_id": source_id,
                "phase": "mapping",
            }

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
    )

    faq_job = app.get_latest_faq_kg_extraction_job("faq_1")
    document_job = app.get_latest_document_kg_extraction_job("imp_1")

    assert faq_job["source_id"] == "faq_1"
    assert document_job["source_id"] == "imp_1"
    assert calls == [("faq", "faq_1"), ("document", "imp_1")]


@pytest.mark.parametrize(
    "method_name",
    ["queue_kg_extraction_job", "run_kg_extraction_job", "_kg_extraction_source"],
)
def test_admin_app_has_no_generic_kg_job_dispatch(method_name):
    """Unit 0c 只保留资源级入口和单步 runner，不保留旧 generic wrapper。"""
    assert not hasattr(AdminApp, method_name)


def test_admin_app_kg_job_step_maps_faq_and_completes_atomically():
    """faq/mapping 读取精确 FAQ、调用一次 Map Chat 并以同 lease 原子完成。"""
    calls = []
    saved = {}

    class FakeDatabase:
        def get_faq(self, faq_id):
            """按 claimed source_id 返回当前 usable FAQ。"""
            calls.append(("get_faq", faq_id))
            return {
                "id": faq_id,
                "question": "如何打开控制台？",
                "answer": "打开控制台。",
                "status": "usable",
            }

        def complete_faq_kg_extraction_job(
            self,
            job_id,
            *,
            lease_token,
            extraction,
        ):
            """记录 FAQ 独立 completion，不接受旧 source_guard wrapper。"""
            calls.append(("complete_faq", job_id, lease_token))
            saved["extraction"] = extraction
            return {"id": job_id, "source_type": "faq", "phase": "completed"}

        def fail_kg_extraction_job(self, job_id, *, lease_token, error):
            """合法 FAQ Map 不应进入失败收口。"""
            raise AssertionError(f"FAQ Map unexpectedly failed: {job_id} {lease_token} {error}")

    chat = _KgStepChat([_kg_step_extraction_response()])
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.process_kg_extraction_job_step(
        _kg_step_job(source_type="faq", phase="mapping")
    )

    assert result["phase"] == "completed"
    assert calls == [
        ("get_faq", "faq_1"),
        ("complete_faq", "kg_job_1", "lease_1"),
    ]
    assert len(chat.calls) == 1
    assert "如何打开控制台？" in chat.calls[0][1]
    evidence = saved["extraction"]["entities"][0]["evidence"][0]
    assert evidence["source_type"] == "faq"
    assert evidence["source_id"] == "faq_1"
    assert evidence["char_end"] > evidence["char_start"]


def test_admin_app_document_kg_map_localizes_and_completes_one_item():
    """document/mapping 只加载一个 item、调用一次 Map Chat 并写 localized staging。"""
    calls = []
    saved = {}
    item = {
        "id": "kg_item_1",
        "job_id": "kg_job_1",
        "chunk_id": "chunk_1",
        "chunk_order": 0,
        "source_text": "控制台入口需要管理员权限。",
        "source": {
            "source_type": "document",
            "source_id": "imp_1",
            "source_chunk_id": "chunk_1",
            "source_title": "平台使用手册.pdf",
            "section_path": ["后台"],
            "page_start": 2,
            "page_end": 2,
        },
    }

    class FakeDatabase:
        def load_document_kg_map_item(self, job_id, *, lease_token):
            """返回当前 lease 唯一尚未 mapped 的 manifest item。"""
            calls.append(("load_map_item", job_id, lease_token))
            return item

        def complete_document_kg_map_item(
            self,
            job_id,
            item_id,
            *,
            lease_token,
            map_result,
        ):
            """记录 localized Map object 和当前 item/lease。"""
            calls.append(("complete_map_item", job_id, item_id, lease_token))
            saved["map_result"] = map_result
            return {"id": job_id, "source_type": "document", "phase": "mapping"}

        def fail_kg_extraction_job(self, job_id, *, lease_token, error):
            """合法文档 Map 不应进入失败收口。"""
            raise AssertionError(
                f"document Map unexpectedly failed: {job_id} {lease_token} {error}"
            )

    chat = _KgStepChat([_kg_step_extraction_response()])
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.process_kg_extraction_job_step(_kg_step_job())

    assert result["phase"] == "mapping"
    assert calls == [
        ("load_map_item", "kg_job_1", "lease_1"),
        ("complete_map_item", "kg_job_1", "kg_item_1", "lease_1"),
    ]
    assert len(chat.calls) == 1
    map_result = saved["map_result"]
    assert map_result["chunk_id"] == "chunk_1"
    assert map_result["chunk_order"] == 0
    assert map_result["entities"][0]["local_entity_id"].startswith("kg_doc_local_")
    assert "id" not in map_result["entities"][0]


@pytest.mark.parametrize("entity_count", [0, 1])
def test_admin_app_document_kg_resolution_skips_chat_for_zero_or_one_entity(
    entity_count,
):
    """document/resolving 的 0/1 个 premerged entity 直接保存空 groups。"""
    entities = []
    if entity_count == 1:
        entities.append(_kg_step_map_entity("local_console", "控制台"))
    map_results = [_kg_step_map_result(entities=entities)]
    calls = []

    class FakeDatabase:
        def load_document_kg_map_results(self, job_id, *, lease_token):
            """按 manifest 顺序返回全部 persisted Map objects。"""
            calls.append(("load_maps", job_id, lease_token))
            return map_results

        def save_document_kg_resolution(
            self,
            job_id,
            *,
            lease_token,
            resolution_result,
        ):
            """记录无模型分支生成的确定性空 resolution。"""
            calls.append(("save_resolution", job_id, lease_token, resolution_result))
            return {"id": job_id, "source_type": "document", "phase": "reducing"}

        def fail_kg_extraction_job(self, job_id, *, lease_token, error):
            """合法短路 resolution 不应进入失败收口。"""
            raise AssertionError(
                f"resolution unexpectedly failed: {job_id} {lease_token} {error}"
            )

    chat = _KgStepChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.process_kg_extraction_job_step(
        _kg_step_job(phase="resolving")
    )

    assert result["phase"] == "reducing"
    assert calls == [
        ("load_maps", "kg_job_1", "lease_1"),
        ("save_resolution", "kg_job_1", "lease_1", {"groups": []}),
    ]
    assert chat.calls == []


def test_admin_app_document_kg_resolution_calls_chat_once_for_multiple_entities():
    """document/resolving 的 2+ 候选只调用一次受约束 resolution Chat。"""
    entities = [
        _kg_step_map_entity("local_console", "控制台"),
        _kg_step_map_entity("local_admin", "管理后台", char_start=10),
    ]
    map_results = [_kg_step_map_result(entities=entities)]
    resolution = {"groups": [["local_console", "local_admin"]]}
    saved = {}

    class FakeDatabase:
        def load_document_kg_map_results(self, job_id, *, lease_token):
            """返回可进入模型消歧的两个 premerged entity。"""
            assert (job_id, lease_token) == ("kg_job_1", "lease_1")
            return map_results

        def save_document_kg_resolution(
            self,
            job_id,
            *,
            lease_token,
            resolution_result,
        ):
            """保存 parser 已校验的 resolution 并进入 reducing。"""
            saved["call"] = (job_id, lease_token, resolution_result)
            return {"id": job_id, "source_type": "document", "phase": "reducing"}

        def fail_kg_extraction_job(self, job_id, *, lease_token, error):
            """合法多实体 resolution 不应进入失败收口。"""
            raise AssertionError(
                f"resolution unexpectedly failed: {job_id} {lease_token} {error}"
            )

    chat = _KgStepChat([json.dumps(resolution)])
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.process_kg_extraction_job_step(
        _kg_step_job(phase="resolving")
    )

    assert result["phase"] == "reducing"
    assert saved["call"] == ("kg_job_1", "lease_1", resolution)
    assert len(chat.calls) == 1
    assert "local_console" in chat.calls[0][1]
    assert "local_admin" in chat.calls[0][1]


def test_admin_app_document_kg_reduce_reloads_staging_and_completes_snapshot():
    """document/reducing 重读 Map/resolution，确定性 Reduce 后原子完成整篇文档。"""
    console = _kg_step_map_entity("local_console", "控制台")
    admin = _kg_step_map_entity("local_admin", "管理后台", char_start=10)
    permission = _kg_step_map_entity(
        "local_permission",
        "管理员权限",
        entity_type="role_permission_channel",
        char_start=20,
    )
    relation = _kg_step_map_relation(
        "local_rel_requires",
        "local_console",
        "local_permission",
    )
    map_results = [
        _kg_step_map_result(
            entities=[console, admin, permission],
            relations=[relation],
        )
    ]
    resolution = {"groups": [["local_console", "local_admin"]]}
    expected = reduce_document_kg(
        premerge_document_kg_map_results(map_results),
        resolution,
    )
    calls = []
    saved = {}

    class FakeDatabase:
        def load_document_kg_map_results(self, job_id, *, lease_token):
            """Reduce 阶段重新读取全部 persisted Map staging。"""
            calls.append(("load_maps", job_id, lease_token))
            return map_results

        def complete_document_kg_extraction_job(
            self,
            job_id,
            *,
            lease_token,
            extraction,
        ):
            """记录确定性 Reduce 结果并模拟整文档 snapshot 发布。"""
            calls.append(("complete_document", job_id, lease_token))
            saved["extraction"] = extraction
            return {"id": job_id, "source_type": "document", "phase": "completed"}

        def fail_kg_extraction_job(self, job_id, *, lease_token, error):
            """合法 Reduce 不应进入失败收口。"""
            raise AssertionError(f"Reduce unexpectedly failed: {job_id} {lease_token} {error}")

    chat = _KgStepChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.process_kg_extraction_job_step(
        _kg_step_job(phase="reducing", resolution_result=resolution)
    )

    assert result["phase"] == "completed"
    assert calls == [
        ("load_maps", "kg_job_1", "lease_1"),
        ("complete_document", "kg_job_1", "lease_1"),
    ]
    assert saved["extraction"] == expected
    assert all("_supports" not in entity for entity in expected["entities"])
    assert chat.calls == []


def test_admin_app_kg_job_step_failure_uses_same_lease_and_bounded_error():
    """任一 phase 异常都用 claimed lease 标 failed，错误最多保留 1000 字符。"""
    failure = RuntimeError("模型失败：" + "x" * 1200)
    failed = {}

    class FakeDatabase:
        def get_faq(self, faq_id):
            """返回会在 Chat 阶段失败的当前 FAQ。"""
            return {
                "id": faq_id,
                "question": "如何打开控制台？",
                "answer": "打开控制台。",
                "status": "usable",
            }

        def fail_kg_extraction_job(self, job_id, *, lease_token, error):
            """记录 phase runner 失败时使用的原 job ID、lease 与截断错误。"""
            failed.update(job_id=job_id, lease_token=lease_token, error=error)
            return {"id": job_id, "phase": "failed", "error": error}

    chat = _KgStepChat(error=failure)
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.process_kg_extraction_job_step(
        _kg_step_job(source_type="faq", phase="mapping")
    )

    assert result["phase"] == "failed"
    assert failed["job_id"] == "kg_job_1"
    assert failed["lease_token"] == "lease_1"
    assert failed["error"].startswith("模型失败：")
    assert len(failed["error"]) == 1000
    assert len(chat.calls) == 1


@pytest.mark.parametrize(
    ("source_type", "phase"),
    [("faq", "resolving"), ("document", "archived")],
)
def test_admin_app_kg_job_step_rejects_faq_resolution_and_unknown_phase(
    source_type,
    phase,
):
    """FAQ resolving 与未知 phase 必须显式失败，不能落入其他来源 fallback。"""
    failed = {}

    class FakeDatabase:
        def fail_kg_extraction_job(self, job_id, *, lease_token, error):
            """记录非法 dispatch 的明确失败，不提供任何可误调的阶段方法。"""
            failed.update(job_id=job_id, lease_token=lease_token, error=error)
            return {"id": job_id, "phase": "failed", "error": error}

    chat = _KgStepChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", chat_model=chat.model),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.process_kg_extraction_job_step(
        _kg_step_job(source_type=source_type, phase=phase)
    )

    assert result["phase"] == "failed"
    assert failed["job_id"] == "kg_job_1"
    assert failed["lease_token"] == "lease_1"
    assert source_type in failed["error"].lower()
    assert phase in failed["error"].lower()
    assert chat.calls == []


def test_retrieval_eval_item_payload_exposes_readable_source_fields():
    """评测候选应带可读来源信息，避免用户只能靠内部 id 标注期望命中。"""
    document = RetrievedKnowledgeChunk(
        id="kc_doc_child_1",
        source_id="imp_1",
        source_type="document",
        source_chunk_id="chunk_1",
        parent_chunk_id="kc_doc_parent_1",
        chunk_level="child",
        source_title="售后手册.pdf",
        section_path=["售后", "报告导出"],
        page_start=3,
        page_end=4,
        block_type="text",
        source_offsets={},
        content="报告导出失败时，先检查账号权限和网络状态。",
        metadata={"source_excerpt": "检查账号权限"},
        tags=[],
        confidence=None,
        status="usable",
        score=0.88,
    )
    candidate = FusedCandidate(
        channels=("vector", "keyword"),
        fused_score=0.91,
        vector_score=0.88,
        keyword_score=0.42,
        document=document,
    )

    payload = retrieval_eval_item_payload(candidate)

    assert payload["id"] == "kc_doc_child_1"
    assert payload["source_id"] == "imp_1"
    assert payload["source_title"] == "售后手册.pdf"
    assert payload["source_chunk_id"] == "chunk_1"
    assert payload["parent_chunk_id"] == "kc_doc_parent_1"
    assert payload["chunk_level"] == "child"
    assert payload["section_path"] == ["售后", "报告导出"]
    assert payload["page_start"] == 3
    assert payload["page_end"] == 4
    assert payload["block_type"] == "text"
    assert payload["content"] == "报告导出失败时，先检查账号权限和网络状态。"
    assert "metadata" not in payload


def test_admin_retrieval_payloads_reject_noncanonical_objects():
    """Admin DTO 只接收 canonical chunk/FusedCandidate，不保留匿名对象 fallback。"""
    with pytest.raises(TypeError, match="RetrievedKnowledgeChunk"):
        assistant_document_payload(SimpleNamespace())
    with pytest.raises(TypeError, match="FusedCandidate"):
        retrieval_eval_item_payload(SimpleNamespace())


@pytest.mark.parametrize("source_type", ["kg_entity", "kg_relation", "unknown"])
def test_retrieval_eval_item_payload_rejects_nonfinal_source_types(source_type):
    """评测最终候选只允许 FAQ/文档，合成 KG 与未知来源必须留在诊断区。"""
    document = RetrievedKnowledgeChunk(
        id="kc_synthetic_1",
        source_id="source_1",
        source_type=source_type,
        source_chunk_id=None,
        parent_chunk_id=None,
        chunk_level="chunk",
        source_title="诊断事实",
        section_path=[],
        page_start=None,
        page_end=None,
        block_type="text",
        source_offsets={},
        content="只允许出现在 KG 诊断中的内容",
        metadata={},
        tags=[],
        confidence=None,
        status="usable",
        score=0.8,
    )
    candidate = FusedCandidate(
        document=document,
        fused_score=0.8,
        channels=("kg",),
    )

    with pytest.raises(ValueError, match="source_type"):
        retrieval_eval_item_payload(candidate)


@pytest.mark.parametrize(("chunk_id", "source_id"), [("", "faq_1"), ("kc_faq_1", "")])
def test_retrieval_eval_item_payload_rejects_missing_canonical_ids(chunk_id, source_id):
    """评测候选必须携带非空知识行 ID 与来源 ID，禁止用别名或 metadata 修复。"""
    document = RetrievedKnowledgeChunk(
        id=chunk_id,
        source_id=source_id,
        source_type="faq",
        source_chunk_id=None,
        parent_chunk_id=None,
        chunk_level="chunk",
        source_title="报告导出失败怎么办？",
        section_path=[],
        page_start=None,
        page_end=None,
        block_type="faq",
        source_offsets={},
        content="先检查账号权限。",
        metadata={"id": "metadata_fallback_forbidden"},
        tags=[],
        confidence=None,
        status="usable",
        score=0.8,
    )
    candidate = FusedCandidate(
        document=document,
        fused_score=0.8,
        channels=("vector",),
    )

    with pytest.raises(ValueError, match="non-empty id and source_id"):
        retrieval_eval_item_payload(candidate)


def test_retrieval_eval_item_payload_uses_only_canonical_faq_fields():
    """FAQ 评测候选只保留 canonical 标题与正文，不输出重复 wire 别名。"""
    content = "问题：报告导出失败怎么办？\n答案：先检查账号权限，再重新生成报告。"
    document = RetrievedKnowledgeChunk(
        id="kc_faq_1",
        source_id="faq_1",
        source_type="faq",
        source_chunk_id=None,
        parent_chunk_id=None,
        chunk_level="chunk",
        source_title="报告导出失败怎么办？",
        section_path=[],
        page_start=None,
        page_end=None,
        block_type="faq",
        source_offsets={},
        content=content,
        metadata={"category": "报表"},
        tags=["报告", "导出"],
        confidence="high",
        status="usable",
        score=0.72,
    )
    candidate = FusedCandidate(
        channels=("vector",),
        fused_score=0.72,
        vector_score=0.72,
        keyword_score=None,
        document=document,
    )

    payload = retrieval_eval_item_payload(candidate)

    assert payload["source_title"] == "报告导出失败怎么办？"
    assert payload["content"] == content
    for redundant_field in ("question", "answer", "category", "tags", "metadata"):
        assert redundant_field not in payload


def test_kg_fact_analysis_payload_rejects_unknown_expanded_fact():
    """证据展开若返回未查询的 fact 必须失败，不能静默丢失诊断关联。"""
    known = SimpleNamespace(
        fact_chunk_id="kc_kg_entity_known",
        fact_id="kg_entity_known",
        fact_type="kg_entity",
        fact_rank=1,
        fact_score=0.8,
    )
    unknown = SimpleNamespace(
        fact_chunk_id="kc_kg_relation_unknown",
        fact_id="kg_relation_unknown",
        fact_type="kg_relation",
        fact_rank=2,
        fact_score=0.7,
    )
    candidate = SimpleNamespace(
        document=SimpleNamespace(id="kc_faq_1"),
        kg_matches=(unknown,),
    )

    with pytest.raises(ValueError, match="unknown KG fact"):
        kg_fact_analysis_payload([known], [candidate])


def test_admin_app_settings_snapshot_masks_sensitive_config_for_frontend(tmp_path):
    """设置中心快照只给前端脱敏敏感值，避免浏览器拿到已保存明文。"""
    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://user:pass@127.0.0.1:5432/app",
            chat_base_url="https://chat.example/v1",
            chat_api_key="chat-secret",
            chat_model="deepseek-chat",
            embedding_base_url="https://embed.example/v1",
            embedding_api_key="embedding-secret",
            embedding_model="text-embedding-v4",
            embedding_dimensions=1024,
            wechat_token_file=tmp_path / "token.json",
            wechat_message_chunk_size=1800,
            rag_top_k=6,
            rag_min_score=0.42,
            upload_dir=tmp_path / "uploads",
            mineru_api_token="mineru-secret",
            mineru_parse_timeout_seconds=600,
            mineru_use_kb_packager=True,
            document_chunk_token_num=512,
            document_chunker_type="naive",
            document_chunk_delimiter="\n。；！？",
            document_chunk_overlap_percent=0,
            document_children_delimiter="",
            document_table_context_size=0,
            document_image_context_size=0,
            rerank_base_url="",
            rerank_api_key="",
            rerank_model="",
            rerank_input_size=50,
        )
    )

    snapshot = app.settings_snapshot()

    assert snapshot["mineru_api_token"] != "mineru-secret"
    assert snapshot["chat_api_key"] != "chat-secret"
    assert snapshot["embedding_api_key"] != "embedding-secret"
    assert snapshot["database_url"] != "postgresql://user:pass@127.0.0.1:5432/app"
    assert "pass" not in snapshot["database_url"]
    assert snapshot["chat_api_key_configured"] is True
    assert snapshot["embedding_api_key_configured"] is True
    assert snapshot["mineru_api_token_configured"] is True
    assert "••" in snapshot["chat_api_key"]
    assert "••" in snapshot["embedding_api_key"]
    assert "••" in snapshot["mineru_api_token"]
    assert "mineru_batch_file_url" not in snapshot
    assert snapshot["rag_top_k"] == 6
    assert snapshot["wechat_token_file"].endswith("token.json")
    assert snapshot["document_chunk_token_num"] == 512
    assert snapshot["document_chunker_type"] == "naive"
    assert snapshot["document_chunk_delimiter"] == "\n。；！？"


def test_admin_app_update_settings_preserves_sensitive_values_when_payload_is_blank(tmp_path):
    """设置弹窗留空敏感字段时应保留旧值，只在输入新值时覆盖。"""
    settings_file = tmp_path / "settings.local.json"
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://user:oldpass@127.0.0.1:5432/app",
            "CHAT_BASE_URL": "https://chat.example/v1",
            "CHAT_API_KEY": "old-chat-key",
            "CHAT_MODEL": "old-model",
            "EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "EMBEDDING_API_KEY": "old-embedding-key",
            "EMBEDDING_MODEL": "text-embedding-v4",
            "MINERU_API_TOKEN": "old-mineru-token",
            "RERANK_BASE_URL": "https://rerank.example/v1",
            "RERANK_API_KEY": "old-rerank-key",
            "RERANK_MODEL": "rerank-model",
        }
    )
    app = AdminApp(settings, settings_file=settings_file)

    snapshot = app.update_settings(
        {
            "database_url": "",
            "chat_base_url": "https://chat-new.example/v1",
            "chat_api_key": "",
            "chat_model": "deepseek-chat",
            "embedding_api_key": "   ",
            "mineru_api_token": "",
            "rerank_api_key": "",
        }
    )

    saved_settings = json.loads(settings_file.read_text(encoding="utf-8"))
    tenant = saved_settings["tenants"]["default"]
    assert app.settings.database_url == "postgresql://user:oldpass@127.0.0.1:5432/app"
    assert app.settings.chat_api_key == "old-chat-key"
    assert app.settings.embedding_api_key == "old-embedding-key"
    assert app.settings.mineru_api_token == "old-mineru-token"
    assert app.settings.rerank_api_key == "old-rerank-key"
    assert tenant["database_url"] == "postgresql://user:oldpass@127.0.0.1:5432/app"
    assert tenant["chat_api_key"] == "old-chat-key"
    assert tenant["embedding_api_key"] == "old-embedding-key"
    assert tenant["mineru_api_token"] == "old-mineru-token"
    assert tenant["rerank_api_key"] == "old-rerank-key"
    assert snapshot["chat_api_key"] != "old-chat-key"
    assert snapshot["chat_api_key_configured"] is True


def test_admin_app_probe_chat_provider_uses_saved_key_when_payload_omits_key(monkeypatch):
    """测试连接留空 key 时应由后端使用已保存 key，前端仍不能读取明文。"""
    captured = []

    class FakeCompletions:
        def create(self, *, model, messages, temperature):
            captured.append(("complete", model, messages, temperature))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])

    class FakeClient:
        chat = SimpleNamespace(completions=FakeCompletions())

    def fake_build_openai_client(base_url, api_key):
        captured.append(("client", base_url, api_key))
        return FakeClient()

    monkeypatch.setattr("cyclops.admin_server.build_openai_client", fake_build_openai_client)
    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            chat_base_url="https://saved.example/v1",
            chat_api_key="saved-chat-key",
            chat_model="saved-model",
        )
    )

    result = app.probe_chat_provider(
        {
            "chat_base_url": "https://draft.example/v1",
            "chat_api_key": "",
            "chat_model": "draft-model",
        }
    )

    assert result["ok"] is True
    assert result["model"] == "draft-model"
    assert captured[0] == ("client", "https://draft.example/v1", "saved-chat-key")


def test_admin_app_list_chat_provider_models_uses_saved_key_when_payload_omits_key(monkeypatch):
    """拉取模型留空 key 时应由后端使用已保存 key，Authorization 不依赖前端回填。"""
    captured = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "z-model", "owned_by": "vendor"},
                    {"id": "a-model"},
                ]
            }

    def fake_get(url, *, headers, timeout):
        captured.append((url, headers, timeout))
        return FakeResponse()

    monkeypatch.setattr("cyclops.admin_server.requests.get", fake_get)
    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            chat_base_url="https://saved.example/v1",
            chat_api_key="saved-chat-key",
            chat_model="saved-model",
        )
    )

    result = app.list_chat_provider_models({"chat_base_url": "https://draft.example/v1"})

    assert result == {
        "ok": True,
        "items": [
            {"id": "a-model", "owned_by": ""},
            {"id": "z-model", "owned_by": "vendor"},
        ],
    }
    assert captured == [
        (
            "https://draft.example/v1/models",
            {"Authorization": "Bearer saved-chat-key"},
            15.0,
        )
    ]


def test_admin_app_update_settings_persists_local_tenant_settings_and_refreshes_runtime_config(tmp_path):
    """保存设置应写入本地租户配置文件，不修改 .env，并立即刷新运行时配置。"""
    settings_file = tmp_path / "settings.local.json"
    env_file = tmp_path / ".env"
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://old@127.0.0.1:5432/app",
            "CHAT_BASE_URL": "https://old-chat.example/v1",
            "CHAT_API_KEY": "old-chat-key",
            "CHAT_MODEL": "old-model",
            "EMBEDDING_BASE_URL": "https://old-embedding.example/v1",
            "EMBEDDING_API_KEY": "old-embedding-key",
            "EMBEDDING_MODEL": "text-embedding-v4",
        }
    )
    app = AdminApp(settings, settings_file=settings_file)

    snapshot = app.update_settings(
        {
            "database_url": "postgresql://new@127.0.0.1:5432/app",
            "chat_base_url": "https://new-chat.example/v1",
            "chat_api_key": "new-chat-key",
            "chat_model": "mimo-v2.5-pro",
            "embedding_base_url": "https://new-embedding.example/v1",
            "embedding_api_key": "new-embedding-key",
            "embedding_model": "text-embedding-v4",
            "embedding_dimensions": "1024",
            "wechat_token_file": str(tmp_path / "token.json"),
            "wechat_message_chunk_size": "1800",
            "rag_top_k": "7",
            "rag_min_score": "0.4",
            "upload_dir": str(tmp_path / "uploads"),
            "mineru_api_token": "new-mineru-token",
            "mineru_parse_timeout_seconds": "600",
            "mineru_use_kb_packager": False,
            "document_chunk_token_num": "256",
            "document_chunker_type": "table",
            "document_chunk_delimiter": "`###`",
            "document_chunk_overlap_percent": "10",
            "document_children_delimiter": r"\n",
            "document_table_context_size": "128",
            "document_image_context_size": "96",
        }
    )

    saved_settings = json.loads(settings_file.read_text(encoding="utf-8"))
    assert snapshot["chat_model"] == "mimo-v2.5-pro"
    assert "mineru_batch_file_url" not in snapshot
    assert "mineru_batch_result_url_template" not in snapshot
    assert "mineru_api_url" not in snapshot
    assert app.settings.chat_api_key == "new-chat-key"
    assert not env_file.exists()
    assert saved_settings["version"] == 1
    assert saved_settings["active_tenant_id"] == "default"
    assert saved_settings["tenants"]["default"]["chat_model"] == "mimo-v2.5-pro"
    assert "mineru_batch_file_url" not in saved_settings["tenants"]["default"]
    assert "mineru_batch_result_url_template" not in saved_settings["tenants"]["default"]
    assert saved_settings["tenants"]["default"]["mineru_use_kb_packager"] is False
    assert saved_settings["tenants"]["default"]["document_chunk_token_num"] == 256
    assert saved_settings["tenants"]["default"]["document_chunker_type"] == "table"
    assert saved_settings["tenants"]["default"]["document_chunk_delimiter"] == "`###`"
    assert saved_settings["tenants"]["default"]["document_chunk_overlap_percent"] == 10
    assert saved_settings["tenants"]["default"]["document_children_delimiter"] == r"\n"
    assert saved_settings["tenants"]["default"]["document_table_context_size"] == 128
    assert saved_settings["tenants"]["default"]["document_image_context_size"] == 96
    assert (settings_file.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "payload",
    [
        {"database_url": "postgresql://new@127.0.0.1:5432/app"},
        {"db_pool_min_size": "2", "db_pool_max_size": "6"},
    ],
    ids=["database-url", "pool-size"],
)
def test_admin_app_update_settings_closes_database_when_pool_config_changes(
    tmp_path,
    payload,
):
    """数据库 URL 或连接池参数变化时必须关闭旧实例，不得泄漏或沿用旧池。"""

    class FakeDatabase:
        """记录关闭次数，模拟 AdminApp 已缓存的连接池。"""

        def __init__(self):
            """初始化未关闭状态。"""
            self.close_count = 0

        def close(self):
            """记录连接池关闭。"""
            self.close_count += 1

    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://old@127.0.0.1:5432/app",
            "CHAT_BASE_URL": "https://chat.example/v1",
            "CHAT_API_KEY": "chat-key",
            "CHAT_MODEL": "chat-model",
            "EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "EMBEDDING_API_KEY": "embedding-key",
            "EMBEDDING_MODEL": "embedding-model",
            "DB_POOL_MIN_SIZE": "1",
            "DB_POOL_MAX_SIZE": "4",
        }
    )
    database = FakeDatabase()
    app = AdminApp(
        settings,
        db=database,
        settings_file=tmp_path / "settings.local.json",
    )

    app.update_settings(payload)

    assert database.close_count == 1
    assert app.db is None


def test_admin_app_update_settings_rebuilds_assistant_stream_guard_when_limit_changes(
    tmp_path,
):
    """问答并发上限变化时必须丢弃旧信号量，下一次按新配置构建。"""
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://unused",
            "CHAT_BASE_URL": "https://chat.example/v1",
            "CHAT_API_KEY": "chat-key",
            "CHAT_MODEL": "chat-model",
            "EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "EMBEDDING_API_KEY": "embedding-key",
            "EMBEDDING_MODEL": "embedding-model",
            "ASSISTANT_MAX_CONCURRENT_STREAMS": "1",
        }
    )
    app = AdminApp(settings, settings_file=tmp_path / "settings.local.json")
    old_guard = app._assistant_stream_guard()

    app.update_settings({"assistant_max_concurrent_streams": "2"})
    new_guard = app._assistant_stream_guard()

    assert new_guard is not old_guard
    assert new_guard.acquire(blocking=False)
    assert new_guard.acquire(blocking=False)
    new_guard.release()
    new_guard.release()


def test_admin_app_update_settings_preserves_document_chunking_when_payload_omits_fields(tmp_path):
    """设置页未提交文档分块字段时，应保留当前运行配置，避免无关保存覆盖自定义值。"""
    settings_file = tmp_path / "settings.local.json"
    settings = Settings.from_env(
        {
            "DATABASE_URL": "postgresql://old@127.0.0.1:5432/app",
            "CHAT_BASE_URL": "https://old-chat.example/v1",
            "CHAT_API_KEY": "old-chat-key",
            "CHAT_MODEL": "old-model",
            "EMBEDDING_BASE_URL": "https://old-embedding.example/v1",
            "EMBEDDING_API_KEY": "old-embedding-key",
            "EMBEDDING_MODEL": "text-embedding-v4",
            "DOCUMENT_CHUNK_TOKEN_NUM": "384",
            "DOCUMENT_CHUNKER_TYPE": "manual",
            "DOCUMENT_CHUNK_DELIMITER": "`@@`",
            "DOCUMENT_CHUNK_OVERLAP_PERCENT": "12",
            "DOCUMENT_CHILDREN_DELIMITER": r"\n+",
            "DOCUMENT_TABLE_CONTEXT_SIZE": "64",
            "DOCUMENT_IMAGE_CONTEXT_SIZE": "32",
        }
    )
    app = AdminApp(settings, settings_file=settings_file)

    snapshot = app.update_settings(
        {
            "database_url": "postgresql://new@127.0.0.1:5432/app",
            "chat_base_url": "https://new-chat.example/v1",
            "chat_api_key": "new-chat-key",
            "chat_model": "mimo-v2.5-pro",
            "embedding_base_url": "https://new-embedding.example/v1",
            "embedding_api_key": "new-embedding-key",
            "embedding_model": "text-embedding-v4",
            "embedding_dimensions": "1024",
            "wechat_token_file": str(tmp_path / "token.json"),
            "wechat_message_chunk_size": "1800",
            "rag_top_k": "7",
            "rag_min_score": "0.4",
            "upload_dir": str(tmp_path / "uploads"),
            "mineru_api_token": "new-mineru-token",
            "mineru_parse_timeout_seconds": "600",
            "mineru_use_kb_packager": True,
        }
    )

    saved_settings = json.loads(settings_file.read_text(encoding="utf-8"))
    tenant = saved_settings["tenants"]["default"]
    assert snapshot["document_chunk_token_num"] == 384
    assert snapshot["document_chunker_type"] == "manual"
    assert snapshot["document_chunk_delimiter"] == "`@@`"
    assert snapshot["document_chunk_overlap_percent"] == 12
    assert snapshot["document_children_delimiter"] == r"\n+"
    assert snapshot["document_table_context_size"] == 64
    assert snapshot["document_image_context_size"] == 32
    assert tenant["document_chunk_token_num"] == 384
    assert tenant["document_chunker_type"] == "manual"
    assert tenant["document_children_delimiter"] == r"\n+"


def _admin_parse_job(**overrides):
    """构造 Admin 持久解析任务 current row，禁止依赖字段默认补齐。"""
    return {
        "id": "parse_job_1",
        "file_id": "imp_1",
        "status": "queued",
        "chunker_type": "naive",
        "input_fingerprint": "sha256:fixture",
        "provider_batch_id": None,
        "provider_file_name": None,
        "progress": {},
        "error": None,
        "lease_token": None,
        "lease_expires_at": None,
        "next_poll_at": "later",
        "created_at": "created",
        "updated_at": "updated",
        **overrides,
    }


def test_admin_app_create_import_file_queues_markdown_parse(tmp_path):
    """自动解析上传只创建 queued job，不在请求内生成切片。"""
    calls = []

    class FakeDatabase:
        def create_import_file(self, row):
            """保存文件记录供随后入队读取。"""
            calls.append(("file", row))
            self.record = {**row, "created_at": "now", "updated_at": "now"}
            return self.record

        def get_import_file(self, file_id):
            """返回刚保存的文件，模拟真实 create 后入队。"""
            assert file_id == self.record["id"]
            return self.record

        def create_import_parse_job(self, file_id, **fields):
            """记录 queued job，禁止同步切片写入。"""
            calls.append(("job", file_id, fields))
            return _admin_parse_job(file_id=file_id, **fields)

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )
    markdown = b"- [2025-08-25 16:20] A: \xe6\x8a\xa5\xe5\x91\x8a\xe6\xb2\xa1\xe7\x94\x9f\xe6\x88\x90\xe6\x80\x8e\xe4\xb9\x88\xe5\x8a\x9e\n- [2025-08-25 16:21] B: \xe9\x9a\x9410\xe5\x88\x86\xe9\x92\x9f\xe5\x88\xb7\xe6\x96\xb0\n"

    result = app.create_import_file("chat.md", markdown)

    assert result["file_type"] == "markdown"
    assert result["parser"] == "markdown_chat"
    assert result["status"] == "processing"
    assert result["parse_job"]["status"] == "queued"
    assert [call[0] for call in calls] == ["file", "job"]
    assert calls[1][2]["input_fingerprint"].startswith("sha256:")


def test_admin_app_create_import_file_can_store_without_parsing(tmp_path):
    """文档管理页上传文件只保存原件，用户点击解析后才生成切块。"""
    calls = []

    class FakeDatabase:
        def create_import_file(self, row):
            calls.append(("file", row))
            return {**row, "created_at": "now", "updated_at": "now"}

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    result = app.create_import_file("manual.pdf", b"%PDF-1.7", auto_parse=False)

    assert result["file_type"] == "pdf"
    assert result["parser"] == "mineru"
    assert result["status"] == "pending"
    assert calls == [
        (
            "file",
            {
                "id": result["id"],
                "original_name": "manual.pdf",
                "stored_path": str(tmp_path / f"{result['id']}_manual.pdf"),
                "file_type": "pdf",
                "parser": "mineru",
                "chunker_type": "naive",
                "status": "pending",
            },
        )
    ]


def test_admin_app_exposes_no_synchronous_reparse_compatibility_method():
    """0→1 持久任务硬切后不得保留同步 reparse 包装。"""
    assert not hasattr(AdminApp, "reparse_import_file")


def test_admin_app_create_import_file_queues_pdf_without_calling_mineru(
    tmp_path,
    monkeypatch,
):
    """自动解析 PDF 只落 queued job，请求阶段不得实例化 MinerU。"""
    calls = []

    class FakeDatabase:
        def create_import_file(self, row):
            """保存文件记录供随后入队读取。"""
            calls.append(("file", row))
            self.record = row
            return row

        def get_import_file(self, file_id):
            """返回刚保存的 PDF。"""
            assert file_id == self.record["id"]
            return self.record

        def create_import_parse_job(self, file_id, **fields):
            """记录唯一 queued 任务。"""
            calls.append(("job", file_id, fields))
            return _admin_parse_job(file_id=file_id, **fields)

    class ForbiddenMineruClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("upload request must not instantiate MinerU")

    monkeypatch.setattr("cyclops.admin_server.MineruClient", ForbiddenMineruClient)

    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=tmp_path,
            mineru_api_token="mineru-token",
            mineru_parse_timeout_seconds=30,
            mineru_use_kb_packager=True,
        ),
        db=FakeDatabase(),
    )

    result = app.create_import_file("manual.pdf", b"%PDF")

    assert result["file_type"] == "pdf"
    assert result["parser"] == "mineru"
    assert result["status"] == "processing"
    assert result["parse_job"]["status"] == "queued"
    assert [call[0] for call in calls] == ["file", "job"]


def test_admin_app_build_document_import_chunks_requires_explicit_chunker_type(monkeypatch):
    """后台切片构建器必须接收显式 chunker，不能回退全局配置。"""
    captured = {}

    def fake_build_import_chunks(file_id, blocks, **kwargs):
        """记录解析层收到的显式参数，避免测试依赖真实切块算法。"""
        captured.update({"file_id": file_id, "blocks": blocks, **kwargs})
        return [{"id": "chunk_1", "source_text": "ok"}]

    monkeypatch.setattr(
        "cyclops.admin_server.build_import_chunks_from_blocks",
        fake_build_import_chunks,
    )
    app = AdminApp(
        SimpleNamespace(
            document_chunk_token_num=256,
            document_chunker_type="table",
            document_chunk_delimiter="`###`",
            document_chunk_overlap_percent=10,
            document_children_delimiter=r"\n",
            document_table_context_size=64,
            document_image_context_size=32,
        )
    )

    blocks = [
        ParsedBlock(
            text="账号登录",
            block_type="title",
            page_number=1,
            section_title="账号登录",
            evidence={"source_file": "manual.pdf"},
        )
    ]
    with pytest.raises(TypeError):
        app._build_document_import_chunks("imp_1", blocks)
    with pytest.raises(AdminValidationError, match="chunker_type"):
        app._build_document_import_chunks("imp_1", blocks, chunker_type=None)

    rows = app._build_document_import_chunks("imp_1", blocks, chunker_type="manual")

    assert rows == [{"id": "chunk_1", "source_text": "ok"}]
    assert captured["file_id"] == "imp_1"
    assert captured["chunker_type"] == "manual"
    assert captured["chunk_token_num"] == 256
    assert captured["delimiter"] == "`###`"


def test_admin_app_starts_mineru_parse_job_without_blocking_for_result(tmp_path, monkeypatch):
    """parse-job POST 只创建 queued 任务，请求阶段不调用 MinerU。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回具备当前必填 chunker 的固定待解析文件。"""
            assert file_id == "imp_1"
            return {
                "id": "imp_1",
                "original_name": "manual.pdf",
                "stored_path": str(source),
                "file_type": "pdf",
                "parser": "mineru",
                "chunker_type": "naive",
                "status": "pending",
            }

        def create_import_parse_job(self, file_id, **fields):
            """记录入队参数并返回数据库 queued 行。"""
            calls.append(("job", file_id, fields))
            return _admin_parse_job(file_id=file_id, **fields)

    class ForbiddenMineruClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("enqueue must not call MinerU")

    monkeypatch.setattr("cyclops.admin_server.MineruClient", ForbiddenMineruClient)

    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=tmp_path,
            mineru_api_token="mineru-token",
            mineru_parse_timeout_seconds=30,
            mineru_use_kb_packager=True,
        ),
        db=FakeDatabase(),
    )

    result = app.start_import_parse_job("imp_1", {"chunker_type": "naive"})

    assert result["status"] == "queued"
    assert result["percent"] == 0
    assert "lease_token" not in result
    assert calls[0][0:2] == ("job", "imp_1")
    assert calls[0][2]["chunker_type"] == "naive"
    assert calls[0][2]["input_fingerprint"].startswith("sha256:")


def test_admin_import_parse_status_requires_embedding_summary_database_contract():
    """文件详情必须直接依赖当前 embedding 摘要接口，不能静默省略。"""

    class FakeDatabase:
        """只实现文件和任务读取，故意缺少当前摘要接口。"""

        def get_import_file(self, file_id):
            """返回固定文件记录。"""
            return {"id": file_id, "status": "processing"}

        def get_latest_import_parse_job_for_file(self, file_id):
            """当前没有解析任务，测试只聚焦摘要接口。"""
            return None

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
    )

    with pytest.raises(AttributeError, match="get_import_file_embedding_summary"):
        app.get_import_file("imp_1")


def test_admin_import_parse_status_rejects_string_parse_progress():
    """任务 DTO 只接受 JSON object progress，不解析不存在的字符串格式。"""
    with pytest.raises(TypeError, match="progress must be a JSON object"):
        AdminApp._import_parse_job_payload(
            _admin_parse_job(progress='{"state":"running"}')
        )


def test_admin_app_start_mineru_parse_job_persists_payload_chunker_type(tmp_path):
    """入队时把显式 chunker 写进 job，后续 worker 只读该任务路线。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            assert file_id == "imp_1"
            return {
                "id": "imp_1",
                "original_name": "manual.pdf",
                "stored_path": str(source),
                "file_type": "pdf",
                "parser": "mineru",
                "status": "pending",
                "chunker_type": "naive",
            }

        def create_import_parse_job(self, file_id, **fields):
            """记录 job 的 canonical chunker 与输入指纹。"""
            calls.append(("job", file_id, fields))
            return _admin_parse_job(file_id=file_id, **fields)

    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=tmp_path,
            mineru_api_token="mineru-token",
            mineru_parse_timeout_seconds=30,
            mineru_use_kb_packager=True,
        ),
        db=FakeDatabase(),
    )

    result = app.start_import_parse_job("imp_1", {"chunker_type": "table"})

    assert result["chunker_type"] == "table"
    assert calls[0][2]["chunker_type"] == "table"


def test_admin_app_start_mineru_parse_job_rejects_unknown_chunker_type(tmp_path):
    """chunker_type 必须显式受控，避免出现不可审计的 auto/lightweight 路线。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回具备当前必填 chunker 的固定待解析文件。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "stored_path": str(source),
                "file_type": "pdf",
                "parser": "mineru",
                "chunker_type": "naive",
                "status": "pending",
            }

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminValidationError, match="chunker_type"):
        app.start_import_parse_job("imp_1", {"chunker_type": "lightweight"})


@pytest.mark.parametrize("chunker_type", [None, "", " ", "TABLE", " table "])
def test_admin_parse_job_rejects_noncanonical_explicit_chunker(
    chunker_type,
    tmp_path,
):
    """parse-job 显式字段必须是 canonical 枚举，非法值不能退回文件当前路线。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回固定 MinerU 文件，使测试只聚焦显式 chunker。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "stored_path": str(source),
                "parser": "mineru",
                "chunker_type": "naive",
            }

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminValidationError, match="chunker_type"):
        app.start_import_parse_job("imp_1", {"chunker_type": chunker_type})


def test_admin_parse_job_exposes_no_removed_chunker_compatibility_helpers():
    """显式 job chunker 生效后不保留旧记录补值 helper。"""
    assert not hasattr(AdminApp, "_document_chunker_type_from_record")
    assert not hasattr(AdminApp, "_document_chunker_update_from_payload")


def test_admin_worker_polling_mineru_updates_persistent_progress(tmp_path, monkeypatch):
    """worker 轮询 running 状态后写 job progress 并释放 lease。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回具备当前必填 chunker 的固定解析中记录。"""
            assert file_id == "imp_1"
            return {
                "id": "imp_1",
                "original_name": "manual.pdf",
                "stored_path": str(source),
                "file_type": "pdf",
                "parser": "mineru",
                "chunker_type": "naive",
                "status": "processing",
            }

        def update_import_parse_job_progress(self, job_id, **fields):
            """记录 worker 写入的 provider 进度与下次轮询时间。"""
            calls.append(("progress", job_id, fields))
            return _admin_parse_job(
                id=job_id,
                status=fields["status"],
                provider_batch_id=fields["provider_batch_id"],
                provider_file_name=fields["provider_file_name"],
                progress=fields["progress"],
            )

        def fail_import_parse_job(self, *args, **kwargs):
            """本测试不应进入失败路径。"""
            raise AssertionError((args, kwargs))

    class FakeMineruClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_task_status(self, batch_id, file_name):
            assert (batch_id, file_name) == ("batch_1", "manual.pdf")
            return SimpleNamespace(
                batch_id=batch_id,
                file_name=file_name,
                state="running",
                progress={"extracted_pages": 3, "total_pages": 12},
                error=None,
                result={},
                zip_url=None,
            )

    monkeypatch.setattr("cyclops.admin_server.MineruClient", FakeMineruClient)

    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=tmp_path,
            mineru_api_token="mineru-token",
            mineru_parse_timeout_seconds=30,
            mineru_use_kb_packager=True,
            import_parse_worker_poll_interval_seconds=2,
        ),
        db=FakeDatabase(),
    )
    fingerprint = app._import_parse_input_fingerprint(
        app.database().get_import_file("imp_1"),
        source,
        chunker_type="naive",
    )

    result = app.process_import_parse_job(
        _admin_parse_job(
            status="polling",
            input_fingerprint=fingerprint,
            provider_batch_id="batch_1",
            provider_file_name="manual.pdf",
            lease_token="lease-current",
        )
    )

    assert result["status"] == "polling"
    assert calls[0][0:2] == ("progress", "parse_job_1")
    assert calls[0][2]["progress"] == {
        "extracted_pages": 3,
        "total_pages": 12,
        "state": "running",
    }
    assert calls[0][2]["provider_batch_id"] == "batch_1"
    assert calls[0][2]["lease_token"] == "lease-current"


def test_admin_worker_done_enters_finalizing_and_completes_atomically(
    tmp_path,
    monkeypatch,
):
    """done 必须先进入 finalizing，再把构建结果交给 DB 原子完成。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []
    asset_dirs = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回 worker 当前解析来源。"""
            assert file_id == "imp_1"
            return {
                "id": "imp_1",
                "original_name": "manual.pdf",
                "stored_path": str(source),
                "file_type": "pdf",
                "parser": "mineru",
                "chunker_type": "naive",
                "status": "processing",
            }

        def begin_import_parse_job_finalization(self, job_id, **fields):
            """记录显式 finalizing 转换并保留 provider locator。"""
            calls.append(("finalizing", job_id, fields))
            return _admin_parse_job(
                id=job_id,
                status="finalizing",
                input_fingerprint=fingerprint,
                provider_batch_id="batch_1",
                provider_file_name="manual.pdf",
                lease_token="lease-current",
                progress=fields["progress"],
            )

        def complete_import_parse_job(self, job_id, **fields):
            """记录原子完成 payload，模拟 DB 最终 completed 行。"""
            calls.append(("complete", job_id, fields))
            return _admin_parse_job(
                id=job_id,
                status="completed",
                input_fingerprint=fingerprint,
                progress=fields["progress"],
            )

        def fail_import_parse_job(self, *args, **kwargs):
            """本测试不应进入失败路径。"""
            raise AssertionError((args, kwargs))

    class FakeMineruClient:
        def __init__(self, *args, **kwargs):
            asset_dirs.append(kwargs.get("asset_output_dir"))

        def get_task_status(self, batch_id, file_name):
            assert (batch_id, file_name) == ("batch_1", "manual.pdf")
            return SimpleNamespace(
                batch_id=batch_id,
                file_name=file_name,
                state="done",
                progress={"extracted_pages": 12, "total_pages": 12},
                error=None,
                result={"file_name": "manual.pdf"},
                zip_url="https://cdn.example/result.zip",
            )

        def download_task_result(self, status):
            assert status.zip_url == "https://cdn.example/result.zip"
            return {
                "content_list": [
                    {"type": "title", "text": "账号登录", "page_idx": 0},
                    {"type": "text", "text": "先检查账号状态。", "page_idx": 0},
                ]
            }

    monkeypatch.setattr("cyclops.admin_server.MineruClient", FakeMineruClient)

    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=tmp_path,
            mineru_api_token="mineru-token",
            mineru_parse_timeout_seconds=30,
            mineru_use_kb_packager=True,
            document_chunk_token_num=512,
        ),
        db=FakeDatabase(),
    )
    fingerprint = app._import_parse_input_fingerprint(
        app.database().get_import_file("imp_1"),
        source,
        chunker_type="naive",
    )

    result = app.process_import_parse_job(
        _admin_parse_job(
            status="polling",
            input_fingerprint=fingerprint,
            provider_batch_id="batch_1",
            provider_file_name="manual.pdf",
            lease_token="lease-current",
        )
    )

    assert result["status"] == "completed"
    assert [call[0] for call in calls] == ["finalizing", "complete"]
    assert "先检查账号状态。" in calls[1][2]["chunks"][0]["source_text"]
    assert calls[1][2]["input_fingerprint"] == fingerprint
    assert calls[1][2]["progress"]["state"] == "completed"
    assert asset_dirs
    assert all(asset_dir == tmp_path / "mineru-assets" / "imp_1" for asset_dir in asset_dirs)


def test_admin_worker_finalizing_uses_job_chunker_type(tmp_path, monkeypatch):
    """恢复 finalizing 时必须使用 job chunker，不读取全局默认或旧文件值。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    captured = {}

    def fake_build_import_chunks(file_id, blocks, **kwargs):
        captured.update({"file_id": file_id, "blocks": blocks, **kwargs})
        return [{"id": "chunk_1", "source_text": "账号登录"}]

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回 stored chunker 不同的文件，验证 job 才是执行真相。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "stored_path": str(source),
                "parser": "mineru",
                "chunker_type": "naive",
            }

        def begin_import_parse_job_finalization(self, job_id, **fields):
            """恢复领取后允许 finalizing 保持原阶段。"""
            return _admin_parse_job(
                id=job_id,
                status="finalizing",
                chunker_type="manual",
                input_fingerprint=fingerprint,
                provider_batch_id="batch_1",
                provider_file_name="manual.pdf",
                lease_token="lease-current",
                progress=fields["progress"],
            )

        def complete_import_parse_job(self, job_id, **fields):
            """记录原子完成使用的切片。"""
            captured["complete"] = fields
            return _admin_parse_job(id=job_id, status="completed")

        def fail_import_parse_job(self, *args, **kwargs):
            """本测试不应进入失败路径。"""
            raise AssertionError((args, kwargs))

    class FakeMineruClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_task_status(self, batch_id, file_name):
            """恢复 finalizing 时重新读取 done 状态，不持久化签名 URL。"""
            return SimpleNamespace(
                batch_id=batch_id,
                file_name=file_name,
                state="done",
                progress={"total_pages": 1, "extracted_pages": 1},
                error=None,
                result={},
                zip_url="https://cdn.example/result.zip",
            )

        def download_task_result(self, status):
            return {
                "content_list": [
                    {"type": "title", "text": "账号登录", "page_idx": 0},
                    {"type": "text", "text": "先检查账号状态。", "page_idx": 0},
                ]
            }

    monkeypatch.setattr("cyclops.admin_server.MineruClient", FakeMineruClient)
    monkeypatch.setattr(
        "cyclops.admin_server.build_import_chunks_from_blocks",
        fake_build_import_chunks,
    )

    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=tmp_path,
            mineru_api_token="mineru-token",
            mineru_parse_timeout_seconds=30,
            mineru_use_kb_packager=True,
            document_chunker_type="naive",
        ),
        db=FakeDatabase(),
    )
    record = app.database().get_import_file("imp_1")
    fingerprint = app._import_parse_input_fingerprint(
        record,
        source,
        chunker_type="manual",
    )

    app.process_import_parse_job(
        _admin_parse_job(
            status="finalizing",
            chunker_type="manual",
            input_fingerprint=fingerprint,
            provider_batch_id="batch_1",
            provider_file_name="manual.pdf",
            lease_token="lease-current",
        )
    )

    assert captured["file_id"] == "imp_1"
    assert captured["chunker_type"] == "manual"
    assert captured["complete"]["input_fingerprint"] == fingerprint


def test_admin_worker_submitting_starts_provider_and_persists_locator(
    tmp_path,
    monkeypatch,
):
    """submitting 阶段只提交一次 MinerU，并把 locator 写入 polling job。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []
    record = {
        "id": "imp_1",
        "original_name": "manual.pdf",
        "stored_path": str(source),
        "parser": "mineru",
        "chunker_type": "naive",
    }

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回提交阶段来源。"""
            assert file_id == "imp_1"
            return record

        def update_import_parse_job_progress(self, job_id, **fields):
            """记录 provider locator 并模拟释放 lease。"""
            calls.append(("progress", job_id, fields))
            return _admin_parse_job(
                id=job_id,
                status="polling",
                input_fingerprint=fingerprint,
                provider_batch_id=fields["provider_batch_id"],
                provider_file_name=fields["provider_file_name"],
                progress=fields["progress"],
            )

        def fail_import_parse_job(self, *args, **kwargs):
            """本测试不应进入失败路径。"""
            raise AssertionError((args, kwargs))

    class FakeMineruClient:
        def __init__(self, *args, **kwargs):
            pass

        def start_file(self, path):
            """返回后续轮询所需的 provider locator。"""
            assert path == source
            calls.append(("start", path))
            return SimpleNamespace(
                batch_id="batch_1",
                file_name="manual.pdf",
                state="waiting-file",
                progress={},
            )

    monkeypatch.setattr("cyclops.admin_server.MineruClient", FakeMineruClient)
    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=tmp_path,
            mineru_api_token="token",
            mineru_parse_timeout_seconds=30,
            mineru_use_kb_packager=True,
            import_parse_worker_poll_interval_seconds=2,
        ),
        db=FakeDatabase(),
    )
    fingerprint = app._import_parse_input_fingerprint(
        record,
        source,
        chunker_type="naive",
    )

    result = app.process_import_parse_job(
        _admin_parse_job(
            status="submitting",
            input_fingerprint=fingerprint,
            lease_token="lease-current",
        )
    )

    assert result["status"] == "polling"
    assert [call[0] for call in calls] == ["start", "progress"]
    assert calls[1][2]["provider_batch_id"] == "batch_1"
    assert calls[1][2]["provider_file_name"] == "manual.pdf"


def test_admin_worker_submitting_markdown_completes_without_provider(
    tmp_path,
    monkeypatch,
):
    """Markdown 在 worker 内本地解析，并从 submitting 直接原子完成。"""
    source = tmp_path / "chat.md"
    source.write_text(
        "\n".join(
            [
                "- [2025-08-25 16:20] 用户: 报告没生成怎么办",
                "- [2025-08-25 16:21] 客服: 隔10分钟刷新",
            ]
        ),
        encoding="utf-8",
    )
    captured = {}
    record = {
        "id": "imp_1",
        "original_name": "chat.md",
        "stored_path": str(source),
        "parser": "markdown_chat",
        "chunker_type": "naive",
    }

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回本地 Markdown 来源。"""
            return record

        def complete_import_parse_job(self, job_id, **fields):
            """记录本地解析的原子完成 payload。"""
            captured.update({"job_id": job_id, **fields})
            return _admin_parse_job(id=job_id, status="completed")

        def fail_import_parse_job(self, *args, **kwargs):
            """本测试不应进入失败路径。"""
            raise AssertionError((args, kwargs))

    class ForbiddenMineruClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Markdown must not call MinerU")

    monkeypatch.setattr("cyclops.admin_server.MineruClient", ForbiddenMineruClient)
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )
    fingerprint = app._import_parse_input_fingerprint(
        record,
        source,
        chunker_type="naive",
    )

    result = app.process_import_parse_job(
        _admin_parse_job(
            status="submitting",
            input_fingerprint=fingerprint,
            lease_token="lease-current",
        )
    )

    assert result["status"] == "completed"
    assert captured["job_id"] == "parse_job_1"
    assert captured["chunks"]
    assert sum(item["message_count"] for item in captured["chunks"]) == 2
    assert captured["progress"] == {"state": "completed", "percent": 100}


def test_admin_worker_changed_input_fails_without_provider_or_completion(
    tmp_path,
    monkeypatch,
):
    """来源指纹变化时任务失败，旧切片保持不动且不调用 provider。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"old")
    calls = []
    record = {
        "id": "imp_1",
        "original_name": "manual.pdf",
        "stored_path": str(source),
        "parser": "mineru",
        "chunker_type": "naive",
    }

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回当前来源 locator。"""
            return record

        def fail_import_parse_job(self, job_id, **fields):
            """记录 fingerprint 冲突的失败终态。"""
            calls.append(("fail", job_id, fields))
            return _admin_parse_job(id=job_id, status="failed", error=fields["error"])

        def complete_import_parse_job(self, *args, **kwargs):
            """来源变化后不得触碰旧 snapshot。"""
            raise AssertionError((args, kwargs))

    class ForbiddenMineruClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("changed input must fail before provider")

    monkeypatch.setattr("cyclops.admin_server.MineruClient", ForbiddenMineruClient)
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )
    old_fingerprint = app._import_parse_input_fingerprint(
        record,
        source,
        chunker_type="naive",
    )
    source.write_bytes(b"changed")

    result = app.process_import_parse_job(
        _admin_parse_job(
            status="submitting",
            input_fingerprint=old_fingerprint,
            lease_token="lease-current",
        )
    )

    assert result["status"] == "failed"
    assert calls[0][0:2] == ("fail", "parse_job_1")
    assert "fingerprint" in calls[0][2]["error"]


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("mineru_use_kb_packager", False),
        ("document_chunk_token_num", 256),
        ("document_chunk_delimiter", "\n"),
        ("document_chunk_overlap_percent", 10),
        ("document_children_delimiter", "##"),
        ("document_table_context_size", 2),
        ("document_image_context_size", 3),
    ],
)
def test_parse_fingerprint_covers_every_mineru_output_setting(
    tmp_path,
    field,
    changed_value,
):
    """任务期间任何 MinerU 输出配置变化都必须让旧任务指纹失效。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    record = {
        "id": "imp_1",
        "original_name": "manual.pdf",
        "stored_path": str(source),
        "parser": "mineru",
        "chunker_type": "naive",
    }
    output_settings = {
        "mineru_use_kb_packager": True,
        "document_chunk_token_num": 512,
        "document_chunk_delimiter": "\n。；！？",
        "document_chunk_overlap_percent": 0,
        "document_children_delimiter": "",
        "document_table_context_size": 0,
        "document_image_context_size": 0,
    }
    baseline_app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", **output_settings)
    )
    changed_app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            **{**output_settings, field: changed_value},
        )
    )

    baseline = baseline_app._import_parse_input_fingerprint(
        record,
        source,
        chunker_type="naive",
    )
    changed = changed_app._import_parse_input_fingerprint(
        record,
        source,
        chunker_type="naive",
    )

    assert changed != baseline


def test_admin_parse_getters_are_provider_free_and_strip_lease(tmp_path, monkeypatch):
    """任务和文件 GET 只读数据库，响应不得暴露 worker lease。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    job = _admin_parse_job(
        status="polling",
        progress={"extracted_pages": 1, "total_pages": 4},
        lease_token="secret-lease",
    )

    class FakeDatabase:
        def get_import_parse_job(self, job_id):
            """返回持久任务。"""
            assert job_id == "parse_job_1"
            return job

        def get_import_file(self, file_id):
            """返回文件记录。"""
            return {"id": file_id, "stored_path": str(source), "status": "processing"}

        def get_latest_import_parse_job_for_file(self, file_id):
            """文件详情读取同一任务。"""
            return job

        def get_import_file_embedding_summary(self, file_id):
            """返回当前向量摘要。"""
            return {"status": "none", "total_chunks": 0, "ready_count": 0}

    class ForbiddenMineruClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("GET must not instantiate MinerU")

    monkeypatch.setattr("cyclops.admin_server.MineruClient", ForbiddenMineruClient)
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
    )

    job_payload = app.get_import_parse_job("parse_job_1")
    file_payload = app.get_import_file("imp_1")

    assert job_payload["percent"] == 25
    assert "lease_token" not in job_payload
    assert file_payload["parse_job"] == job_payload


def test_admin_app_delete_import_file_removes_record_and_local_upload(tmp_path):
    """删除文档时同时清理本地原件，并把要展示给用户的提示文案数组直接组装好返回，
    让前端无需关心业务字段，只按 messages 数组逐条提示。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []

    class FakeDatabase:
        def delete_import_file(self, file_id):
            calls.append(("delete", file_id))
            return {
                "id": file_id,
                "stored_path": str(source),
                "original_name": "manual.pdf",
                "_deleted_chunk_count": 7,
                "_deleted_vector_count": 12,
            }

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    result = app.delete_import_file("imp_1")

    assert result == {
        "deleted": True,
        "id": "imp_1",
        "messages": [
            "已删除文件原件",
            "已清理文档切片 7 个",
            "已清理向量索引 12 条",
        ],
    }
    assert calls == [("delete", "imp_1")]
    assert not source.exists()


def test_admin_app_delete_import_file_skips_zero_count_messages(tmp_path):
    """文档没有切片或向量时，对应提示就不应该出现在 messages 数组里 — 由后端决定要发哪几条。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")

    class FakeDatabase:
        def delete_import_file(self, file_id):
            return {
                "id": file_id,
                "stored_path": str(source),
                "_deleted_chunk_count": 0,
                "_deleted_vector_count": 0,
            }

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    result = app.delete_import_file("imp_1")

    assert result["messages"] == ["已删除文件原件"]


def test_admin_app_save_import_candidate_keeps_embedding_as_independent_step():
    """候选保存只写 needs_review FAQ，向量必须由后续显式操作独立生成。"""
    calls = []

    class FakeDatabase:
        def get_import_candidate(self, candidate_id):
            """返回待保存候选，聚焦正文保存与向量步骤边界。"""
            assert candidate_id == "cand_1"
            return {
                "id": "cand_1",
                "question": "报告没生成怎么办？",
                "answer": "建议隔 10 分钟刷新查看进度。",
                "similar_questions": ["团体报告下载不了怎么办？"],
                "category": "报告服务",
                "tags": ["报告"],
                "confidence": "medium",
                "source_excerpt": "客服: 隔10分钟刷新",
                "file_name": "chat.md",
                "chunk_id": "chunk_1",
            }

        def save_faq_text(self, row):
            """保存候选正文并保持 pending，禁止在当前操作中生成向量。"""
            calls.append(("faq", row))
            return {
                **row,
                "embedding_status": "pending",
                "embedding_error": None,
                "content_hash": compute_content_hash(row),
            }

        def prepare_faq_embedding(self, faq_id):
            """候选保存若触发向量准备就直接让测试失败。"""
            raise AssertionError(f"candidate save must not prepare embedding: {faq_id}")

        def mark_import_candidate_saved(self, candidate_id, faq_id):
            """记录候选与正式 FAQ 的唯一关联。"""
            calls.append(("saved", candidate_id, faq_id))
            return {"id": candidate_id, "status": "saved", "saved_faq_id": faq_id}

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
    )

    result = app.save_import_candidate("cand_1")

    assert calls[0][1]["status"] == "needs_review"
    assert calls[0][1]["embedding_text"].startswith("标准问题")
    assert [call[0] for call in calls] == ["faq", "saved"]
    assert result["status"] == "saved"
    assert result["embedding_status"] == "pending"


def test_admin_app_embed_faq_rejects_changed_source_without_marking_current_row_failed():
    """FAQ 在向量生成期间变化时应显式冲突，不得把新正文错误标成 failed。"""
    calls = []

    class FakeDatabase:
        def prepare_faq_embedding(self, faq_id):
            """返回向量生成前的固定 FAQ 正文快照。"""
            calls.append(("prepare", faq_id))
            return {
                "id": faq_id,
                "embedding_text": "旧正文",
                "content_hash": "hash-before-embedding",
            }

        def update_faq_embedding(
            self,
            faq_id,
            vector,
            *,
            embedding_model,
            embedding_dimensions,
            expected_content_hash,
        ):
            """模拟正文并发变化导致的 guarded 更新冲突。"""
            calls.append((faq_id, vector, expected_content_hash))
            raise ValueError("FAQ changed or was deleted during embedding")

        def mark_embedding_failed(self, faq_id, error, *, expected_content_hash):
            """确保冲突不会污染当前 FAQ 的失败状态。"""
            raise AssertionError(f"current FAQ must not be marked failed: {faq_id} {error}")

    class FakeEmbedding:
        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """返回旧正文对应的固定测试向量。"""
            assert text == "旧正文"
            return [0.1, 0.2]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    with pytest.raises(AdminValidationError, match="changed|变化"):
        app.embed_faq("faq_1")

    assert calls == [
        ("prepare", "faq_1"),
        ("faq_1", [0.1, 0.2], "hash-before-embedding"),
    ]


def test_admin_app_embed_faq_propagates_persistence_failure_without_marking_failed():
    """向量已生成但持久化失败时必须原样抛错，不能补偿写成 provider failed。"""
    calls = []

    class FakeDatabase:
        """模拟准备成功、统一投影持久化失败的数据库边界。"""

        def prepare_faq_embedding(self, faq_id):
            """返回本次 embedding 的固定内容快照。"""
            calls.append(("prepare", faq_id))
            return {
                "id": faq_id,
                "embedding_text": "当前正文",
                "content_hash": "hash-current",
            }

        def update_faq_embedding(
            self,
            faq_id,
            vector,
            *,
            embedding_model,
            embedding_dimensions,
            expected_content_hash,
        ):
            """模拟 FAQ 与统一投影事务无法持久化。"""
            calls.append(
                (
                    "complete",
                    faq_id,
                    vector,
                    embedding_model,
                    embedding_dimensions,
                    expected_content_hash,
                )
            )
            raise RuntimeError("projection write failed")

        def mark_embedding_failed(self, faq_id, error, *, expected_content_hash):
            """若持久化异常被误当作 provider 失败，立即让测试失败。"""
            raise AssertionError(
                f"persistence failure must propagate: {faq_id} {error} {expected_content_hash}"
            )

    class FakeEmbedding:
        """返回成功向量，确保故障发生在持久化阶段。"""

        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """记录输入并返回固定向量。"""
            calls.append(("provider", text))
            return [0.1, 0.2]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    with pytest.raises(RuntimeError, match="projection write failed"):
        app.embed_faq("faq_1")

    assert calls == [
        ("prepare", "faq_1"),
        ("provider", "当前正文"),
        (
            "complete",
            "faq_1",
            [0.1, 0.2],
            "embedding-current",
            2,
            "hash-current",
        ),
    ]


def test_admin_app_embed_faq_marks_provider_failure_with_prepared_hash():
    """FAQ provider 失败必须用准备阶段指纹做 CAS 标记，不能污染并发新正文。"""
    calls = []

    class FakeDatabase:
        def prepare_faq_embedding(self, faq_id):
            """返回固定内容快照与本次 CAS 指纹。"""
            calls.append(("prepare", faq_id))
            return {
                "id": faq_id,
                "embedding_text": "当前正文",
                "content_hash": "hash-current",
            }

        def mark_embedding_failed(self, faq_id, error, *, expected_content_hash):
            """记录 provider 失败及其绑定的内容指纹。"""
            calls.append(("failed", faq_id, error, expected_content_hash))
            return {"id": faq_id, "embedding_status": "failed", "embedding_error": error}

    class FailingEmbedding:
        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """模拟外部 embedding provider 超时。"""
            calls.append(("provider", text))
            raise RuntimeError("provider timeout")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FailingEmbedding(),
    )

    result = app.embed_faq("faq_1")

    assert result["embedding_status"] == "failed"
    assert calls == [
        ("prepare", "faq_1"),
        ("provider", "当前正文"),
        ("failed", "faq_1", "provider timeout", "hash-current"),
    ]


def test_admin_app_embed_faq_maps_failure_mark_cas_conflict():
    """provider 失败后的 mark CAS 冲突必须显式返回正文变化错误。"""

    class FakeDatabase:
        def prepare_faq_embedding(self, faq_id):
            """返回随后会失效的内容快照。"""
            return {
                "id": faq_id,
                "embedding_text": "旧正文",
                "content_hash": "hash-old",
            }

        def mark_embedding_failed(self, faq_id, error, *, expected_content_hash):
            """模拟 provider 期间 FAQ 正文已被并发修改。"""
            raise ValueError(
                f"FAQ changed during failed embedding: {faq_id} {error} {expected_content_hash}"
            )

    class FailingEmbedding:
        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """模拟旧正文的 provider 请求失败。"""
            raise RuntimeError(f"provider timeout: {text}")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FailingEmbedding(),
    )

    with pytest.raises(AdminValidationError, match="changed"):
        app.embed_faq("faq_1")


def test_admin_app_embed_import_file_requires_parsed_document():
    """文档切片生成 embedding 只能在解析完成后触发。"""

    class FakeDatabase:
        def get_import_file(self, file_id):
            assert file_id == "imp_1"
            return {
                "id": "imp_1",
                "original_name": "manual.pdf",
                "status": "pending",
                "chunk_count": 0,
            }

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    with pytest.raises(AdminValidationError, match="parsed"):
        app.embed_import_file("imp_1")


def test_admin_app_embed_import_file_rejects_disabled_file_before_provider():
    """禁用文件必须在读取切片和调用 embedding provider 前明确拒绝。"""

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回已禁用但解析完成的文件，验证禁用门禁优先于后续流程。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "status": "needs_review",
                "is_disabled": True,
            }

        def list_import_chunks(self, file_id):
            """禁用文件不应继续读取切片。"""
            pytest.fail(f"disabled file must not list chunks: {file_id}")

    class ForbiddenEmbedding:
        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """禁用来源不得消耗 provider 调用。"""
            pytest.fail(f"disabled file must not be embedded: {text}")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=ForbiddenEmbedding(),
    )

    with pytest.raises(AdminValidationError, match="disabled"):
        app.embed_import_file("imp_1")


@pytest.mark.parametrize(
    ("chunk_disabled", "file_disabled"),
    [(True, False), (False, True)],
    ids=["disabled-chunk", "disabled-file"],
)
def test_admin_app_embed_import_chunk_rejects_disabled_source_before_provider(
    chunk_disabled,
    file_disabled,
):
    """单切片入口必须在 provider 前拒绝任一层禁用状态。"""

    class FakeDatabase:
        def get_import_chunk(self, chunk_id):
            """返回测试指定禁用状态的来源切片。"""
            return {
                "id": chunk_id,
                "file_id": "imp_1",
                "chunk_index": 1,
                "source_text": "正文",
                "source_blocks": [],
                "children_delimiter": "",
                "is_disabled": chunk_disabled,
            }

        def get_import_file(self, file_id):
            """返回测试指定禁用状态的来源文件。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "status": "needs_review",
                "is_disabled": file_disabled,
            }

        def replace_document_chunk_embeddings(self, **_payload):
            """禁用来源不得进入数据库批次提交。"""
            pytest.fail("disabled source must not commit document embeddings")

    class ForbiddenEmbedding:
        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """禁用来源不得消耗 provider 调用。"""
            pytest.fail(f"disabled source must not be embedded: {text}")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=ForbiddenEmbedding(),
    )

    with pytest.raises(AdminValidationError, match="disabled"):
        app.embed_import_chunk("chunk_1")


def test_admin_app_embed_import_chunk_uses_one_guarded_batch_commit():
    """单切片向量必须先完成全部 provider 调用，再通过唯一 DB 批次入口原子提交。"""
    from cyclops.db import builders as builders_module

    assert hasattr(builders_module, "document_embedding_source_fingerprint")
    calls = []
    import_file = {
        "id": "imp_1",
        "original_name": "manual.pdf",
        "file_type": "pdf",
        "parser": "mineru",
        "chunker_type": "naive",
        "status": "needs_review",
        "is_disabled": False,
    }
    source_chunk = {
        "id": "chunk_1",
        "file_id": "imp_1",
        "chunk_index": 1,
        "source_text": "报告导出失败时先检查权限。",
        "keywords": ["报告"],
        "status": "generated",
        "message_count": 1,
        "start_at": None,
        "end_at": None,
        "source_blocks": [],
        "children_delimiter": "",
        "questions": [],
        "is_disabled": False,
    }

    class FakeDatabase:
        """记录专用批次提交，若调用通用 upsert 则立即让测试失败。"""

        def get_import_chunk(self, chunk_id):
            """记录并返回待向量化切片快照。"""
            calls.append(("chunk", chunk_id))
            return dict(source_chunk)

        def get_import_file(self, file_id):
            """记录并返回切片所属文件快照。"""
            calls.append(("file", file_id))
            return dict(import_file)

        def replace_document_chunk_embeddings(self, **payload):
            """记录 guarded 批次提交并返回 ready 行。"""
            calls.append(("commit", payload))
            return [
                {**row, "embedding_status": "ready"}
                for row, _vector in payload["items"]
            ]

        def get_import_file_embedding_summary(self, file_id):
            """记录并返回文件向量状态摘要。"""
            calls.append(("summary", file_id))
            return {"status": "ready", "total_chunks": 2, "ready_count": 2}

    class FakeEmbedding:
        """记录所有外部向量调用，验证提交发生在完整批次之后。"""

        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """记录 provider 调用并返回固定向量。"""
            calls.append(("embed", text))
            return [0.1, 0.2]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    result = app.embed_import_chunk("chunk_1")

    commit_index, commit_payload = next(
        (index, call[1])
        for index, call in enumerate(calls)
        if call[0] == "commit"
    )
    embed_indexes = [index for index, call in enumerate(calls) if call[0] == "embed"]
    assert embed_indexes
    assert max(embed_indexes) < commit_index
    assert commit_payload["file_id"] == "imp_1"
    assert commit_payload["chunk_id"] == "chunk_1"
    assert commit_payload["source_fingerprint"] == (
        builders_module.document_embedding_source_fingerprint(import_file, source_chunk)
    )
    assert len(commit_payload["items"]) == 2
    assert [row["chunk_level"] for row, _vector in commit_payload["items"]] == [
        "parent",
        "child",
    ]
    assert result["count"] == 2


def test_admin_app_embed_import_chunk_does_not_commit_partial_provider_batch():
    """文档 provider 在 child 中途失败时，不得提交已生成的 parent 向量。"""
    calls = []

    class FakeDatabase:
        def get_import_chunk(self, chunk_id):
            """返回会生成 parent 与一个 child 的有效切片。"""
            return {
                "id": chunk_id,
                "file_id": "imp_1",
                "chunk_index": 1,
                "source_text": "正文",
                "source_blocks": [],
                "children_delimiter": "",
                "is_disabled": False,
            }

        def get_import_file(self, file_id):
            """返回可生成向量的有效文件。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "status": "needs_review",
                "is_disabled": False,
            }

        def replace_document_chunk_embeddings(self, **_payload):
            """provider 未完成全部行时禁止进入提交。"""
            pytest.fail("partial provider batch must not be committed")

    class FailingSecondEmbedding:
        model = "embedding-current"
        dimensions = 2

        def embed(self, text):
            """首条成功、第二条失败，模拟 child provider 中断。"""
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError("child embedding failed")
            return [0.1, 0.2]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FailingSecondEmbedding(),
    )

    with pytest.raises(RuntimeError, match="child embedding failed"):
        app.embed_import_chunk("chunk_1")

    assert len(calls) == 2


def test_admin_app_embed_import_chunk_maps_database_cas_conflict():
    """文档 provider 完成后来源变化时，DB CAS 冲突必须映射为管理端校验错误。"""

    class FakeDatabase:
        def get_import_chunk(self, chunk_id):
            """返回 provider 调用前的来源切片快照。"""
            return {
                "id": chunk_id,
                "file_id": "imp_1",
                "chunk_index": 1,
                "source_text": "旧正文",
                "source_blocks": [],
                "children_delimiter": "",
                "is_disabled": False,
            }

        def get_import_file(self, file_id):
            """返回 provider 调用前的来源文件快照。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "status": "needs_review",
                "is_disabled": False,
            }

        def replace_document_chunk_embeddings(self, **_payload):
            """模拟 provider 期间来源正文已变化。"""
            raise ValueError("document embedding source changed or became unavailable")

    class FakeEmbedding:
        model = "embedding-current"
        dimensions = 2

        def embed(self, _text):
            """返回固定向量，让故障发生在数据库 CAS 阶段。"""
            return [0.1, 0.2]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    with pytest.raises(AdminValidationError, match="changed|unavailable"):
        app.embed_import_chunk("chunk_1")


def test_generate_import_file_questions_preserves_questions_during_failed_refresh(monkeypatch):
    """强制重算失败时，pending/failed 只更新状态，不得先清空已有假设问题。"""
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回问题刷新使用的固定导入文件。"""
            return {"id": file_id, "original_name": "manual.pdf"}

        def list_import_chunks(self, file_id):
            """返回带已有问题的固定切片列表。"""
            return [
                {
                    "id": "chunk_1",
                    "file_id": file_id,
                    "source_text": "正文",
                    "questions": ["已有问题"],
                    "questions_status": "ready",
                    "section_path": [],
                }
            ]

        def set_import_chunk_questions(
            self,
            chunk_id,
            questions,
            *,
            model,
            status="ready",
            error=None,
        ):
            """记录问题状态更新且保留传入问题。"""
            calls.append((chunk_id, questions, model, status, error))
            return {"id": chunk_id, "questions": questions, "questions_status": status}

    class FailingQuestionAssistant:
        model = "question-model"

        def __init__(self, chat):
            """校验问题助手复用指定聊天客户端。"""
            assert chat is fake_chat

        def generate_questions(self, **_kwargs):
            """模拟问题生成 provider 失败。"""
            raise ImportQuestionError("provider timeout")

    fake_chat = object()
    monkeypatch.setattr(
        "cyclops.admin_server.ImportQuestionAssistant",
        FailingQuestionAssistant,
    )
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        chat=fake_chat,
    )

    result = app.generate_import_file_questions("imp_1", {"force": True})

    assert result["failed"] == 1
    assert calls == [
        ("chunk_1", ["已有问题"], "question-model", "pending", None),
        ("chunk_1", ["已有问题"], "question-model", "failed", "provider timeout"),
    ]


def test_admin_app_embed_import_file_writes_document_chunks_to_knowledge_chunks():
    """解析完成的文档可以把每个切片写入统一知识单元并生成向量。"""
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            calls.append(("file", file_id))
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "file_type": "pdf",
                "parser": "mineru",
                "status": "needs_review",
                "chunk_count": 2,
            }

        def list_import_chunks(self, file_id):
            calls.append(("chunks", file_id))
            return [
                {
                    "id": "chunk_1",
                    "file_id": file_id,
                    "chunk_index": 1,
                    "source_text": "第一段原文",
                    "keywords": ["登录"],
                    "status": "generated",
                    "message_count": 0,
                    "start_at": None,
                    "end_at": None,
                },
                {
                    "id": "chunk_2",
                    "file_id": file_id,
                    "chunk_index": 2,
                    "source_text": "第二段原文",
                    "keywords": ["报告"],
                    "status": "generated",
                    "message_count": 0,
                    "start_at": None,
                    "end_at": None,
                },
            ]

        def replace_document_chunk_embeddings(
            self,
            *,
            file_id,
            chunk_id,
            source_fingerprint,
            items,
            embedding_model,
            embedding_dimensions,
        ):
            """记录每个原子批次中的行，同时保留原测试对内容和向量的断言。"""
            assert file_id == "imp_1"
            assert chunk_id in {"chunk_1", "chunk_2"}
            assert source_fingerprint
            saved = []
            for row, vector in items:
                calls.append(
                    ("knowledge_chunk", row, vector, embedding_model, embedding_dimensions)
                )
                saved.append({**row, "embedding_status": "ready"})
            return saved

        def get_import_file_embedding_summary(self, file_id):
            """记录并返回全文件向量摘要。"""
            calls.append(("summary", file_id))
            return {"status": "ready", "total_chunks": 2, "ready_count": 4}

    class FakeEmbedding:
        model = "fake-embedding"
        dimensions = 3

        def embed(self, text):
            calls.append(("embed", text))
            return [0.1, 0.2, 0.3]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    result = app.embed_import_file("imp_1")

    assert result["count"] == 4
    assert calls[0] == ("file", "imp_1")
    assert calls[1] == ("chunks", "imp_1")
    embed_inputs = [call[1] for call in calls if call[0] == "embed"]
    assert any("文件：manual.pdf" in text and "正文：第一段原文" in text for text in embed_inputs)
    chunk_calls = [call for call in calls if call[0] == "knowledge_chunk"]
    assert chunk_calls[0][1]["source_type"] == "document"
    assert chunk_calls[0][1]["source_id"] == "imp_1"
    assert chunk_calls[0][1]["source_chunk_id"] == "chunk_1"
    assert chunk_calls[0][1]["chunk_level"] == "parent"
    assert chunk_calls[0][1]["status"] == "usable"
    assert chunk_calls[0][2] == [0.1, 0.2, 0.3]


@pytest.mark.parametrize(
    ("chunk_overrides", "expected_child_content"),
    [
        ({"source_blocks": [], "children_delimiter": ""}, "完整来源正文"),
        (
            {
                "source_text": "父级汇总正文",
                "source_blocks": [{"text": "唯一结构块正文", "block_type": "text"}],
                "children_delimiter": "",
            },
            "唯一结构块正文",
        ),
        ({"source_blocks": [], "children_delimiter": r"\n"}, "完整来源正文"),
    ],
    ids=["no-structured-block", "single-structured-block", "single-delimiter-segment"],
)
def test_document_rows_create_direct_child_for_unsplit_sources(
    chunk_overrides,
    expected_child_content,
):
    """零块、单块或单段切片都必须生成一个使用原始来源身份的 direct child。"""
    chunk = {
        "id": "chunk_1",
        "file_id": "imp_1",
        "chunk_index": 1,
        "source_text": "完整来源正文",
        "keywords": ["来源"],
        "status": "generated",
        "message_count": 0,
        "start_at": None,
        "end_at": None,
        **chunk_overrides,
    }
    import_file = {
        "id": "imp_1",
        "original_name": "manual.pdf",
        "file_type": "pdf",
        "parser": "mineru",
    }

    rows = document_knowledge_rows_for_embedding(chunk, import_file)

    assert [row["chunk_level"] for row in rows] == ["parent", "child"]
    assert [row["id"] for row in rows] == [
        "kc_document_chunk_1",
        "kc_document_chunk_1_child_1",
    ]
    assert [row["source_chunk_id"] for row in rows] == ["chunk_1", "chunk_1"]
    assert [row["metadata"]["chunk_id"] for row in rows] == ["chunk_1", "chunk_1"]
    assert rows[1]["parent_chunk_id"] == rows[0]["id"]
    assert rows[1]["content"] == expected_child_content


def test_admin_app_embed_import_file_derives_child_chunks_from_structured_blocks():
    """一个审核块包含多个解析块时，应写入 parent 和 child 知识单元支持精准召回。"""
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "file_type": "pdf",
                "parser": "mineru",
                "status": "needs_review",
                "chunk_count": 1,
            }

        def list_import_chunks(self, file_id):
            return [
                {
                    "id": "chunk_1",
                    "file_id": file_id,
                    "chunk_index": 1,
                    "source_text": "这是审核用 parent 文本，不依赖渲染分隔符派生 child。",
                    "keywords": ["登录"],
                    "status": "generated",
                    "message_count": 2,
                    "start_at": None,
                    "end_at": None,
                    "section_path": ["登录"],
                    "page_start": 1,
                    "page_end": 2,
                    "block_type": "mixed",
                    "source_offsets": {},
                    "source_blocks": [
                        {
                            "text": "第一段",
                            "block_type": "text",
                            "page_number": 1,
                            "section_title": "登录",
                            "evidence": {"page_number": 1, "block_type": "text"},
                        },
                        {
                            "text": "第二段",
                            "block_type": "text",
                            "page_number": 2,
                            "section_title": "登录",
                            "evidence": {"page_number": 2, "block_type": "text"},
                        },
                    ],
                }
            ]

        def replace_document_chunk_embeddings(self, **payload):
            """展开专用批次，测试继续检查结构化 parent/child 内容。"""
            rows = [row for row, _vector in payload["items"]]
            calls.extend(rows)
            return [{**row, "embedding_status": "ready"} for row in rows]

        def get_import_file_embedding_summary(self, file_id):
            return {"status": "ready", "total_chunks": 1, "ready_count": 3}

    class FakeEmbedding:
        model = "fake-embedding"
        dimensions = 3

        def embed(self, text):
            return [0.1, 0.2, 0.3]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    result = app.embed_import_file("imp_1")

    assert result["count"] == 3
    assert [row["chunk_level"] for row in calls] == ["parent", "child", "child"]
    assert calls[1]["parent_chunk_id"] == calls[0]["id"]
    assert calls[2]["parent_chunk_id"] == calls[0]["id"]
    assert [row["source_chunk_id"] for row in calls] == ["chunk_1", "chunk_1", "chunk_1"]
    assert [row["metadata"]["chunk_id"] for row in calls] == ["chunk_1", "chunk_1", "chunk_1"]
    assert [row["id"] for row in calls] == [
        "kc_document_chunk_1",
        "kc_document_chunk_1_child_1",
        "kc_document_chunk_1_child_2",
    ]
    assert calls[1]["chunk_index"] < 0
    assert calls[2]["chunk_index"] < 0
    assert len({row["chunk_index"] for row in calls}) == 3
    assert "第一段" in calls[1]["content"]
    assert "第二段" in calls[2]["content"]


def test_admin_app_embed_import_file_child_indexes_do_not_overlap_parent_indexes():
    """child 知识单元应使用不和 parent 正常切片编号冲突的 chunk_index。"""
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "file_type": "pdf",
                "parser": "mineru",
                "status": "needs_review",
                "chunk_count": 2,
            }

        def list_import_chunks(self, file_id):
            return [
                {
                    "id": "chunk_1",
                    "file_id": file_id,
                    "chunk_index": 1,
                    "source_text": "父块一",
                    "keywords": [],
                    "status": "generated",
                    "message_count": 2,
                    "start_at": None,
                    "end_at": None,
                    "source_blocks": [
                        {"text": "子块一", "block_type": "text"},
                        {"text": "子块二", "block_type": "text"},
                    ],
                },
                {
                    "id": "chunk_1001",
                    "file_id": file_id,
                    "chunk_index": 1001,
                    "source_text": "真实父块 1001",
                    "keywords": [],
                    "status": "generated",
                    "message_count": 1,
                    "start_at": None,
                    "end_at": None,
                    "source_blocks": [{"text": "真实父块 1001", "block_type": "text"}],
                },
            ]

        def replace_document_chunk_embeddings(self, **payload):
            """展开专用批次，测试继续检查全文件 chunk_index 唯一性。"""
            rows = [row for row, _vector in payload["items"]]
            calls.extend(rows)
            return [{**row, "embedding_status": "ready"} for row in rows]

        def get_import_file_embedding_summary(self, file_id):
            """返回 child 索引测试使用的向量摘要。"""
            return {"status": "ready", "total_chunks": 2, "ready_count": 5}

    class FakeEmbedding:
        model = "fake-embedding"
        dimensions = 3

        def embed(self, text):
            return [0.1, 0.2, 0.3]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    app.embed_import_file("imp_1")

    indexes = [row["chunk_index"] for row in calls]
    assert 1001 in indexes
    assert all(row["chunk_index"] < 0 for row in calls if row["chunk_level"] == "child")
    assert len(indexes) == len(set(indexes))


def test_admin_app_embed_import_file_prefers_structured_blocks_over_delimiter():
    """结构化块与 delimiter 同时存在时，必须优先按解析器结构生成 child。"""
    calls = []

    class FakeDatabase:
        def get_import_file(self, file_id):
            """返回结构块优先测试使用的固定文件。"""
            return {
                "id": file_id,
                "original_name": "manual.pdf",
                "file_type": "pdf",
                "parser": "mineru",
                "status": "needs_review",
                "chunk_count": 1,
            }

        def list_import_chunks(self, file_id):
            """返回同时含结构块和 delimiter 的切片。"""
            return [
                {
                    "id": "chunk_1",
                    "file_id": file_id,
                    "chunk_index": 1,
                    "source_text": "第一问\n第二问",
                    "children_delimiter": r"\n",
                    "keywords": ["问答"],
                    "status": "generated",
                    "message_count": 1,
                    "start_at": None,
                    "end_at": None,
                    "section_path": ["FAQ"],
                    "page_start": 1,
                    "page_end": 1,
                    "block_type": "text",
                    "source_offsets": {},
                    "source_blocks": [
                        {
                            "text": "解析器结构块正文",
                            "block_type": "text",
                            "page_number": 1,
                            "section_title": "FAQ",
                            "evidence": {"page_number": 1, "block_type": "text"},
                        }
                    ],
                }
            ]

        def replace_document_chunk_embeddings(self, **payload):
            """记录结构化 child 批次，供测试核对来源优先级。"""
            rows = [row for row, _vector in payload["items"]]
            calls.extend(rows)
            return [{**row, "embedding_status": "ready"} for row in rows]

        def get_import_file_embedding_summary(self, file_id):
            """返回结构块 child 生成后的向量摘要。"""
            return {"status": "ready", "total_chunks": 1, "ready_count": 2}

    class FakeEmbedding:
        model = "fake-embedding"
        dimensions = 3

        def embed(self, text):
            """返回结构块测试使用的固定向量。"""
            return [0.1, 0.2, 0.3]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused"),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
    )

    result = app.embed_import_file("imp_1")

    assert result["count"] == 2
    assert [row["content"] for row in calls] == ["第一问\n第二问", "解析器结构块正文"]
    assert [row["source_chunk_id"] for row in calls] == ["chunk_1", "chunk_1"]
    assert [row["metadata"]["chunk_id"] for row in calls] == ["chunk_1", "chunk_1"]
    assert calls[1]["metadata"]["parent_content"] == "第一问\n第二问"


def test_admin_app_update_import_chunk_text_marks_embedding_stale():
    """保存切片原文后返回更新后的切片和文档向量摘要。"""
    calls = []

    class FakeDatabase:
        def update_import_chunk_text(self, chunk_id, source_text):
            calls.append(("update", chunk_id, source_text))
            return {
                "id": chunk_id,
                "file_id": "imp_1",
                "chunk_index": 1,
                "source_text": source_text,
            }

        def get_import_file_embedding_summary(self, file_id):
            calls.append(("summary", file_id))
            return {
                "status": "stale",
                "total_chunks": 2,
                "ready_count": 1,
                "stale_count": 1,
            }

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.update_import_chunk_text("chunk_1", {"source_text": "更新后的切片原文"})

    assert calls == [
        ("update", "chunk_1", "更新后的切片原文"),
        ("summary", "imp_1"),
    ]
    assert result["item"]["source_text"] == "更新后的切片原文"
    assert result["embedding_summary"]["status"] == "stale"


def test_admin_app_update_import_chunk_text_rejects_blank_text():
    """切片正文不能为空，避免保存后破坏后续 embedding 输入。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    with pytest.raises(AdminValidationError, match="source_text"):
        app.update_import_chunk_text("chunk_1", {"source_text": "   "})


def test_admin_app_list_import_file_candidates_delegates_to_database():
    """候选 FAQ 视图按文件汇总候选，不要求用户先进入某个切块。"""
    calls = []

    class FakeDatabase:
        def list_import_file_candidates(self, file_id):
            calls.append(file_id)
            return [{"id": "cand_1", "file_id": file_id, "chunk_index": 3}]

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    assert app.list_import_file_candidates("imp_1") == {
        "items": [{"id": "cand_1", "file_id": "imp_1", "chunk_index": 3}]
    }
    assert calls == ["imp_1"]


def test_static_path_root_returns_react_index():
    """根路径返回 React SPA 入口。老的 /admin.html 已删除，应 404。"""
    import pytest
    from cyclops.admin_server import AdminNotFoundError

    assert static_path("/").name == "index.html"
    assert static_path("/").parent.name == "dist"
    with pytest.raises(AdminNotFoundError):
        static_path("/admin.html")


def test_admin_app_save_faq_keeps_existing_metadata_when_payload_omits_it():
    """前端保存未改动 FAQ 时不能因省略置信度等字段触发 embedding stale。"""
    calls = []

    class FakeDatabase:
        def get_faq(self, faq_id):
            assert faq_id == "faq_1"
            return {
                "id": "faq_1",
                "source_file": "seed.jsonl",
                "source_group": "manual",
                "source_date": None,
                "evidence": [],
                "confidence": "low",
                "sensitivity": None,
            }

        def save_faq_text(self, row):
            calls.append(row)
            return {**row, "embedding_status": "ready"}

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    app.save_faq(
        {
            "id": "faq_1",
            "question": "未完成名单下载后不准怎么办？",
            "answer": "重新下载最新名单后核对。",
            "question_variants": [],
            "category": "测评数据",
            "tags": [],
            "status": "usable",
        }
    )

    assert calls[0]["confidence"] == "low"
    assert calls[0]["source_file"] == "seed.jsonl"


def test_admin_app_create_import_generation_job_requires_chunk_ids():
    """创建生成任务时必须提供切块 id。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    with pytest.raises(AdminValidationError, match="chunk_ids"):
        app.create_import_generation_job({"chunk_ids": []})


def test_admin_app_create_import_generation_job_delegates_to_database():
    """批量生成任务创建只把去重后的切块 id 交给数据库。"""
    calls = []

    class FakeDatabase:
        def create_import_generation_job(self, chunk_ids):
            calls.append(chunk_ids)
            return {"id": "job_1", "items": [{"chunk_id": "chunk_1", "status": "queued"}]}

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.create_import_generation_job({"chunk_ids": ["chunk_1", "chunk_1", "chunk_2"]})

    assert calls == [["chunk_1", "chunk_2"]]
    assert result["id"] == "job_1"


def test_admin_app_iter_import_generation_events_streams_statuses():
    """生成任务事件流应输出处理、完成和最终 done 状态。"""
    calls = []

    class FakeChat:
        def complete(self, system_prompt, user_prompt):
            return '{"candidates":[{"question":"报告没生成怎么办？","answer":"隔10分钟刷新。"}]}'

    class FakeDatabase:
        def get_import_generation_job(self, job_id):
            assert job_id == "job_1"
            return {"id": "job_1", "status": "queued"}

        def list_import_generation_job_items(self, job_id):
            assert job_id == "job_1"
            return [
                {"id": "item_1", "chunk_id": "chunk_1", "status": "queued"},
                {"id": "item_2", "chunk_id": "chunk_2", "status": "skipped", "reason": "already_generated"},
            ]

        def update_import_generation_job_item(self, item_id, **fields):
            calls.append(("item", item_id, fields))
            return {"id": item_id, **fields}

        def update_import_generation_job_summary(self, job_id, status):
            calls.append(("job", job_id, status))
            return {"id": job_id, "status": status}

        def get_import_chunk(self, chunk_id):
            return {
                "id": chunk_id,
                "file_id": "imp_1",
                "source_text": "[2025-08-25 16:20] 用户: 报告没生成怎么办",
            }

        def list_import_dedupe_references(self, chunk_id):
            return []

        def create_import_candidates(self, chunk, rows):
            calls.append(("candidates", chunk["id"], rows))
            return rows

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase(), chat=FakeChat())

    events = list(app.iter_import_generation_events("job_1"))

    assert [event["type"] for event in events] == ["processing", "generated", "skipped", "done"]
    assert events[1]["candidate_count"] == 1
    assert events[2]["reason"] == "already_generated"
    assert calls[-1] == ("job", "job_1", "completed")


def test_admin_app_generate_import_candidates_sets_duplicate_fields():
    """候选生成后需要写入重复程度，供人工审核判断。"""
    calls = []

    class FakeChat:
        def complete(self, system_prompt, user_prompt):
            return '{"candidates":[{"question":"退款多久到账？","answer":"一般1-3个工作日到账。"}]}'

    class FakeDatabase:
        def get_import_chunk(self, chunk_id):
            return {"id": chunk_id, "file_id": "imp_1", "source_text": "退款多久到账"}

        def list_import_dedupe_references(self, chunk_id):
            return [{"id": "faq_1", "question": "退款多久到账", "answer": "一般 1-3 个工作日到账"}]

        def create_import_candidates(self, chunk, rows):
            calls.extend(rows)
            return rows

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase(), chat=FakeChat())

    app.generate_import_candidates("chunk_1")

    assert calls[0]["duplicate_level"] == "high"
    assert calls[0]["duplicate_target_id"] == "faq_1"
    assert calls[0]["duplicate_reason"] == "exact_text"


def test_format_sse_event_outputs_named_json_event():
    """SSE 输出需要包含事件名和 JSON 数据。"""
    content = format_sse_event({"type": "generated", "chunk_id": "chunk_1", "candidate_count": 2})

    assert content.startswith("event: generated\n")
    assert '"chunk_id": "chunk_1"' in content
    assert content.endswith("\n\n")


def test_parse_sse_event_round_trips_named_json_event():
    """测试工具需要能解析后端 SSE 文本，避免前后端事件名不一致。"""
    content = format_sse_event({"type": "delta", "text": "回答片段"})

    parsed = parse_sse_event(content)

    assert parsed == {"event": "delta", "data": {"type": "delta", "text": "回答片段"}}


def test_format_sse_event_contract_covers_assistant_event_types():
    """问答页 SSE 契约应覆盖前端消费的 meta/step/delta/done/error 事件。"""
    events = [
        {"type": "meta", "flow_id": "basic_rag", "stream": True},
        {"type": "step", "step_id": "source_context", "status": "completed"},
        {"type": "delta", "text": "片段"},
        {"type": "done", "answer_draft": "完成", "documents": []},
        {"type": "error", "message": "失败"},
    ]

    parsed = [parse_sse_event(format_sse_event(event)) for event in events]

    assert [item["event"] for item in parsed] == ["meta", "step", "delta", "done", "error"]
    assert [item["data"]["type"] for item in parsed] == ["meta", "step", "delta", "done", "error"]
    assert parsed[1]["data"]["step_id"] == "source_context"
    assert parsed[3]["data"]["documents"] == []


def _admin_hybrid_result(*, use_parent=False, use_kg=False):
    """构造后台入口使用的完整统一检索结果，避免测试手写第二套融合流程。"""
    child = RetrievedKnowledgeChunk(
        id="kc_document_child_1",
        source_type="document",
        source_id="file_1",
        source_chunk_id="chunk_1",
        parent_chunk_id="kc_document_parent_1" if use_parent else None,
        chunk_level="child",
        source_title="平台操作手册",
        section_path=["报告", "导出"],
        page_start=2,
        page_end=2,
        block_type="text",
        source_offsets={},
        content="点击右上角导出。",
        metadata={"category": "报告"},
        tags=["导出"],
        confidence=None,
        status="usable",
        score=0.86,
    )
    parent = RetrievedKnowledgeChunk(
        **{
            **child.__dict__,
            "id": "kc_document_parent_1",
            "parent_chunk_id": None,
            "chunk_level": "parent",
            "content": "进入报告管理，筛选目标报告后点击右上角导出。",
            "score": 1.0,
        }
    )
    candidate = FusedCandidate(
        document=child,
        fused_score=0.1,
        channels=("vector", "keyword"),
        vector_score=0.86,
        keyword_score=0.7,
    )
    return HybridRetrievalResult(
        query="报告没有生成怎么办？",
        query_terms=["报告", "生成"],
        vector_documents=[child],
        keyword_documents=[child],
        candidates=[candidate],
        parent_documents=[parent] if use_parent else [],
        kg_fact_hits=[],
        kg_expanded_candidates=[],
        candidate_limit=6,
        query_embedding_dimensions=3,
        rerank_used=False,
    )


def test_admin_assistant_delegates_to_hybrid_service_with_kg_disabled():
    """后台正式问答必须委托唯一混合服务，并显式关闭 KG。"""
    calls = []

    class FakeRetrieval:
        """记录后台助手调用并返回带 parent 的统一结果。"""

        top_k = 3
        min_score = 0.4
        rerank = None

        def retrieve(self, query, *, include_parent_context, use_kg):
            """记录正式助手检索参数并返回 parent 上下文。"""
            calls.append((query, include_parent_context, use_kg))
            return _admin_hybrid_result(use_parent=True)

    class FakeChat:
        """输出固定回答，测试只关注检索委托与 prompt 上下文。"""

        def __init__(self):
            """初始化助手提示词记录。"""
            self.prompts = []

        def stream_complete(self, system_prompt, user_prompt):
            """记录用户提示并输出固定回答。"""
            self.prompts.append(user_prompt)
            yield "可以导出。"

    chat = FakeChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=SimpleNamespace(),
        chat=chat,
        retrieval=FakeRetrieval(),
    )

    events = list(app.iter_assistant_chat_events({"question": "报告没有生成怎么办？"}))

    assert calls == [("报告没有生成怎么办？", True, False)]
    assert "筛选目标报告" in chat.prompts[0]
    assert events[-1]["type"] == "done"


def test_admin_app_iter_assistant_chat_events_streams_hybrid_retrieval_trace():
    """智能问答应展示意图识别、混合召回和来源融合信息。"""

    class FakeEmbedding:
        def embed(self, text):
            assert text == "报告没有生成怎么办？"
            return [0.1, 0.2, 0.3]

    class FakeDatabase:
        def list_retrieval_aliases(self, status="active"):
            """返回空别名集合，测试只关注混合召回事件。"""
            return []

        def search_knowledge(self, query_embedding, *, top_k, min_score):
            """校验并返回向量召回候选。"""
            assert query_embedding == [0.1, 0.2, 0.3]
            assert top_k == 6
            assert min_score == 0.4
            return _admin_hybrid_result().vector_documents

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            """校验并返回关键词召回候选。"""
            assert query_text == "报告没有生成怎么办？"
            assert top_k == 6
            assert "报告" in query_terms
            return _admin_hybrid_result().keyword_documents

    class FakeChat:
        def __init__(self):
            self.calls = []

        def stream_complete(self, system_prompt, user_prompt):
            self.calls.append((system_prompt, user_prompt))
            yield "请等待 "
            yield "10 分钟后刷新。"

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=FakeChat(),
    )

    events = list(app.iter_assistant_chat_events({"question": "报告没有生成怎么办？"}))

    assert [event["type"] for event in events] == [
        "meta",
        "step",  # input_question
        "step",  # intent_detection
        "step",  # query_embedding
        "step",  # vector_search
        "step",  # keyword_search
        "step",  # hybrid_retrieval
        "step",  # source_context
        "step",  # answer_generation (running)
        "delta",
        "delta",
        "step",  # answer_generation (completed)
        "done",
    ]
    assert events[0]["flow_id"] == "basic_rag"
    assert events[0]["stream"] is True
    assert "intent_detection" in events[0]["available_nodes"]
    assert "intent_detection" in events[0]["enabled_nodes"]
    assert "vector_search" in events[0]["enabled_nodes"]
    assert "keyword_search" in events[0]["enabled_nodes"]
    assert events[2]["step_id"] == "intent_detection"
    assert events[2]["analysis"]["intent"] == "troubleshooting"
    assert events[4]["step_id"] == "vector_search"
    assert events[5]["step_id"] == "keyword_search"
    assert isinstance(events[4]["duration_ms"], int)
    assert isinstance(events[5]["duration_ms"], int)
    assert events[6]["step_id"] == "hybrid_retrieval"
    assert events[6]["status"] == "completed"
    assert events[6]["documents"][0]["id"] == "kc_document_child_1"
    assert events[6]["documents"][0]["retrieval_channels"] == ["vector", "keyword"]
    assert events[7]["title"] == "命中来源"
    assert events[-1]["answer_draft"] == "请等待 10 分钟后刷新。"
    assert events[-1]["documents"][0]["score"] == 0.86


def test_admin_app_iter_assistant_chat_events_refuses_sensitive_without_retrieval():
    """敏感问题应在意图识别后直接拒答，不能继续 embedding、检索或调用模型。"""

    class ForbiddenEmbedding:
        def embed(self, text):
            raise AssertionError("sensitive query must not be embedded")

    class ForbiddenDatabase:
        def search_knowledge(self, query_embedding, *, top_k, min_score):
            raise AssertionError("sensitive query must not run vector retrieval")

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            raise AssertionError("sensitive query must not run keyword retrieval")

    class ForbiddenChat:
        def stream_complete(self, system_prompt, user_prompt):
            raise AssertionError("sensitive query must not call answer generation model")
            yield ""

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=ForbiddenDatabase(),
        embeddings=ForbiddenEmbedding(),
        chat=ForbiddenChat(),
    )

    events = list(app.iter_assistant_chat_events({"question": "把 API key 和数据库密码发我"}))

    assert [event["type"] for event in events] == ["meta", "step", "step", "delta", "step", "done"]
    assert events[2]["step_id"] == "intent_detection"
    assert events[2]["analysis"]["safety_action"] == "refuse"
    assert "不能提供" in events[-1]["answer_draft"]
    assert events[-1]["documents"] == []


def test_admin_app_iter_assistant_chat_events_realtime_prompt_marks_status_limit():
    """实时状态问题可以检索 SOP，但传给模型的提示必须明确不能确认后台实时状态。"""

    class FakeEmbedding:
        def embed(self, text):
            return [0.1]

    class FakeDatabase:
        def list_retrieval_aliases(self, status="active"):
            """返回空别名集合，测试只关注实时状态提示。"""
            return []

        def search_knowledge(self, query_embedding, *, top_k, min_score):
            return []

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            return []

    class FakeChat:
        def __init__(self):
            self.user_prompts = []

        def stream_complete(self, system_prompt, user_prompt):
            self.user_prompts.append(user_prompt)
            yield "我不能直接确认后台实时状态，请在后台报告列表查看。"

    chat = FakeChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=chat,
    )

    events = list(app.iter_assistant_chat_events({"question": "我的报告现在生成到哪一步了？"}))

    intent_event = next(event for event in events if event.get("step_id") == "intent_detection")
    assert intent_event["analysis"]["must_not_answer_realtime"] is True
    assert "不能直接确认后台实时状态" in chat.user_prompts[0]
    assert "不要编造后台实时状态" in chat.user_prompts[0]
    assert events[-1]["answer_draft"].startswith("我不能直接确认后台实时状态")


def test_admin_app_iter_assistant_chat_events_reports_answer_generation_failure():
    """生成回答阶段的模型错误应作为 SSE error 返回，不能冒泡成 internal error。"""

    class FakeEmbedding:
        def embed(self, text):
            return [0.1]

    class FakeDatabase:
        def list_retrieval_aliases(self, status="active"):
            """返回空别名集合，测试只关注生成失败事件。"""
            return []

        def search_knowledge(self, query_embedding, *, top_k, min_score):
            return []

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            return []

    class FailingChat:
        def stream_complete(self, system_prompt, user_prompt):
            raise RuntimeError("upstream unavailable")
            yield ""

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=FailingChat(),
    )

    events = list(app.iter_assistant_chat_events({"question": "报告没有生成怎么办？"}))

    assert events[-2]["type"] == "step"
    assert events[-2]["step_id"] == "answer_generation"
    assert events[-2]["status"] == "failed"
    assert events[-1] == {
        "type": "error",
        "error": "模型服务调用失败：upstream unavailable",
    }


def test_assistant_document_payload_exposes_provenance_fields():
    """来源 payload 应顶层暴露章节、页码和偏移，前端无需猜 metadata 内部结构。"""
    doc = RetrievedKnowledgeChunk(
        id="kc_document_chunk_1",
        source_type="document",
        source_id="file_1",
        source_chunk_id="chunk_1",
        parent_chunk_id=None,
        chunk_level="child",
        source_title="manual.pdf",
        section_path=["报告", "导出"],
        page_start=2,
        page_end=3,
        block_type="table",
        source_offsets={"row": 5},
        content="导出说明",
        metadata={"file_name": "manual.pdf"},
        tags=["导出"],
        confidence=None,
        status="usable",
        score=0.86,
    )

    payload = assistant_document_payload(doc)

    assert payload["section_path"] == ["报告", "导出"]
    assert payload["page_start"] == 2
    assert payload["page_end"] == 3
    assert payload["block_type"] == "table"
    assert payload["source_offsets"] == {"row": 5}


def test_admin_app_iter_assistant_chat_events_expands_child_hits_with_parent_context():
    """智能问答命中文档 child 后，应把 parent 上下文追加给模型回答。"""

    class FakeEmbedding:
        def embed(self, text):
            return [0.1, 0.2, 0.3]

    parent_result = _admin_hybrid_result(use_parent=True)
    child_doc = parent_result.candidates[0].document
    parent_doc = parent_result.parent_documents[0]

    class FakeDatabase:
        def search_knowledge(self, query_embedding, *, top_k, min_score):
            return [child_doc]

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            return []

        def list_retrieval_aliases(self, status="active"):
            return []

        def get_parent_context_chunks(self, child_ids):
            """校验 child ID 并返回对应 parent。"""
            assert child_ids == [child_doc.id]
            return [parent_doc]

    class FakeChat:
        def __init__(self):
            self.user_prompts = []

        def stream_complete(self, system_prompt, user_prompt):
            self.user_prompts.append(user_prompt)
            yield "可以导出。"

        def complete(self, system_prompt, prompt):
            return json.dumps(
                {
                    "intent": "procedure",
                    "confidence": "medium",
                    "query_rewrite": "报告怎么导出？",
                    "preferred_sources": ["document"],
                    "must_not_answer_realtime": False,
                    "safety_action": "answer_with_retrieval",
                    "reason": "测试",
                },
                ensure_ascii=False,
            )

    chat = FakeChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=chat,
    )

    events = list(app.iter_assistant_chat_events({"question": "报告怎么导出？"}))

    source_event = next(event for event in events if event.get("step_id") == "source_context")
    assert [doc["id"] for doc in source_event["documents"]] == [
        child_doc.id,
        parent_doc.id,
    ]
    assert "筛选目标报告" in chat.user_prompts[0]


def test_admin_app_iter_assistant_chat_events_uses_conversation_system_prompt():
    """会话级系统提示词应覆盖默认提示词，但不改变检索链路。"""

    class FakeEmbedding:
        def embed(self, text):
            return [0.1]

    class FakeDatabase:
        def list_retrieval_aliases(self, status="active"):
            """返回空别名集合，测试只关注会话系统提示。"""
            return []

        def search_knowledge(self, query_embedding, *, top_k, min_score):
            return []

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            return []

    class FakeChat:
        def __init__(self):
            self.calls = []

        def stream_complete(self, system_prompt, user_prompt):
            self.calls.append((system_prompt, user_prompt))
            yield "会话提示已生效"

    chat = FakeChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.4),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=chat,
    )

    events = list(
        app.iter_assistant_chat_events(
            {
                "question": "开票需要什么资料？",
                "system_prompt": "你是财务客服助手，只回答开票相关问题。",
            }
        )
    )

    assert chat.calls[0][0] == "你是财务客服助手，只回答开票相关问题。"
    assert events[-1]["answer_draft"] == "会话提示已生效"


def test_admin_app_assistant_system_prompt_has_no_code_default(monkeypatch):
    """智能问答没有会话提示词和本地文件时，不再注入代码硬编码默认提示。"""
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"))

    def missing_system_prompt():
        raise FileNotFoundError

    monkeypatch.setattr("cyclops.admin_server.load_system_prompt", missing_system_prompt)

    assert app.assistant_system_prompt() == ""
    assert app.assistant_system_prompt_from_payload({"system_prompt": ""}) == ""


def _settings_with_rerank(**overrides):
    """构造带 rerank 字段的 Settings，给 snapshot / payload 测试复用。"""
    base = {
        "database_url": "postgresql://u:p@127.0.0.1:5432/db",
        "chat_base_url": "https://newapi.example.com/v1",
        "chat_api_key": "chat-key",
        "chat_model": "deepseek-chat",
        "embedding_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embedding_api_key": "embedding-key",
        "embedding_model": "text-embedding-v4",
    }
    env = {key.upper(): value for key, value in base.items()}
    env.update(
        {
            "RERANK_BASE_URL": overrides.get("rerank_base_url", "https://rerank.example.com"),
            "RERANK_API_KEY": overrides.get("rerank_api_key", "rerank-key"),
            "RERANK_MODEL": overrides.get("rerank_model", "bge-reranker-v2-m3"),
            "RERANK_INPUT_SIZE": str(overrides.get("rerank_input_size", 50)),
        }
    )
    return Settings.from_env(env)


def test_settings_snapshot_includes_masked_rerank_fields():
    """设置弹窗需要返回 rerank 配置摘要，但不能暴露 API key 明文。"""
    settings = _settings_with_rerank(rerank_input_size=30)
    app = AdminApp(settings)

    snapshot = app.settings_snapshot()

    assert snapshot["rerank_base_url"] == "https://rerank.example.com"
    assert snapshot["rerank_api_key"] != "rerank-key"
    assert snapshot["rerank_api_key_configured"] is True
    assert "••" in snapshot["rerank_api_key"]
    assert snapshot["rerank_model"] == "bge-reranker-v2-m3"
    assert snapshot["rerank_input_size"] == 30


def test_settings_payload_to_env_passes_rerank_fields():
    """设置页 payload 转 env 时应携带 rerank 4 个字段。"""
    env = settings_payload_to_env(
        {
            "database_url": "postgresql://u:p@127.0.0.1:5432/db",
            "chat_base_url": "https://newapi.example.com/v1",
            "chat_api_key": "chat-key",
            "chat_model": "deepseek-chat",
            "embedding_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "embedding_api_key": "embedding-key",
            "embedding_model": "text-embedding-v4",
            "rerank_base_url": "https://rerank.example.com",
            "rerank_api_key": "rerank-key",
            "rerank_model": "bge-reranker-v2-m3",
            "rerank_input_size": "40",
        }
    )

    assert env["RERANK_BASE_URL"] == "https://rerank.example.com"
    assert env["RERANK_API_KEY"] == "rerank-key"
    assert env["RERANK_MODEL"] == "bge-reranker-v2-m3"
    assert env["RERANK_INPUT_SIZE"] == "40"


def test_settings_to_tenant_settings_includes_rerank_fields():
    """tenant settings 持久化时需要包含 rerank 字段。"""
    settings = _settings_with_rerank()
    values = settings_to_tenant_settings(settings)

    assert values["rerank_base_url"] == "https://rerank.example.com"
    assert values["rerank_api_key"] == "rerank-key"
    assert values["rerank_model"] == "bge-reranker-v2-m3"
    assert values["rerank_input_size"] == 50


def test_settings_round_trip_preserves_import_parse_worker_timing():
    """保存任意设置时必须保留 worker 运维参数，不能静默回到进程默认值。"""
    settings = Settings(
        database_url="postgresql://u:p@127.0.0.1:5432/db",
        chat_base_url="https://newapi.example.com/v1",
        chat_api_key="chat-key",
        chat_model="deepseek-chat",
        embedding_base_url="https://embedding.example.com/v1",
        embedding_api_key="embedding-key",
        embedding_model="text-embedding-v4",
        import_parse_worker_poll_interval_seconds=0.25,
        import_parse_worker_lease_seconds=90,
    )

    persisted = settings_to_tenant_settings(settings)
    restored = Settings.from_env(settings_payload_to_env(persisted))

    assert persisted["import_parse_worker_poll_interval_seconds"] == 0.25
    assert persisted["import_parse_worker_lease_seconds"] == 90
    assert restored.import_parse_worker_poll_interval_seconds == 0.25
    assert restored.import_parse_worker_lease_seconds == 90


def test_admin_app_analytics_overview_returns_hit_rate_buckets():
    """看板概览应给出今日 / 7 日 / 30 日的命中率和总查询。"""

    class FakeDatabase:
        def __init__(self):
            self.calls = []

        def query_analytics_overview(self, *, today, last_7d, last_30d):
            self.calls.append(("overview", today, last_7d, last_30d))
            return {
                "today": {"total": 12, "hit_rate": 0.83, "zero_hit": 2},
                "last_7d": {"total": 88, "hit_rate": 0.74, "zero_hit": 14},
                "last_30d": {"total": 350, "hit_rate": 0.71, "zero_hit": 60},
            }

    db = FakeDatabase()
    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=db)

    overview = app.analytics_overview()

    assert overview["today"]["hit_rate"] == 0.83
    assert overview["last_30d"]["zero_hit"] == 60
    assert db.calls and db.calls[0][0] == "overview"


def test_admin_app_analytics_top_queries_passes_filters():
    """高频查询接口应按 limit 和 since 透传到 DB。"""
    calls = []

    class FakeDatabase:
        def list_top_queries(self, *, limit, since):
            calls.append((limit, since))
            return [{"query": "登录失败", "count": 12}]

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.list_top_queries({"limit": ["20"], "days": ["14"]})

    assert result["items"][0]["query"] == "登录失败"
    assert calls[0][0] == 20
    # since 必须是 UTC 时间戳，并对应到 14 天前
    assert calls[0][1] is not None


def test_admin_app_analytics_zero_hit_returns_zero_hit_queries():
    """零命中接口应返回 hit_count=0 的最近查询。"""

    class FakeDatabase:
        def list_zero_hit_queries(self, *, limit, since):
            assert limit == 50
            return [{"query": "印度站本月活动", "created_at": "2026-05-20T10:00:00Z"}]

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.list_zero_hit_queries({"limit": ["50"], "days": ["7"]})

    assert result["items"][0]["query"] == "印度站本月活动"


def test_admin_app_analytics_low_score_uses_min_score_threshold():
    """低置信查询接口默认用 rag_min_score 当阈值，可被参数覆盖。"""
    calls = []

    class FakeDatabase:
        def list_low_score_queries(self, *, limit, since, threshold):
            calls.append({"limit": limit, "threshold": threshold})
            return [{"query": "TikTok Shop 黑名单", "top_score": 0.31}]

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_min_score=0.35),
        db=FakeDatabase(),
    )

    result = app.list_low_score_queries({"limit": ["20"], "days": ["30"]})

    assert result["items"][0]["query"] == "TikTok Shop 黑名单"
    assert calls[0]["threshold"] == 0.35


def test_admin_app_analytics_top_chunks_returns_chunk_frequency():
    """chunk 引用频次接口应返回 chunk_id 出现次数排序。"""

    class FakeDatabase:
        def top_referenced_chunks(self, *, limit, since):
            return [
                {"chunk_id": "kc_doc_1", "count": 42},
                {"chunk_id": "kc_doc_2", "count": 18},
            ]

    app = AdminApp(SimpleNamespace(database_url="postgresql://unused"), db=FakeDatabase())

    result = app.list_top_referenced_chunks({"limit": ["10"], "days": ["7"]})

    assert result["items"][0]["chunk_id"] == "kc_doc_1"
    assert result["items"][0]["count"] == 42


def test_admin_app_cluster_zero_hit_calls_chat_and_saves_summaries():
    """零命中聚类应取最近 N 天零命中 query，调 chat，并把 JSON 结果写入 cluster_summaries。"""
    saved = []

    class FakeDatabase:
        def list_zero_hit_queries(self, *, limit, since):
            return [
                {"query": "印度站本月活动", "created_at": "2026-05-20T01:00:00Z"},
                {"query": "印度站活动规则", "created_at": "2026-05-19T01:00:00Z"},
                {"query": "TikTok Shop 黑名单", "created_at": "2026-05-18T01:00:00Z"},
            ]

        def save_cluster_summary(self, row):
            saved.append(row)
            return {**row, "id": len(saved)}

    class FakeChat:
        def __init__(self):
            self.calls = []

        def complete(self, system_prompt, user_prompt):
            self.calls.append((system_prompt, user_prompt))
            return json.dumps(
                {
                    "clusters": [
                        {
                            "cluster_label": "印度站活动",
                            "suggested_content": "补充印度站本月活动规则页面",
                            "representative_queries": ["印度站本月活动", "印度站活动规则"],
                        },
                        {
                            "cluster_label": "TikTok 黑名单",
                            "suggested_content": "补充黑名单复审 SOP",
                            "representative_queries": ["TikTok Shop 黑名单"],
                        },
                    ]
                },
                ensure_ascii=False,
            )

    chat = FakeChat()
    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=5, rag_min_score=0.35),
        db=FakeDatabase(),
        chat=chat,
    )

    result = app.cluster_zero_hit_queries({"days": "7", "limit": "200"})

    assert chat.calls, "cluster path must call chat model"
    assert len(saved) == 2
    assert saved[0]["cluster_label"] == "印度站活动"
    assert "印度站本月活动" in saved[0]["sample_queries"]
    assert saved[0]["event_count"] == 2
    assert result["items"][0]["cluster_label"] == "印度站活动"


def test_admin_app_iter_assistant_chat_events_records_query_event():
    """RAG 主路径完成后应把查询写入 query_analytics_events，便于看板分析。"""
    recorded = []

    class FakeEmbedding:
        def embed(self, text):
            return [0.1, 0.2, 0.3]

    class FakeDatabase:
        def list_retrieval_aliases(self, status="active"):
            """返回空别名集合，测试只关注查询分析记录。"""
            return []

        def search_knowledge(self, query_embedding, *, top_k, min_score):
            """返回查询打点测试使用的向量候选。"""
            return _admin_hybrid_result().vector_documents

        def search_knowledge_text(self, query_text, *, top_k, query_terms):
            return []

        def record_query_event(self, event):
            recorded.append(event)

    class FakeChat:
        def stream_complete(self, system_prompt, user_prompt):
            yield "答："
            yield "ok"

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", rag_top_k=3, rag_min_score=0.35),
        db=FakeDatabase(),
        embeddings=FakeEmbedding(),
        chat=FakeChat(),
    )

    events = list(
        app.iter_assistant_chat_events(
            {
                "question": "如何处理印度站本月活动？",
                "requester_type": "admin_ui",
                "requester_id": "human-1",
            }
        )
    )

    assert events[-1]["type"] == "done"
    assert recorded, "iter_assistant_chat_events must record query_analytics_events"
    event = recorded[0]
    assert event["query"] == "如何处理印度站本月活动？"
    assert event["hit_count"] >= 1
    assert event["top_score"] is not None
    assert event["requester_type"] == "admin_ui"
    assert event["requester_id"] == "human-1"
    assert "kc_document_child_1" in event["retrieved_chunk_ids"]


def _mineru_asset_app(tmp_path, file_id="imp_test", asset_relpath="images/cover.png", asset_bytes=b"PNG_BYTES"):
    """构造一个 AdminApp 加上对应 mineru-assets 目录与可下载的导入文件记录。"""
    upload_dir = tmp_path / "uploads"
    asset_dir = upload_dir / "mineru-assets" / file_id
    asset_dir.mkdir(parents=True)
    asset_subdir = asset_dir / "/".join(asset_relpath.split("/")[:-1]) if "/" in asset_relpath else asset_dir
    asset_subdir.mkdir(parents=True, exist_ok=True)
    asset_full = asset_subdir / asset_relpath.split("/")[-1]
    asset_full.write_bytes(asset_bytes)

    class FakeDatabase:
        def get_import_file(self, fid):
            if fid != file_id:
                return None
            return {"id": file_id, "parser": "mineru", "stored_path": str(upload_dir / "raw.pdf")}

    app = AdminApp(
        SimpleNamespace(
            database_url="postgresql://unused",
            upload_dir=upload_dir,
        ),
        db=FakeDatabase(),
    )
    return app, asset_full


def test_admin_app_get_import_asset_serves_file_within_mineru_assets(tmp_path):
    """资产路由应返回 mineru-assets/<file_id>/<relpath> 下的文件路径与字节。"""
    app, asset_full = _mineru_asset_app(tmp_path, asset_relpath="images/cover.png", asset_bytes=b"PNG_BYTES")

    record, path = app.get_import_asset("imp_test", "images/cover.png")

    assert record["id"] == "imp_test"
    assert path == asset_full
    assert path.read_bytes() == b"PNG_BYTES"


def test_admin_app_get_import_asset_rejects_path_traversal(tmp_path):
    """资产路径含 ../ 必须拒绝，避免读取上层目录文件。"""
    app, _ = _mineru_asset_app(tmp_path)

    with pytest.raises(AdminValidationError):
        app.get_import_asset("imp_test", "../../etc/passwd")


def test_admin_app_get_import_asset_404_when_missing(tmp_path):
    """资产文件不存在时报 not found，不能落到任意 IO 错误。"""
    app, _ = _mineru_asset_app(tmp_path)

    with pytest.raises(AdminNotFoundError):
        app.get_import_asset("imp_test", "images/missing.png")


def test_admin_app_get_import_asset_unknown_file_id_404(tmp_path):
    """import file 记录不存在时也是 404。"""
    app, _ = _mineru_asset_app(tmp_path)

    with pytest.raises(AdminNotFoundError):
        app.get_import_asset("imp_unknown", "images/cover.png")


def test_admin_app_set_import_file_disabled_persists_flag(tmp_path):
    """文件级禁用 API 必须把 is_disabled 写回去并返回最新文件记录。"""
    captured = []

    class FakeDatabase:
        def set_import_file_disabled(self, file_id, is_disabled):
            captured.append((file_id, is_disabled))
            return {"id": file_id, "is_disabled": is_disabled, "original_name": "x.pdf"}

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    result = app.set_import_file_disabled("imp_1", {"is_disabled": True})

    assert captured == [("imp_1", True)]
    assert result["item"]["is_disabled"] is True


def test_admin_app_set_import_file_disabled_requires_flag(tmp_path):
    """payload 缺 is_disabled 字段必须报校验错误，避免静默写默认值。"""
    class FakeDatabase:
        def set_import_file_disabled(self, file_id, is_disabled):  # pragma: no cover
            raise AssertionError("should not be called")

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminValidationError):
        app.set_import_file_disabled("imp_1", {})


def test_admin_app_set_import_file_disabled_unknown_id_404(tmp_path):
    """切换不存在的文件应抛 NotFound，不能静默成功。"""
    class FakeDatabase:
        def set_import_file_disabled(self, file_id, is_disabled):
            return None

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminNotFoundError):
        app.set_import_file_disabled("imp_missing", {"is_disabled": True})


def test_admin_app_set_import_chunk_disabled_persists_flag(tmp_path):
    """切片级禁用 API 必须把 is_disabled 写回切片记录并返回最新条目。"""
    captured = []

    class FakeDatabase:
        def set_import_chunk_disabled(self, chunk_id, is_disabled):
            captured.append((chunk_id, is_disabled))
            return {"id": chunk_id, "file_id": "imp_1", "is_disabled": is_disabled}

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    result = app.set_import_chunk_disabled("chk_1", {"is_disabled": False})

    assert captured == [("chk_1", False)]
    assert result["item"]["is_disabled"] is False


def test_admin_app_set_import_chunk_disabled_unknown_id_404(tmp_path):
    """切片不存在必须 404，前端可据此提示用户刷新。"""
    class FakeDatabase:
        def set_import_chunk_disabled(self, chunk_id, is_disabled):
            return None

    app = AdminApp(
        SimpleNamespace(database_url="postgresql://unused", upload_dir=tmp_path),
        db=FakeDatabase(),
    )

    with pytest.raises(AdminNotFoundError):
        app.set_import_chunk_disabled("chk_missing", {"is_disabled": True})
