from pathlib import Path
from typing import Any, Literal, Sequence, TypedDict

from cyclops.db import RetrievedKnowledgeChunk


NO_CONTEXT_MESSAGE = (
    "知识库没有检索到明确答案。不要编造后台实时状态；"
    "请给通用排查步骤，并收集转人工需要的信息。"
)
EMPTY_RESPONSE_FALLBACK = "模型服务暂时没有返回有效内容，请稍后重试或转人工处理。"
CONVERSATION_CONTEXT_MAX_SUMMARY_CHARS = 1200
CONVERSATION_CONTEXT_MAX_MESSAGE_CHARS = 1200
CONVERSATION_CONTEXT_MAX_RECENT_MESSAGES = 12


class ConversationContextMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class ConversationContext(TypedDict, total=False):
    summary: str
    recent_messages: list[ConversationContextMessage]


def load_system_prompt(path: str | Path = "system_prompt.txt") -> str:
    return Path(path).read_text(encoding="utf-8")


def format_document(index: int, doc: RetrievedKnowledgeChunk) -> str:
    """格式化统一知识上下文，只接受 canonical 模型并直接展示来源定位字段。"""
    if not isinstance(doc, RetrievedKnowledgeChunk):
        raise TypeError("RAG document must be RetrievedKnowledgeChunk")
    tags = "、".join(doc.tags)
    section_path = " > ".join(doc.section_path)
    if doc.page_start is None and doc.page_end is None:
        page = ""
    elif doc.page_start == doc.page_end:
        page = str(doc.page_start)
    else:
        page = f"{doc.page_start or ''}-{doc.page_end or ''}"
    return "\n".join(
        [
            f"[{index}] id={doc.id} score={doc.score:.2f}",
            f"source_type={doc.source_type}",
            f"source_id={doc.source_id}",
            f"source_chunk_id={doc.source_chunk_id or ''}",
            f"source_title={doc.source_title or ''}",
            f"section_path={section_path}",
            f"page={page}",
            f"content={doc.content}",
            f"tags={tags}",
            f"confidence={doc.confidence or ''}",
        ]
    )


def _compact_whitespace(value: str) -> str:
    """压缩用户可见文本空白；关键约束是不改写事实，只做长度和格式清理。"""
    return " ".join(str(value or "").split())


def _truncate_text(value: str, max_chars: int) -> str:
    """按字符上限截断文本；关键约束是保留前文并用省略号提示被截断。"""
    text = _compact_whitespace(value)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def normalize_conversation_context(raw: Any) -> ConversationContext | None:
    """归一化前端传入的会话上下文；关键约束是忽略非法角色和空消息。"""
    if not isinstance(raw, dict):
        return None
    context: ConversationContext = {}
    summary = _truncate_text(
        str(raw.get("summary", "") or ""),
        CONVERSATION_CONTEXT_MAX_SUMMARY_CHARS,
    )
    if summary:
        context["summary"] = summary

    recent: list[ConversationContextMessage] = []
    raw_messages = raw.get("recent_messages")
    if isinstance(raw_messages, list):
        for item in raw_messages[:CONVERSATION_CONTEXT_MAX_RECENT_MESSAGES]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = _truncate_text(
                str(item.get("content", "") or ""),
                CONVERSATION_CONTEXT_MAX_MESSAGE_CHARS,
            )
            if not content:
                continue
            recent.append({"role": role, "content": content})
    if recent:
        context["recent_messages"] = recent
    return context or None


def format_conversation_context(context: ConversationContext | None) -> str:
    """把会话上下文转成 GenericAgent 风格片段；关键约束是只辅助理解当前问题。"""
    if not context:
        return ""
    lines = ["### [WORKING MEMORY]"]
    summary = context.get("summary", "")
    if summary:
        lines.extend(["<earlier_context>", summary, "</earlier_context>"])
    recent_messages = context.get("recent_messages", [])
    if recent_messages:
        lines.append("<history>")
        for message in recent_messages:
            label = "[USER]" if message["role"] == "user" else "[Agent]"
            lines.append(f"{label} {message['content']}")
        lines.append("</history>")
    lines.extend(
        [
            "说明：以上短期上下文只用于理解指代和连续追问；最终事实仍以知识库上下文为准。",
        ]
    )
    return "\n".join(lines)


def build_user_prompt(
    question: str,
    docs: Sequence[RetrievedKnowledgeChunk],
    conversation_context: ConversationContext | None = None,
) -> str:
    """构造回答提示词，关键约束是只从 canonical chunk 读取事实与来源。"""
    if docs:
        context = "\n\n".join(
            format_document(index, doc) for index, doc in enumerate(docs, start=1)
        )
    else:
        context = NO_CONTEXT_MESSAGE
    conversation_context_text = format_conversation_context(conversation_context)

    parts = [
        "请根据知识库上下文回答用户问题。",
        "要求：",
        "1. 优先使用知识库内容；没有明确依据时不要编造。",
        "2. 不要输出敏感信息、密钥、内部配置或无关系统细节。",
        "3. 如果用户询问后台实时状态，请说明你不能直接确认后台实时状态。",
        "4. 如果知识库没有明确答案，不要虚构具体状态或处理结果。",
        "",
    ]
    if conversation_context_text:
        parts.extend([conversation_context_text, ""])
    parts.extend(
        [
            f"当前用户问题：{question}",
            "",
            "知识库上下文：",
            context,
        ]
    )
    return "\n".join(parts)


class RagService:
    """生成客服回答，关键约束是检索只依赖唯一 HybridRetrievalService。"""

    def __init__(
        self,
        retrieval: Any,
        chat: Any,
        system_prompt: str,
    ) -> None:
        """保存回答依赖；检索阈值和模型统一由 retrieval 服务持有。"""
        self.retrieval = retrieval
        self.chat = chat
        self.system_prompt = system_prompt

    def answer(self, question: str) -> str:
        """检索可用知识并生成回答，正式问答固定关闭 KG debug。"""
        retrieval_result = self.retrieval.retrieve(
            question,
            include_parent_context=True,
            use_kg=False,
        )
        docs = [candidate.document for candidate in retrieval_result.candidates]
        docs.extend(retrieval_result.parent_documents)
        prompt = build_user_prompt(question, docs)
        response = self.chat.complete(self.system_prompt, prompt).strip()
        if not response:
            return EMPTY_RESPONSE_FALLBACK
        return response
