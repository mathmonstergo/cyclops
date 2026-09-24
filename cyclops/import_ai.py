from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class ImportCandidateError(ValueError):
    pass


@dataclass(frozen=True)
class ImportCandidate:
    question: str
    answer: str
    similar_questions: list[str]
    category: str
    tags: list[str]
    confidence: str
    internal_note: str


class ImportAiAssistant:
    def __init__(self, chat: Any):
        self.chat = chat

    def generate_candidates(self, source_text: str) -> list[ImportCandidate]:
        """从一个聊天切块生成候选 FAQ，输出只作为人工审核草稿。"""
        response = self.chat.complete(self._system_prompt(), self._user_prompt(source_text))
        payload = self._parse_json_object(response)
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise ImportCandidateError("AI candidates field candidates must be a list")
        return [self._candidate_from_payload(item) for item in candidates if isinstance(item, dict)]

    @staticmethod
    def _system_prompt() -> str:
        """约束模型只沉淀长期知识，不输出一次性密码或临时状态。"""
        return "\n".join(
            [
                "你是客服知识库导入审核助手。",
                "请从聊天记录片段中提取可长期沉淀的 FAQ 候选。",
                "不要把临时进度、一次性账号密码、客户隐私写入标准答案。",
                "不输出一次性密码、密钥、token 或内部敏感配置。",
                "如果只适合内部提醒，请写入 internal_note，不要放进 answer。",
                "输出 JSON 对象，不要输出 Markdown。",
                "JSON 字段为 candidates；每项包含 question, answer, similar_questions, category, tags, confidence, internal_note。",
            ]
        )

    @staticmethod
    def _user_prompt(source_text: str) -> str:
        """把来源片段交给模型，并要求保守生成候选问答。"""
        return "\n".join(
            [
                "请基于以下来源片段生成候选 FAQ：",
                "",
                source_text,
            ]
        )

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        """解析模型 JSON 对象；外部模型误加 json fence 时先剥离代码块。"""
        stripped = text.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
        if fence_match:
            stripped = fence_match.group(1)
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ImportCandidateError("AI candidates must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ImportCandidateError("AI candidates must be a JSON object")
        return payload

    @staticmethod
    def _candidate_from_payload(payload: dict[str, Any]) -> ImportCandidate:
        """将模型单条候选转成内部结构，字段缺失时保持为空字符串或空列表。"""
        return ImportCandidate(
            question=str(payload.get("question", "")).strip(),
            answer=str(payload.get("answer", "")).strip(),
            similar_questions=_clean_list(payload.get("similar_questions")),
            category=str(payload.get("category", "")).strip(),
            tags=_clean_list(payload.get("tags")),
            confidence=str(payload.get("confidence", "medium")).strip() or "medium",
            internal_note=str(payload.get("internal_note", "")).strip(),
        )


def _clean_list(value: Any) -> list[str]:
    """把模型返回的列表字段清洗为非空字符串列表。"""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
