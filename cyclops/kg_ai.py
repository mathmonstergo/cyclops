from __future__ import annotations

import json
from typing import Any

from cyclops.document_kg import parse_document_entity_resolution_response
from cyclops.kg import parse_kg_extraction_response


class KnowledgeGraphAiAssistant:
    """KG 抽取助手：调用 Chat 模型生成实体/关系候选，结果必须再进入人工审核。"""

    def __init__(self, chat: Any):
        self.chat = chat
        self.model = str(getattr(chat, "model", "") or "")

    def extract(self, *, source_text: str, source: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """从单条 FAQ 或文档切片抽取 KG 候选，关键约束是只返回待审核结构化结果。"""
        response = self.chat.complete(self._system_prompt(), self._user_prompt(source_text, source))
        return parse_kg_extraction_response(
            self._strip_json_fence(response),
            source_text=source_text,
            source=source,
        )

    def resolve_document_entities(
        self,
        *,
        entities: list[dict[str, Any]],
    ) -> dict[str, list[list[str]]]:
        """仅让模型分组已存在 local entity ID，不接受 canonical/fact 输出。"""
        response = self.chat.complete(
            self._entity_resolution_system_prompt(),
            self.entity_resolution_user_prompt(entities),
        )
        return parse_document_entity_resolution_response(
            self._strip_json_fence(response),
            entities=entities,
        )

    @staticmethod
    def entity_resolution_user_prompt(entities: list[dict[str, Any]]) -> str:
        """只投影 resolution 必需实体字段，禁止把证据、关系或隐藏支持项送入模型。"""
        if not isinstance(entities, list):
            raise TypeError("entities must be an array")
        projected: list[dict[str, Any]] = []
        for index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                raise TypeError(f"entities[{index}] must be an object")
            projected.append(
                {
                    "local_entity_id": entity["local_entity_id"],
                    "name": entity["name"],
                    "entity_type": entity["entity_type"],
                    "aliases": entity["aliases"],
                    "description": entity["description"],
                }
            )
        candidates = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "\n".join(
            [
                "请只把代表同一现实对象、且 entity_type 完全相同的 local_entity_id 分组。",
                "未列出的 ID 保持独立；每个 group 至少两个 ID。",
                "候选实体：",
                candidates,
            ]
        )

    @staticmethod
    def _entity_resolution_system_prompt() -> str:
        """约束 resolution 只返回已有 local ID 的互斥等价组。"""
        return "\n".join(
            [
                "你是单文档实体消歧助手。",
                "只输出 JSON 对象，顶层唯一字段为 groups。",
                "groups 是 local_entity_id 字符串数组的数组，每个 ID 至多出现一次。",
                "不得新增、改写或删除 ID，不得跨 entity_type 分组。",
                "不得输出 canonical name、实体、关系、证据、解释或其他字段。",
            ]
        )

    @staticmethod
    def _system_prompt() -> str:
        """约束模型使用固定客服领域 schema，避免自由造类型或事实。"""
        return "\n".join(
            [
                "你是客服知识图谱抽取助手。",
                "只从来源文本中抽取明确出现或可由文本直接支持的实体、关系和证据。",
                "不要补充来源文本没有支持的事实，不要输出客户隐私、密钥、token、一次性账号密码。",
                "实体类型只能使用：product_platform_module, feature_ui_action, error_symptom, process_task_object, role_permission_channel, condition_policy。",
                "关系类型只能使用：belongs_to, requires, causes, resolves_by, blocked_by, available_for, escalate_when。",
                "每个实体和关系都必须带 evidence 数组，每条 evidence 必须包含 excerpt。",
                "excerpt 必须逐字复制来源文本中的连续子串，不得改写、概括、省略或规范化标点空白。",
                "输出 JSON 对象，不要输出 Markdown。",
                "JSON 顶层字段为 entities 和 relations。",
                "entities 每项包含 name, entity_type, aliases, description, confidence, evidence。",
                "relations 每项包含 head, head_type, relation_type, tail, tail_type, description, confidence, evidence。",
            ]
        )

    @staticmethod
    def _user_prompt(source_text: str, source: dict[str, Any]) -> str:
        """构造来源提示，关键约束是明确来源信息和原文边界。"""
        source_title = str(source.get("source_title") or "").strip()
        section_path = " > ".join(str(item) for item in source.get("section_path") or [])
        parts = ["请从以下来源文本抽取知识图谱候选。"]
        if source_title:
            parts.append(f"来源标题：{source_title}")
        if section_path:
            parts.append(f"章节：{section_path}")
        page_start = source.get("page_start")
        page_end = source.get("page_end")
        if page_start is not None or page_end is not None:
            start_text = str(page_start) if page_start is not None else ""
            end_text = str(page_end) if page_end is not None else ""
            parts.append(f"页码：{start_text}-{end_text}".strip("-"))
        parts.extend(["", "来源文本：", str(source_text or "")])
        return "\n".join(parts)

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        """仅提取首尾完整 JSON fence；非围栏响应原样交给 json.loads 拒绝。"""
        stripped = str(text or "").strip()
        if not stripped.startswith("```") or not stripped.endswith("```"):
            return stripped
        fenced_body = stripped[3:].lstrip()
        if fenced_body[:4].lower() == "json" and (
            len(fenced_body) == 4 or fenced_body[4].isspace()
        ):
            fenced_body = fenced_body[4:].lstrip()
        fence_end = fenced_body.rfind("```")
        if fence_end == -1:
            return stripped
        return fenced_body[:fence_end].strip()
