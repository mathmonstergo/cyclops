from __future__ import annotations

import json
import re
from typing import Any

from cyclops.llm import ChatClient


class ImportQuestionError(ValueError):
    pass


class ImportQuestionAssistant:
    """对单个文档切片生成假设性用户问题，用于补强 embedding 与召回。"""

    def __init__(self, chat: ChatClient):
        self.chat = chat

    @property
    def model(self) -> str:
        return self.chat.model

    def generate_questions(
        self,
        *,
        source_text: str,
        section_path: list[str] | None = None,
        source_title: str = "",
        block_type: str | None = None,
        max_questions: int = 5,
    ) -> list[str]:
        """同步调用 LLM 产出 1~max_questions 条口语化问题。

        失败抛 ImportQuestionError 由上层捕获写入 questions_error，
        不阻塞同批次其他切片继续生成。
        """
        cleaned = (source_text or "").strip()
        if not cleaned:
            raise ImportQuestionError("source_text is empty")
        system_prompt = self._system_prompt(max_questions)
        user_prompt = self._user_prompt(
            source_text=cleaned,
            section_path=section_path or [],
            source_title=source_title,
            block_type=block_type,
            max_questions=max_questions,
        )
        response = self.chat.complete(system_prompt, user_prompt)
        payload = self._parse_json_object(response)
        questions = payload.get("questions")
        if not isinstance(questions, list):
            raise ImportQuestionError("AI questions field must be a list")
        normalized = [str(q).strip() for q in questions if str(q).strip()]
        # 保留顺序去重；同义不同写法保留前者
        seen: set[str] = set()
        unique: list[str] = []
        for q in normalized:
            if q in seen:
                continue
            seen.add(q)
            unique.append(q)
        return unique[:max_questions]

    @staticmethod
    def _system_prompt(max_questions: int) -> str:
        """约束模型只产出口语化问题，不引入 chunk 中没有的事实。"""
        return "\n".join(
            [
                "你是客服知识库切片问题生成助手。",
                f"任务是为下面这段文档切片生成 1~{max_questions} 条用户最可能用来检索它的口语化问题。",
                "硬性要求：",
                "- 每个问题尽量像真实用户提问，避免书面化堆砌名词",
                "- 不要把切片原句一字不差搬过来，要重新组织成问句",
                "- 严禁生成切片中没有出现的事实或数字",
                "- 切片是图片/表格/公式时，问题围绕标题、说明、上下文，不要编造未出现的列名或图例",
                "- 如果切片内容过短或没有可被检索的实质信息，返回空列表",
                "输出 JSON 对象 {\"questions\": [\"...\", \"...\"]}，不要 Markdown 或解释。",
            ]
        )

    @staticmethod
    def _user_prompt(
        *,
        source_text: str,
        section_path: list[str],
        source_title: str,
        block_type: str | None,
        max_questions: int,
    ) -> str:
        """把切片上下文（文件、章节、块类型）一并提交，让问题更贴合定位。"""
        header_parts: list[str] = []
        if source_title:
            header_parts.append(f"来源文件：{source_title}")
        if section_path:
            header_parts.append(f"章节路径：{' > '.join(section_path)}")
        if block_type:
            header_parts.append(f"块类型：{block_type}")
        header = "\n".join(header_parts)
        lines = []
        if header:
            lines.append(header)
            lines.append("")
        lines.append("切片正文：")
        lines.append(source_text)
        lines.append("")
        lines.append(f"请输出最多 {max_questions} 条问题。")
        return "\n".join(lines)

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        """解析模型 JSON 对象；外部模型误加 json fence 时先剥离代码块。"""
        stripped = (text or "").strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
        if fence_match:
            stripped = fence_match.group(1)
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ImportQuestionError("AI questions must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ImportQuestionError("AI questions must be a JSON object")
        return payload
