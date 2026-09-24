from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

from cyclops.db.models import KgExpandedCandidate, KgFactHit, RetrievedKnowledgeChunk


logger = logging.getLogger(__name__)

INTENT_FAQ_EXACT = "faq_exact"
INTENT_PROCEDURE = "procedure"
INTENT_TROUBLESHOOTING = "troubleshooting"
INTENT_REALTIME_STATUS = "realtime_status"
INTENT_CHITCHAT = "chitchat_or_out_of_scope"
INTENT_SENSITIVE = "sensitive_or_forbidden"

DOMAIN_KEYWORDS = (
    "团体报告",
    "测评报告",
    "报告",
    "生成",
    "导出",
    "下载",
    "登录",
    "密码",
    "账号",
    "账户",
    "订单",
    "退款",
    "发票",
    "上传",
    "配置",
    "审核",
    "失败",
    "报错",
    "无法",
    "权限",
    "微信",
    "后台",
    "状态",
    "进度",
)


@dataclass(frozen=True)
class QueryAnalysis:
    """表示用户问题的检索意图，关键约束是只影响召回策略，不直接生成答案。"""

    intent: str
    confidence: str
    query: str
    query_rewrite: str
    preferred_sources: list[str]
    must_not_answer_realtime: bool = False
    safety_action: str = "answer_with_retrieval"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为前端调试事件可直接序列化的字典。"""
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "query": self.query,
            "query_rewrite": self.query_rewrite,
            "preferred_sources": self.preferred_sources,
            "must_not_answer_realtime": self.must_not_answer_realtime,
            "safety_action": self.safety_action,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FusedCandidate:
    """表示多路召回融合后的候选，保留来源通道和原始分数供调试。"""

    document: RetrievedKnowledgeChunk
    fused_score: float
    channels: tuple[str, ...]
    vector_score: float | None = None
    keyword_score: float | None = None
    kg_score: float | None = None
    kg_matches: tuple[KgFactHit, ...] = ()


@dataclass(frozen=True)
class HybridRetrievalResult:
    """表示一次统一混合检索结果，候选与 parent 上下文必须分开保存。"""

    query: str
    query_terms: list[str]
    vector_documents: list[RetrievedKnowledgeChunk]
    keyword_documents: list[RetrievedKnowledgeChunk]
    candidates: list[FusedCandidate]
    parent_documents: list[RetrievedKnowledgeChunk]
    kg_fact_hits: list[KgFactHit]
    kg_expanded_candidates: list[KgExpandedCandidate]
    candidate_limit: int
    query_embedding_dimensions: int
    rerank_used: bool


class HybridRetrievalService:
    """执行唯一混合检索流程，正式调用默认只融合向量与关键词候选。"""

    def __init__(
        self,
        *,
        database: Any,
        embeddings: Any,
        rerank: Any | None,
        top_k: int,
        min_score: float,
    ) -> None:
        """保存检索依赖与阈值，关键约束是所有入口复用同一组配置。"""
        self.database = database
        self.embeddings = embeddings
        self.rerank = rerank
        self.top_k = top_k
        self.min_score = min_score

    def retrieve(
        self,
        query: str,
        *,
        include_parent_context: bool,
        use_kg: bool,
    ) -> HybridRetrievalResult:
        """召回并融合统一知识候选，KG 只能由调用方显式开启。"""
        aliases = self.database.list_retrieval_aliases()
        query_terms = build_keyword_terms(query, aliases)
        query_embedding = self.embeddings.embed(query)
        rerank_input_size = int(getattr(self.rerank, "input_size", 0) or 0)
        candidate_limit = max(self.top_k * 2, self.top_k, rerank_input_size)
        vector_documents = self.database.search_knowledge(
            query_embedding,
            top_k=candidate_limit,
            min_score=self.min_score,
        )
        keyword_documents = self.database.search_knowledge_text(
            query,
            top_k=candidate_limit,
            query_terms=query_terms,
        )
        kg_fact_hits: list[KgFactHit] = []
        kg_expanded_candidates: list[KgExpandedCandidate] = []
        if use_kg:
            kg_fact_hits = self.database.search_kg_knowledge_text(
                query,
                top_k=candidate_limit,
                query_terms=query_terms,
            )
            kg_expanded_candidates = self.database.expand_kg_fact_hits(kg_fact_hits)
        fused_candidates = fuse_retrieval_candidates(
            vector_docs=vector_documents,
            keyword_docs=keyword_documents,
            kg_candidates=kg_expanded_candidates if use_kg else None,
            top_k=candidate_limit,
        )
        candidates, rerank_used = rerank_candidates(
            query,
            fused_candidates,
            client=self.rerank,
            top_k=self.top_k,
        )
        parent_documents: list[RetrievedKnowledgeChunk] = []
        if include_parent_context:
            child_ids = [
                candidate.document.id
                for candidate in candidates
                if candidate.document.parent_chunk_id
                and candidate.document.chunk_level != "parent"
            ]
            if child_ids:
                candidate_ids = {candidate.document.id for candidate in candidates}
                retrieved_parents = self.database.get_parent_context_chunks(child_ids)
                if any(
                    not isinstance(document, RetrievedKnowledgeChunk)
                    for document in retrieved_parents
                ):
                    raise TypeError("parent context must contain RetrievedKnowledgeChunk")
                parent_documents = [
                    document
                    for document in retrieved_parents
                    if document.id not in candidate_ids
                ]
        return HybridRetrievalResult(
            query=query,
            query_terms=query_terms,
            vector_documents=vector_documents,
            keyword_documents=keyword_documents,
            candidates=candidates,
            parent_documents=parent_documents,
            kg_fact_hits=kg_fact_hits,
            kg_expanded_candidates=kg_expanded_candidates,
            candidate_limit=candidate_limit,
            query_embedding_dimensions=len(query_embedding),
            rerank_used=rerank_used,
        )


@dataclass(frozen=True)
class EvalCaseResult:
    """表示单条检索评测结果，关键约束是 expected_ids 和 retrieved_ids 使用同一 id 口径。"""

    question: str
    expected_ids: list[str]
    retrieved_ids: list[str]


def analyze_query(question: str, chat: Any | None = None) -> QueryAnalysis:
    """分析用户问题意图；规则高置信命中优先，低置信场景可用 Chat 模型兜底。"""
    text = _normalize_query(question)
    analysis = _analyze_query_by_rules(text)
    if analysis.confidence == "high" or chat is None:
        return analysis
    fallback = _analyze_query_with_chat(text, chat)
    return fallback or analysis


def fuse_retrieval_candidates(
    *,
    vector_docs: Iterable[RetrievedKnowledgeChunk],
    keyword_docs: Iterable[RetrievedKnowledgeChunk],
    kg_candidates: Iterable[KgExpandedCandidate] | None = None,
    top_k: int,
    rrf_k: int = 60,
) -> list[FusedCandidate]:
    """用 RRF 融合多路候选，关键约束是 KG 通道必须由调用方显式传入。"""
    candidates: dict[str, dict[str, Any]] = {}

    def add_channel(
        docs: Iterable[RetrievedKnowledgeChunk],
        channel: str,
    ) -> None:
        """加入 canonical 检索候选，禁止 dict 或旧 FAQ 对象双形状。"""
        for rank, doc in enumerate(docs, start=1):
            if not isinstance(doc, RetrievedKnowledgeChunk):
                raise TypeError("retrieval candidate must be RetrievedKnowledgeChunk")
            doc_id = doc.id
            item = candidates.setdefault(
                doc_id,
                {
                    "document": doc,
                    "fused_score": 0.0,
                    "channels": [],
                    "vector_score": None,
                    "keyword_score": None,
                    "kg_score": None,
                    "kg_rank": None,
                    "kg_matches": {},
                },
            )
            item["fused_score"] += 1.0 / (rrf_k + rank)
            if channel not in item["channels"]:
                item["channels"].append(channel)
            score = float(doc.score)
            if channel == "vector":
                item["vector_score"] = score
            if channel == "keyword":
                item["keyword_score"] = score

    def add_kg_channel(candidates_to_add: Iterable[KgExpandedCandidate]) -> None:
        """按原始知识行加入一次 KG RRF vote，并合并该行关联的 fact 诊断。"""
        for candidate in candidates_to_add:
            if not isinstance(candidate, KgExpandedCandidate):
                raise TypeError("KG candidate must be KgExpandedCandidate")
            if not isinstance(candidate.document, RetrievedKnowledgeChunk):
                raise TypeError("KG expanded document must be RetrievedKnowledgeChunk")
            if any(not isinstance(match, KgFactHit) for match in candidate.kg_matches):
                raise TypeError("KG matches must be KgFactHit")
            if not candidate.kg_matches:
                raise ValueError("KG expanded candidate requires at least one fact match")
            doc = candidate.document
            doc_id = str(doc.id)
            item = candidates.setdefault(
                doc_id,
                {
                    "document": doc,
                    "fused_score": 0.0,
                    "channels": [],
                    "vector_score": None,
                    "keyword_score": None,
                    "kg_score": None,
                    "kg_rank": None,
                    "kg_matches": {},
                },
            )
            best_match = min(
                candidate.kg_matches,
                key=lambda match: (match.fact_rank, match.fact_chunk_id),
            )
            previous_rank = item["kg_rank"]
            if previous_rank is None:
                item["fused_score"] += 1.0 / (rrf_k + best_match.fact_rank)
            elif best_match.fact_rank < previous_rank:
                item["fused_score"] += 1.0 / (rrf_k + best_match.fact_rank)
                item["fused_score"] -= 1.0 / (rrf_k + previous_rank)
            item["kg_rank"] = (
                best_match.fact_rank
                if previous_rank is None
                else min(previous_rank, best_match.fact_rank)
            )
            if "kg" not in item["channels"]:
                item["channels"].append("kg")
            if (
                item["kg_score"] is None
                or best_match.fact_rank < previous_rank
                or (
                    best_match.fact_rank == item["kg_rank"]
                    and best_match.fact_score > item["kg_score"]
                )
            ):
                item["kg_score"] = best_match.fact_score
            for match in candidate.kg_matches:
                existing = item["kg_matches"].get(match.fact_chunk_id)
                if existing is None or match.fact_rank < existing.fact_rank:
                    item["kg_matches"][match.fact_chunk_id] = match

    add_channel(vector_docs, "vector")
    add_channel(keyword_docs, "keyword")
    if kg_candidates is not None:
        add_kg_channel(kg_candidates)

    fused = [
        FusedCandidate(
            document=item["document"],
            fused_score=float(item["fused_score"]),
            channels=tuple(item["channels"]),
            vector_score=item["vector_score"],
            keyword_score=item["keyword_score"],
            kg_score=item["kg_score"],
            kg_matches=tuple(
                sorted(
                    item["kg_matches"].values(),
                    key=lambda match: (match.fact_rank, match.fact_chunk_id),
                )
            ),
        )
        for item in candidates.values()
    ]
    return sorted(
        fused,
        key=lambda item: (
            item.fused_score,
            item.vector_score if item.vector_score is not None else -1.0,
            item.keyword_score if item.keyword_score is not None else -1.0,
        ),
        reverse=True,
    )[:top_k]


def rerank_candidates(
    query: str,
    candidates: list[FusedCandidate],
    *,
    client: Any | None,
    top_k: int,
) -> tuple[list[FusedCandidate], bool]:
    """对融合候选执行 cross-encoder 重排，并返回结果与是否实际采用排名。

    关键约束：只有至少一个合法 index 真正进入结果时才标记已重排；provider
    返回不足时按原融合顺序补齐，调用失败或无有效排名则完整保留原顺序。
    """
    if not candidates:
        return [], False
    if client is None or len(candidates) <= top_k:
        return candidates[:top_k], False
    input_size = int(getattr(client, "input_size", len(candidates)) or len(candidates))
    payload = candidates[:input_size]
    documents = [_extract_candidate_text(candidate) for candidate in payload]
    try:
        results = client.rerank(query, documents, top_n=min(top_k, len(payload)))
    except Exception as exc:
        logger.warning("rerank_candidates failed: %s", exc, exc_info=True)
        return candidates[:top_k], False
    if not results:
        return candidates[:top_k], False
    ordered: list[FusedCandidate] = []
    seen: set[int] = set()
    for item in results:
        index = getattr(item, "index", None)
        if index is None or index in seen or index < 0 or index >= len(payload):
            continue
        ordered.append(payload[index])
        seen.add(index)
        if len(ordered) >= top_k:
            break
    if not ordered:
        return candidates[:top_k], False
    if len(ordered) < top_k:
        for index, candidate in enumerate(candidates):
            if index in seen:
                continue
            ordered.append(candidate)
            if len(ordered) >= top_k:
                break
    return ordered[:top_k], True


def _extract_candidate_text(candidate: FusedCandidate) -> str:
    """读取 canonical content 作为 rerank 文本，不接受旧 FAQ 字段回退。"""
    document = candidate.document
    if not isinstance(document, RetrievedKnowledgeChunk):
        raise TypeError("rerank candidate document must be RetrievedKnowledgeChunk")
    return document.content


def build_keyword_terms(question: str, aliases: Iterable[dict[str, Any]] | None = None) -> list[str]:
    """构建关键词召回词表，关键约束是保留错误码并用人工别名扩展。"""
    text = _normalize_query(question)
    terms: list[str] = []

    for code in re.findall(r"\b[A-Za-z]+[-_]?\d{2,}[A-Za-z0-9_-]*\b", text):
        _append_unique(terms, code.upper())

    alias_rows = list(aliases or [])
    for row in alias_rows:
        canonical = str(row.get("canonical") or "").strip()
        row_aliases = _coerce_aliases(row.get("aliases"))
        matched_aliases = [alias for alias in row_aliases if alias in text]
        matched = bool(canonical and canonical in text) or bool(matched_aliases)
        if not matched:
            continue
        for alias in matched_aliases:
            _append_unique(terms, alias)
        if canonical:
            _append_unique(terms, canonical)
        for alias in row_aliases:
            _append_unique(terms, alias)

    for keyword in DOMAIN_KEYWORDS:
        if keyword in text:
            _append_unique(terms, keyword)

    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", text):
        upper = token.upper()
        if upper not in terms:
            _append_unique(terms, token)

    return terms[:20]


def compute_retrieval_metrics(cases: list[EvalCaseResult], *, k: int) -> dict[str, Any]:
    """计算检索评测指标，第一版聚焦 Recall@K、MRR 和首位命中率。"""
    if not cases:
        return {"case_count": 0, "recall_at_k": 0.0, "mrr": 0.0, "hit_rate_at_1": 0.0}

    recall_hits = 0
    reciprocal_ranks = 0.0
    top1_hits = 0
    for case in cases:
        expected = set(case.expected_ids)
        retrieved = case.retrieved_ids[:k]
        if not expected:
            continue
        if expected.intersection(retrieved):
            recall_hits += 1
        if retrieved and retrieved[0] in expected:
            top1_hits += 1
        for index, doc_id in enumerate(retrieved, start=1):
            if doc_id in expected:
                reciprocal_ranks += 1.0 / index
                break

    case_count = len(cases)
    return {
        "case_count": case_count,
        "recall_at_k": recall_hits / case_count,
        "mrr": reciprocal_ranks / case_count,
        "hit_rate_at_1": top1_hits / case_count,
    }


def _normalize_query(question: str) -> str:
    """清理用户问题的首尾空白，避免规则识别受换行和多空格影响。"""
    return re.sub(r"\s+", " ", str(question or "").strip())


def _analyze_query_by_rules(text: str) -> QueryAnalysis:
    """规则识别高确定性意图，关键约束是宁可保守也不误判敏感和实时状态。"""
    lowered = text.lower()
    if _contains_any(
        lowered,
        (
            "api key",
            "apikey",
            "secret",
            "access token",
            "system prompt",
            "系统提示词",
            "密钥",
            "数据库密码",
            "微信 token",
            "后台密码",
        ),
    ):
        return QueryAnalysis(
            intent=INTENT_SENSITIVE,
            confidence="high",
            query=text,
            query_rewrite=text,
            preferred_sources=[],
            safety_action="refuse",
            reason="命中敏感信息规则",
        )

    if _contains_any(
        text,
        ("现在", "当前", "实时", "到哪一步", "处理到哪", "有没有完成", "是否完成", "进度"),
    ) and _contains_any(text, ("状态", "报告", "订单", "账号", "后台", "生成", "处理")):
        return QueryAnalysis(
            intent=INTENT_REALTIME_STATUS,
            confidence="high",
            query=text,
            query_rewrite=text,
            preferred_sources=["faq", "document"],
            must_not_answer_realtime=True,
            reason="命中实时状态规则",
        )

    if _contains_any(
        text,
        ("怎么办", "无法", "不能", "失败", "报错", "没有生成", "没生成", "打不开", "登不上", "异常"),
    ):
        return QueryAnalysis(
            intent=INTENT_TROUBLESHOOTING,
            confidence="high",
            query=text,
            query_rewrite=text,
            preferred_sources=["faq", "document"],
            reason="命中故障排查规则",
        )

    if _contains_any(text, ("怎么", "如何", "步骤", "流程", "操作", "在哪里", "导出", "上传", "配置")):
        return QueryAnalysis(
            intent=INTENT_PROCEDURE,
            confidence="high",
            query=text,
            query_rewrite=text,
            preferred_sources=["document", "faq"],
            reason="命中操作流程规则",
        )

    if text in {"你好", "您好", "谢谢", "感谢", "在吗"}:
        return QueryAnalysis(
            intent=INTENT_CHITCHAT,
            confidence="high",
            query=text,
            query_rewrite=text,
            preferred_sources=[],
            safety_action="smalltalk",
            reason="命中闲聊规则",
        )

    return QueryAnalysis(
        intent=INTENT_FAQ_EXACT,
        confidence="medium",
        query=text,
        query_rewrite=text,
        preferred_sources=["faq", "document"],
        reason="未命中高置信规则，默认按标准知识库问答处理",
    )


def _analyze_query_with_chat(text: str, chat: Any) -> QueryAnalysis | None:
    """调用 Chat 模型做低置信兜底，只接受结构化 JSON，失败时回退规则结果。"""
    complete = getattr(chat, "complete", None)
    if complete is None:
        return None
    prompt = "\n".join(
        [
            "请判断客服知识库问题的检索意图，只输出 JSON。",
            "intent 只能是 faq_exact、procedure、troubleshooting、realtime_status、chitchat_or_out_of_scope、sensitive_or_forbidden。",
            "字段：intent, confidence, query_rewrite, preferred_sources, must_not_answer_realtime, safety_action, reason。",
            f"用户问题：{text}",
        ]
    )
    try:
        raw = complete("你是客服知识库检索意图分类器。", prompt)
        data = json.loads(str(raw))
    except Exception as exc:
        logger.warning(
            "intent classifier fallback to rule-based: %s", exc, exc_info=True
        )
        return None
    intent = str(data.get("intent") or INTENT_FAQ_EXACT)
    if intent not in {
        INTENT_FAQ_EXACT,
        INTENT_PROCEDURE,
        INTENT_TROUBLESHOOTING,
        INTENT_REALTIME_STATUS,
        INTENT_CHITCHAT,
        INTENT_SENSITIVE,
    }:
        return None
    preferred_sources = data.get("preferred_sources")
    if not isinstance(preferred_sources, list):
        preferred_sources = ["faq", "document"]
    return QueryAnalysis(
        intent=intent,
        confidence=str(data.get("confidence") or "medium"),
        query=text,
        query_rewrite=str(data.get("query_rewrite") or text).strip() or text,
        preferred_sources=[str(item) for item in preferred_sources],
        must_not_answer_realtime=bool(data.get("must_not_answer_realtime")),
        safety_action=str(data.get("safety_action") or "answer_with_retrieval"),
        reason=str(data.get("reason") or "Chat 模型兜底识别"),
    )


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    """判断文本是否包含任一关键词，保持规则实现直接可读。"""
    return any(needle in text for needle in needles)


def _append_unique(values: list[str], value: str) -> None:
    """追加非空唯一词，保持关键词构造顺序稳定。"""
    normalized = str(value or "").strip()
    if normalized and normalized not in values:
        values.append(normalized)


def _coerce_aliases(value: Any) -> list[str]:
    """把数据库或测试中的别名字段整理成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [item.strip() for item in value.split(",") if item.strip()]
        value = parsed
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
