from __future__ import annotations

import io
import csv
import json
import re
import time
import uuid
import zipfile
from html import unescape
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from cyclops.chunking import (
    StructuredChunk,
    attach_media_context_to_blocks,
    extract_pdf_positions,
    ragflow_naive_merge_blocks,
)

MINERU_BATCH_FILE_URL = "https://mineru.net/api/v4/file-urls/batch"
MINERU_BATCH_RESULT_URL_TEMPLATE = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"
MINERU_PAGE_CHROME_TYPES = {
    "discarded",
    "footer",
    "header",
    "page_aside_text",
    "page_footer",
    "page_header",
    "page_number",
}
MINERU_CONTENT_TYPES = {
    "code",
    "equation",
    "equation_interline",
    "image",
    "list",
    "table",
    "text",
    "title",
}
SUPPORTED_CHUNKER_TYPES = {"manual", "naive", "qa", "table"}


class MineruParseError(RuntimeError):
    """表示 MinerU 服务没有返回可保存到导入审核流程的解析结果。"""


@dataclass(frozen=True)
class ParsedBlock:
    """表示外部文档解析器输出的统一文本块，关键约束是必须可追溯来源。"""

    text: str
    block_type: str
    page_number: int | None
    section_title: str | None
    evidence: dict[str, Any]
    position_tag: str | None = None


@dataclass(frozen=True)
class MineruParseStatus:
    """表示 MinerU 单个文件的批量解析状态，关键约束是不包含用户 Token。"""

    batch_id: str
    file_name: str
    state: str
    progress: dict[str, Any]
    result: dict[str, Any]
    error: str | None = None
    zip_url: str | None = None


class MineruClient:
    """封装 MinerU 文档解析 API，避免解析实现侵入管理后台。"""

    def __init__(
        self,
        *,
        api_token: str | None = None,
        batch_file_url: str = MINERU_BATCH_FILE_URL,
        batch_result_url_template: str = MINERU_BATCH_RESULT_URL_TEMPLATE,
        timeout_seconds: int = 600,
        use_kb_packager: bool = True,
        asset_output_dir: str | Path | None = None,
        session: Any | None = None,
    ):
        self.api_token = api_token.strip() if api_token else None
        self.batch_file_url = batch_file_url.strip().rstrip("/")
        self.batch_result_url_template = batch_result_url_template.strip().rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.use_kb_packager = use_kb_packager
        self.asset_output_dir = Path(asset_output_dir) if asset_output_dir else None
        self.session = session or requests.Session()

    def parse_file(self, path: str | Path) -> list[ParsedBlock]:
        """提交本地文件给 MinerU，并把不同 API 响应统一为 ParsedBlock。"""
        file_path = Path(path)
        if not file_path.exists():
            raise MineruParseError(f"input file does not exist: {file_path}")

        payload = self._parse_standard_file(file_path)
        return extract_blocks_from_mineru_payload(
            payload,
            source_file=file_path.name,
            use_kb_packager=self.use_kb_packager,
        )

    def start_file(self, path: str | Path) -> MineruParseStatus:
        """提交本地文件并完成签名上传，返回后续轮询需要的批次信息。"""
        file_path = Path(path)
        if not file_path.exists():
            raise MineruParseError(f"input file does not exist: {file_path}")
        if not self.api_token:
            raise MineruParseError("MINERU_API_TOKEN is required for standard mode")
        response = self.session.post(
            self.batch_file_url,
            headers=self._auth_headers(),
            json={
                "enable_formula": True,
                "enable_table": True,
                "language": "ch",
                "files": [{"name": file_path.name, "data_id": file_path.name}],
                "model_version": "vlm",
            },
            timeout=30,
        )
        data = _mineru_success_data(_response_json(response))
        batch_id = _required_string(data, "batch_id")
        upload_url = _extract_upload_url(data, file_path.name)
        self._upload_signed_file(file_path, upload_url)
        return MineruParseStatus(
            batch_id=batch_id,
            file_name=file_path.name,
            state="waiting-file",
            progress={},
            result={},
        )

    def get_task_status(self, batch_id: str, file_name: str) -> MineruParseStatus:
        """查询 MinerU 批量任务中某个文件的实时状态，供前端轮询展示。"""
        if not self.api_token:
            raise MineruParseError("MINERU_API_TOKEN is required for standard mode")
        response = self.session.get(
            self._batch_result_url(batch_id=batch_id),
            headers=self._auth_headers(),
            timeout=30,
        )
        data = _mineru_success_data(_response_json(response))
        result = _select_extract_result(data, file_name)
        state = str(result.get("state") or result.get("status") or "pending").lower()
        progress = _extract_progress(result)
        error_value = result.get("err_msg") or result.get("error") or result.get("message")
        error = str(error_value) if error_value else None
        zip_url = _find_first_text(result, ("full_zip_url", "zip_url", "fullZipUrl"))
        return MineruParseStatus(
            batch_id=batch_id,
            file_name=file_name,
            state=state,
            progress=progress,
            result=result,
            error=error,
            zip_url=zip_url,
        )

    def download_task_result(self, status: MineruParseStatus) -> dict[str, Any]:
        """下载已完成任务的结果 zip，关键约束是只接受带结果地址的 done 状态。"""
        if not status.zip_url:
            raise MineruParseError("MinerU standard result missing full_zip_url")
        return self._download_result_zip(status.zip_url)

    def _parse_standard_file(self, file_path: Path) -> dict[str, Any]:
        """调用 MinerU 精准 API，关键约束是必须有 Token 且优先读取结构化 JSON。"""
        task = self.start_file(file_path)
        status = self._wait_standard_result(task.batch_id, task.file_name)
        return self.download_task_result(status)

    def _upload_signed_file(self, file_path: Path, upload_url: str) -> None:
        """把本地文件 PUT 到 MinerU 返回的签名地址，不携带业务 Token。"""
        with file_path.open("rb") as file_obj:
            response = self.session.put(upload_url, data=file_obj, timeout=self.timeout_seconds)
        _check_response_status(response)

    def _wait_standard_result(self, batch_id: str, file_name: str) -> MineruParseStatus:
        """轮询精准 API 批量任务，返回当前上传文件对应的结果项。"""
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            status = self.get_task_status(batch_id, file_name)
            if status.state in {"done", "finished", "success", "completed"}:
                return status
            if status.state in {"failed", "error", "cancelled", "canceled"}:
                error = status.error or status.result
                raise MineruParseError(f"MinerU standard task failed: {error}")
            time.sleep(2)
        raise MineruParseError(f"MinerU standard task timeout: {batch_id}")

    def _download_text(self, url: str) -> str:
        """下载 MinerU Markdown 文本，避免把空结果送入审核流程。"""
        response = self.session.get(url, timeout=self.timeout_seconds)
        _check_response_status(response)
        text = getattr(response, "text", "") or ""
        if not text and getattr(response, "content", None):
            text = response.content.decode("utf-8")
        if not text.strip():
            raise MineruParseError("MinerU markdown download is empty")
        return text

    def _download_result_zip(self, url: str) -> dict[str, Any]:
        """下载并读取 MinerU 结果 zip，优先返回 content_list，Markdown 作为兜底。"""
        response = self.session.get(url, timeout=self.timeout_seconds)
        _check_response_status(response)
        content = getattr(response, "content", b"")
        if not content:
            raise MineruParseError("MinerU result zip is empty")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return _payload_from_result_zip(archive, asset_output_dir=self.asset_output_dir)
        except zipfile.BadZipFile as exc:
            raise MineruParseError("MinerU result is not a valid zip") from exc

    def _auth_headers(self) -> dict[str, str]:
        """构造 MinerU 精准 API 鉴权头，Token 不进入日志和返回值。"""
        return {"Authorization": f"Bearer {self.api_token}"}

    def _batch_result_url(self, **values: str) -> str:
        """按官方批量结果 URL 模板替换 batch_id，避免用户配置 URL。"""
        try:
            return self.batch_result_url_template.format(**values)
        except KeyError as exc:
            missing = str(exc).strip("'")
            raise MineruParseError(f"MinerU batch result url missing {{{missing}}}") from exc


def extract_blocks_from_mineru_payload(
    payload: dict[str, Any],
    *,
    source_file: str,
    use_kb_packager: bool = False,
) -> list[ParsedBlock]:
    """从 MinerU 多种可能响应结构中抽取统一文本块。"""
    if use_kb_packager:
        blocks = package_mineru_payload_for_kb(payload, source_file=source_file)
        if blocks:
            return blocks

    root = _unwrap_payload(payload)
    items = _find_first_list(root, ("content_list", "contents", "blocks"))
    if items:
        blocks = _blocks_from_content_list(items, source_file=source_file)
    else:
        markdown = _find_first_text(root, ("md_content", "markdown", "md", "content"))
        blocks = _blocks_from_markdown(markdown, source_file=source_file) if markdown else []
    if not blocks:
        raise MineruParseError("MinerU returned no parseable text")
    return blocks


def package_mineru_payload_for_kb(payload: dict[str, Any], *, source_file: str) -> list[ParsedBlock]:
    """按 mineru-kb-packager 思路把 MinerU 结构化输出整理为知识库块。"""
    root = _unwrap_payload(payload)
    content_data = _find_first_raw_list(
        root,
        ("content_list_v2", "content_list", "contents", "blocks"),
    )
    pages = _normalize_mineru_pages(content_data)
    if not pages:
        return []

    blocks: list[ParsedBlock] = []
    section_path: list[str] = []
    current_section_title = ""
    skip_current_section = False

    for page_number, page_blocks in enumerate(pages, start=1):
        text_buffer: list[ParsedBlock] = []
        for block in page_blocks:
            block_type = str(block.get("type") or block.get("block_type") or "").strip()
            normalized_type = _kb_block_type(block_type)

            if normalized_type == "title":
                blocks.extend(_merge_packager_text_buffer(text_buffer))
                text_buffer = []
                title = _clean_section_title(_extract_kb_text(block, block_type))
                level = _extract_title_level(block)
                if title:
                    while len(section_path) >= level:
                        section_path.pop()
                    section_path.append(title)
                    current_section_title = " > ".join(section_path)
                    skip_current_section = _should_skip_section(title)
                continue

            if normalized_type == "noise":
                continue
            if skip_current_section and normalized_type in {"text", "table"}:
                continue

            text = _extract_kb_text(block, block_type).strip()
            asset_paths = _mineru_asset_paths(block)
            # 图 / 表 / 公式即便没有 caption 也要保留：MinerU 经常返回无标题的截图，
            # 关键内容在 asset_paths 里，丢掉就等于解析丢图。只有纯 text 类型没文字才跳过。
            if not text and not asset_paths:
                continue

            position_tag = _ragflow_position_tag(block)
            evidence = {
                "source_file": source_file,
                "page_number": page_number,
                "block_type": normalized_type,
                "layout_type": block_type,
                "doc_type_kwd": _ragflow_doc_type(normalized_type, block),
                "postprocess": "mineru-kb-packager",
            }
            if position_tag:
                evidence["position_tag"] = position_tag
            if asset_paths:
                evidence["asset_paths"] = asset_paths
            if normalized_type == "table":
                table_html = _mineru_table_html(block)
                if table_html:
                    evidence["table_html"] = table_html
            parsed = ParsedBlock(
                text=text,
                block_type=normalized_type,
                page_number=page_number,
                section_title=current_section_title or None,
                evidence=evidence,
                position_tag=position_tag,
            )

            if normalized_type == "text":
                text_buffer.append(parsed)
            else:
                blocks.extend(_merge_packager_text_buffer(text_buffer))
                text_buffer = []
                blocks.append(parsed)
        blocks.extend(_merge_packager_text_buffer(text_buffer))

    return blocks


def build_import_chunks_from_blocks(
    file_id: str,
    blocks: list[ParsedBlock],
    *,
    max_chars: int = 6000,
    chunk_token_num: int | None = None,
    delimiter: str = "\n。；！？",
    overlapped_percent: int = 0,
    children_delimiter: str = "",
    table_context_size: int = 0,
    image_context_size: int = 0,
    chunker_type: str = "naive",
) -> list[dict[str, Any]]:
    """按指定 RAGFlow-style chunker 把解析块合并为导入审核切块。"""
    normalized = []
    for block in blocks:
        if not isinstance(block, ParsedBlock):
            raise TypeError("blocks must contain ParsedBlock")
        # 保留两类块：①有文字的块；②虽然没文字但带资产路径的块（image/table/equation）。
        # 后者是 MinerU 对无 caption 截图的常见输出 —— 丢掉就等于丢图。
        has_text = bool(block.text.strip())
        has_assets = bool((block.evidence or {}).get("asset_paths"))
        if has_text or has_assets:
            normalized.append(block)
    if not normalized:
        raise MineruParseError("MinerU returned no parseable text")

    source_blocks = attach_media_context_to_blocks(
        [_source_block_payload(block) for block in normalized],
        table_context_size=table_context_size,
        image_context_size=image_context_size,
    )
    selected_chunker = _normalize_chunker_type(chunker_type)
    if selected_chunker == "table":
        merged_chunks = _table_chunks_from_blocks(source_blocks)
    elif selected_chunker == "qa":
        merged_chunks = _qa_chunks_from_blocks(source_blocks)
    elif selected_chunker == "manual":
        merged_chunks = _manual_chunks_from_blocks(source_blocks)
    else:
        merged_chunks = ragflow_naive_merge_blocks(
            source_blocks,
            chunk_token_num=chunk_token_num or max_chars,
            delimiter=delimiter,
            overlapped_percent=overlapped_percent,
        )
    return [
        _build_import_chunk(
            file_id,
            index,
            chunk,
            children_delimiter=children_delimiter,
        )
        for index, chunk in enumerate(merged_chunks, start=1)
    ]


def _normalize_chunker_type(chunker_type: str | None) -> str:
    """规范化 chunker 名称，关键约束是未知值不能静默走错规则。"""
    value = str(chunker_type or "naive").strip().lower() or "naive"
    if value not in SUPPORTED_CHUNKER_TYPES:
        raise MineruParseError(f"Unsupported chunker_type: {chunker_type}")
    return value


def _table_chunks_from_blocks(blocks: list[dict[str, Any]]) -> list[StructuredChunk]:
    """按 RAGFlow table.py 口径把表格数据行转换为一行一个 chunk。"""
    chunks: list[StructuredChunk] = []
    for block in blocks:
        if str(block.get("block_type") or "").lower() not in {"table", "table_row"}:
            continue
        headers, rows = _table_headers_and_rows(block)
        if not headers:
            continue
        _ensure_unique_headers(headers)
        evidence = block.get("evidence") if isinstance(block.get("evidence"), dict) else {}
        for row_number, values in enumerate(rows, start=1):
            if not any(str(value).strip() for value in values):
                continue
            if len(values) != len(headers):
                continue
            row_fields = [
                (str(header).strip(), str(value).strip())
                for header, value in zip(headers, values, strict=False)
                if str(value).strip()
            ]
            if not row_fields:
                continue
            text = "\n".join(f"- {header}: {value}" for header, value in row_fields)
            source_block = {
                **block,
                "text": text,
                "block_type": "table_row",
                "evidence": {
                    **evidence,
                    "headers": headers,
                    "row_index": evidence.get("row_index") or row_number,
                },
            }
            chunks.append(
                StructuredChunk(
                    text=text,
                    source_blocks=[source_block],
                    section_path=_section_path_from_block(block),
                    page_start=_block_page(block),
                    page_end=_block_page(block),
                    block_type="table_row",
                    source_offsets=_source_offsets_for_blocks(
                        [source_block],
                        chunker={"type": "table"},
                        extra={
                            "sheet_name": evidence.get("sheet_name"),
                            "row_index": evidence.get("row_index") or row_number,
                            "headers": headers,
                            "field_map": {str(header): str(header) for header in headers},
                            "table_html": evidence.get("table_html"),
                        },
                    ),
                )
            )
    if not chunks:
        raise MineruParseError("table chunker found no table rows")
    return chunks


def _table_headers_and_rows(block: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    """从结构化证据或表格文本提取 headers/rows，优先保留 RAGFlow 行级语义。"""
    evidence = block.get("evidence") if isinstance(block.get("evidence"), dict) else {}
    headers = [str(item).strip() for item in evidence.get("headers", []) if str(item).strip()]
    raw_rows = evidence.get("rows")
    if headers and isinstance(raw_rows, list):
        rows = [
            [str(value).strip() for value in row]
            for row in raw_rows
            if isinstance(row, (list, tuple))
        ]
        return headers, rows

    lines = [line.strip() for line in str(block.get("text") or "").splitlines() if line.strip()]
    parsed_rows = [_split_table_row(line) for line in lines]
    parsed_rows = [row for row in parsed_rows if row]
    if len(parsed_rows) < 2:
        return [], []
    return parsed_rows[0], parsed_rows[1:]


def _split_table_row(line: str) -> list[str]:
    """按 RAGFlow 表格行输入常见分隔符拆字段，支持 pipe/tab/csv。"""
    text = str(line or "").strip().strip("|")
    if not text:
        return []
    if "|" in text:
        return [part.strip() for part in text.split("|")]
    if "\t" in text:
        return [part.strip() for part in text.split("\t")]
    try:
        return [part.strip() for part in next(csv.reader([text]))]
    except csv.Error:
        return [text]


def _ensure_unique_headers(headers: list[str]) -> None:
    """检查表头唯一性，避免像 RAGFlow 一样静默覆盖字段。"""
    clean = [header for header in headers if header]
    if len(clean) != len(set(clean)):
        raise MineruParseError(f"Duplicate table headers detected: {headers}")


def _qa_chunks_from_blocks(blocks: list[dict[str, Any]]) -> list[StructuredChunk]:
    """按 RAGFlow qa.py 口径生成一问一答 chunk，并处理坏行续写。"""
    chunks: list[StructuredChunk] = []
    current_question = ""
    current_answer = ""
    current_blocks: list[dict[str, Any]] = []
    for block in blocks:
        parsed = _qa_pair_from_block(block)
        if parsed is None:
            if current_question:
                current_answer = "\n".join(
                    part for part in (current_answer, str(block.get("text") or "").strip()) if part
                )
                current_blocks.append(block)
            continue
        if current_question:
            chunks.append(_qa_structured_chunk(current_question, current_answer, current_blocks))
        current_question, current_answer = parsed
        current_blocks = [block]
    if current_question:
        chunks.append(_qa_structured_chunk(current_question, current_answer, current_blocks))
    if not chunks:
        raise MineruParseError("qa chunker found no question-answer pairs")
    return chunks


def _qa_pair_from_block(block: dict[str, Any]) -> tuple[str, str] | None:
    """从 evidence 或两列文本中提取 Q/A；不合法行交给调用方按坏行处理。"""
    evidence = block.get("evidence") if isinstance(block.get("evidence"), dict) else {}
    question = str(evidence.get("question") or "").strip()
    answer = str(evidence.get("answer") or "").strip()
    if question:
        return question, answer

    text = str(block.get("text") or "").strip()
    if not text:
        return None
    if "\t" in text:
        parts = [part.strip() for part in text.split("\t")]
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]
    try:
        row = [part.strip() for part in next(csv.reader([text]))]
    except csv.Error:
        return None
    if len(row) == 2 and all(row):
        return row[0], row[1]
    return None


def _qa_structured_chunk(
    question: str,
    answer: str,
    blocks: list[dict[str, Any]],
) -> StructuredChunk:
    """把一个 RAGFlow Q/A pair 转成项目 import chunk，保留行号和来源块。"""
    text = f"问题：{question}\n回答：{answer}".strip()
    rows = [
        _optional_int((block.get("evidence") or {}).get("row_index"))
        for block in blocks
        if isinstance(block.get("evidence"), dict)
    ]
    rows = [row for row in rows if row is not None]
    return StructuredChunk(
        text=text,
        source_blocks=[dict(block) for block in blocks],
        section_path=_section_path_from_block(blocks[0]) if blocks else [],
        page_start=_min_page(blocks),
        page_end=_max_page(blocks),
        block_type="qa",
        source_offsets=_source_offsets_for_blocks(
            blocks,
            chunker={"type": "qa"},
            extra={
                "question": question,
                "answer": answer,
                "rows": rows or None,
            },
        ),
    )


def _manual_chunks_from_blocks(blocks: list[dict[str, Any]]) -> list[StructuredChunk]:
    """按 RAGFlow manual.py 思路优先按标题/章节连续聚合块。"""
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    current_key = ""
    current_blocks: list[dict[str, Any]] = []
    for block in blocks:
        key = _manual_group_key(block)
        if current_blocks and key != current_key:
            groups.append((current_key, current_blocks))
            current_blocks = []
        current_key = key
        current_blocks.append(block)
    if current_blocks:
        groups.append((current_key, current_blocks))

    chunks = []
    for group_key, group_blocks in groups:
        text = "\n".join(str(block.get("text") or "").strip() for block in group_blocks if str(block.get("text") or "").strip())
        if not text:
            continue
        chunks.append(
            StructuredChunk(
                text=text,
                source_blocks=[dict(block) for block in group_blocks],
                section_path=_section_path_from_title(group_key),
                page_start=_min_page(group_blocks),
                page_end=_max_page(group_blocks),
                block_type=_combined_block_type(group_blocks),
                source_offsets=_source_offsets_for_blocks(
                    group_blocks,
                    chunker={"type": "manual", "group": group_key},
                ),
            )
        )
    if not chunks:
        raise MineruParseError("manual chunker found no section chunks")
    return chunks


def _manual_group_key(block: dict[str, Any]) -> str:
    """读取 manual 聚合键，优先章节标题，缺失时用标题块正文兜底。"""
    title = str(block.get("section_title") or "").strip()
    if title:
        return title
    if str(block.get("block_type") or "").lower() == "title":
        text = str(block.get("text") or "").strip()
        if text:
            return text
    return "未分章节"


def _source_offsets_for_blocks(
    blocks: list[dict[str, Any]],
    *,
    chunker: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """汇总 chunker 元数据和来源证据，保持切片抽屉可追溯。"""
    offsets: dict[str, Any] = {"chunker": {key: value for key, value in chunker.items() if value}}
    position_tags = []
    pdf_positions = []
    for block in blocks:
        evidence = block.get("evidence") if isinstance(block.get("evidence"), dict) else {}
        position_tag = block.get("position_tag") or evidence.get("position_tag")
        if position_tag:
            position_tags.append(str(position_tag))
        for position in extract_pdf_positions(block):
            if position not in pdf_positions:
                pdf_positions.append(position)
    if position_tags:
        offsets["position_tags"] = position_tags
    if pdf_positions:
        offsets["pdf_positions"] = pdf_positions
    for key, value in (extra or {}).items():
        if value is not None and value != [] and value != {}:
            offsets[key] = value
    return offsets


def _section_path_from_block(block: dict[str, Any]) -> list[str]:
    """从来源块章节标题生成 section_path。"""
    return _section_path_from_title(str(block.get("section_title") or "").strip())


def _section_path_from_title(title: str) -> list[str]:
    """把 RAGFlow 风格标题路径转为项目 section_path。"""
    text = str(title or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(">") if part.strip()]


def _block_page(block: dict[str, Any]) -> int | None:
    """读取来源块页码，非法值返回 None。"""
    page = _optional_int(block.get("page_number"))
    if page is not None:
        return page
    evidence = block.get("evidence") if isinstance(block.get("evidence"), dict) else {}
    return _optional_int(evidence.get("page_number"))


def _optional_int(value: Any) -> int | None:
    """把证据里的行号/页码转成整数，非法值保持为空。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _min_page(blocks: list[dict[str, Any]]) -> int | None:
    """计算一组块的最小页码，供 import_chunks 页码展示。"""
    pages = [_block_page(block) for block in blocks]
    pages = [page for page in pages if page is not None]
    return min(pages) if pages else None


def _max_page(blocks: list[dict[str, Any]]) -> int | None:
    """计算一组块的最大页码，供 import_chunks 页码展示。"""
    pages = [_block_page(block) for block in blocks]
    pages = [page for page in pages if page is not None]
    return max(pages) if pages else None


def _combined_block_type(blocks: list[dict[str, Any]]) -> str:
    """归纳 manual 聚合后的块类型，多个类型返回 mixed。"""
    types = {
        str(block.get("block_type") or "").strip()
        for block in blocks
        if str(block.get("block_type") or "").strip()
    }
    if not types:
        return "text"
    if len(types) == 1:
        return next(iter(types))
    return "mixed"


def _check_response_status(response: Any) -> None:
    """检查 HTTP 状态码，避免非 JSON 下载响应被静默当作成功。"""
    if getattr(response, "status_code", 200) >= 400:
        text = getattr(response, "text", "")
        raise MineruParseError(f"MinerU request failed: {response.status_code} {text}")


def _response_json(response: Any) -> dict[str, Any]:
    """解析 HTTP 响应 JSON，并在非 2xx 状态时给出清晰错误。"""
    _check_response_status(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise MineruParseError("MinerU response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise MineruParseError("MinerU response must be a JSON object")
    return payload


def _parse_json_string(value: str) -> Any:
    """把 MinerU JSON 字符串转回结构，解析失败时返回原文本供 Markdown 兜底。"""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _mineru_success_data(payload: dict[str, Any]) -> dict[str, Any]:
    """校验 MinerU 业务 code，并返回 data 包装层里的业务数据。"""
    code = payload.get("code")
    if code is not None and code != 0:
        message = payload.get("msg") or payload.get("message") or payload
        raise MineruParseError(f"MinerU API returned error: {message}")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _required_string(payload: dict[str, Any], key: str) -> str:
    """读取必需字符串字段，缺失时直接暴露上游响应结构问题。"""
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise MineruParseError(f"MinerU response missing {key}")


def _extract_upload_url(payload: dict[str, Any], file_name: str) -> str:
    """从精准 API 批量响应中取出当前文件的签名上传地址。"""
    file_urls = payload.get("file_urls") or payload.get("urls")
    if not isinstance(file_urls, list) or not file_urls:
        raise MineruParseError("MinerU response missing file_urls")

    first_url = ""
    for item in file_urls:
        if isinstance(item, str) and item.strip():
            return item.strip()
        if not isinstance(item, dict):
            continue
        upload_url = item.get("upload_url") or item.get("file_url") or item.get("url")
        if not isinstance(upload_url, str) or not upload_url.strip():
            continue
        if not first_url:
            first_url = upload_url.strip()
        item_name = item.get("file_name") or item.get("name")
        if not item_name or str(item_name) == file_name:
            return upload_url.strip()
    if first_url:
        return first_url
    raise MineruParseError("MinerU response missing upload url")


def _select_extract_result(payload: dict[str, Any], file_name: str) -> dict[str, Any]:
    """从批量查询结果中选择当前文件，缺少文件名时退回唯一结果项。"""
    extract_result = payload.get("extract_result")
    if isinstance(extract_result, dict):
        return extract_result
    if not isinstance(extract_result, list):
        return payload

    first_result: dict[str, Any] | None = None
    for item in extract_result:
        if not isinstance(item, dict):
            continue
        if first_result is None:
            first_result = item
        if str(item.get("file_name") or "") == file_name:
            return item
    if first_result is not None:
        return first_result
    raise MineruParseError("MinerU extract_result is empty")


def _extract_progress(result: dict[str, Any]) -> dict[str, Any]:
    """读取 MinerU 动态解析进度，只保留 JSON 对象形式的页数进度。"""
    progress = result.get("extract_progress")
    if isinstance(progress, dict):
        return dict(progress)
    return {}


def _payload_from_result_zip(
    archive: zipfile.ZipFile,
    *,
    asset_output_dir: Path | None = None,
) -> dict[str, Any]:
    """从 MinerU 结果 zip 中优先读取内容列表，缺失时读取 full.md。"""
    names = [name for name in archive.namelist() if not name.endswith("/")]
    asset_root = _extract_mineru_zip_assets(archive, names, asset_output_dir)
    v2_names = [
        name
        for name in names
        if name.endswith("content_list_v2.json")
    ]
    content_names = v2_names or [
        name
        for name in names
        if name.endswith("_content_list.json")
        or name.endswith("content_list.json")
    ]
    if content_names:
        name = sorted(content_names)[0]
        raw = archive.read(name).decode("utf-8")
        data = json.loads(raw)
        if asset_root is not None:
            _rewrite_mineru_asset_paths(data, asset_root)
        key = "content_list_v2" if "content_list_v2" in name else "content_list"
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {key: data}
        raise MineruParseError("MinerU content list JSON has unsupported shape")

    markdown_names = [name for name in names if name.endswith("full.md") or name.endswith(".md")]
    if markdown_names:
        markdown = archive.read(sorted(markdown_names)[0]).decode("utf-8")
        return {"md_content": markdown}
    raise MineruParseError("MinerU result zip missing content_list.json or full.md")


def _extract_mineru_zip_assets(
    archive: zipfile.ZipFile,
    names: list[str],
    asset_output_dir: Path | None,
) -> Path | None:
    """把 MinerU zip 中的图片/表格资产安全解压到本地目录，JSON/Markdown 不落资产目录。"""
    if asset_output_dir is None:
        return None
    asset_output_dir.mkdir(parents=True, exist_ok=True)
    root = asset_output_dir.resolve()
    for name in names:
        if name.endswith((".json", ".md")):
            continue
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise MineruParseError(f"Unsafe MinerU asset path: {name}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))
    return root


def _rewrite_mineru_asset_paths(data: Any, asset_root: Path) -> None:
    """把 MinerU JSON 中的相对资产路径改成本地绝对路径，贴近 RAGFlow _read_output 行为。"""
    if isinstance(data, list):
        for item in data:
            _rewrite_mineru_asset_paths(item, asset_root)
        return
    if not isinstance(data, dict):
        return
    for key in ("img_path", "table_img_path", "equation_img_path"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            resolved = _resolve_mineru_asset_path(asset_root, value.strip())
            if resolved is not None:
                data[key] = str(resolved)
    for value in data.values():
        if isinstance(value, (dict, list)):
            _rewrite_mineru_asset_paths(value, asset_root)


def _resolve_mineru_asset_path(asset_root: Path, value: str) -> Path | None:
    """解析 MinerU 资产路径；直接路径不存在时按后缀匹配 zip 内嵌根目录。"""
    candidate = (asset_root / value).resolve()
    try:
        candidate.relative_to(asset_root)
    except ValueError:
        return None
    if candidate.exists():
        return candidate
    suffix = Path(value).as_posix().lstrip("/")
    for path in asset_root.rglob(Path(value).name):
        if path.as_posix().endswith(suffix):
            return path.resolve()
    return None


def _unwrap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """剥离常见 data/result 包装层，找到实际解析内容。"""
    current = payload
    for key in ("data", "result"):
        value = current.get(key)
        if isinstance(value, dict):
            current = value
    return current


def _find_first_list(payload: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """递归查找第一个内容块列表，兼容 MinerU 不同版本响应。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_first_list(value, keys)
            if found:
                return found
    return []


def _find_first_raw_list(payload: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    """递归查找原始列表，保留 content_list_v2 的按页嵌套结构。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_first_raw_list(value, keys)
            if found:
                return found
    return []


def _normalize_mineru_pages(content_data: list[Any]) -> list[list[dict[str, Any]]]:
    """把 MinerU content_list_v2 或扁平 content_list 统一为按页块列表。"""
    if not content_data:
        return []
    if all(isinstance(page, list) for page in content_data):
        return [
            [block for block in page if isinstance(block, dict)]
            for page in content_data
        ]

    pages: dict[int, list[dict[str, Any]]] = {}
    has_page_number = False
    for block in content_data:
        if not isinstance(block, dict):
            continue
        page_number = _extract_page_number(block) or 1
        has_page_number = has_page_number or page_number != 1
        pages.setdefault(page_number, []).append(block)
    if not pages:
        return []
    if not has_page_number:
        return [pages[1]]
    return [pages[key] for key in sorted(pages)]


def _find_first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """递归查找 Markdown 正文，作为 content_list 缺失时的兜底。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_first_text(value, keys)
            if found:
                return found
    return None


def _blocks_from_content_list(items: list[dict[str, Any]], *, source_file: str) -> list[ParsedBlock]:
    """把 MinerU content_list 条目转为统一块，并按 RAGFlow 方式保留位置 tag。"""
    blocks: list[ParsedBlock] = []
    current_section: str | None = None
    layout_counters: dict[str, int] = {}
    for item in items:
        block_type = str(item.get("type") or item.get("block_type") or "text").strip() or "text"
        if _is_ignored_mineru_block_type(block_type):
            continue
        text = _extract_item_text(item)
        asset_paths = _mineru_asset_paths(item)
        # 图 / 表 / 公式即便没有 caption 也要保留：MinerU 经常返回无标题的截图，
        # 关键内容在 asset_paths 里，丢掉就等于解析丢图。
        if not text and not asset_paths:
            continue
        page_number = _extract_page_number(item)
        position_tag = _ragflow_position_tag(item)
        if block_type == "title":
            current_section = text.splitlines()[0].strip()
        layout_type = re.sub(r"\s+", " ", block_type)
        layout_index = layout_counters.get(layout_type, 0)
        layout_counters[layout_type] = layout_index + 1
        doc_type = _ragflow_doc_type(layout_type, item)
        evidence = {
            "source_file": source_file,
            "page_number": page_number,
            "block_type": block_type,
            "layout_type": layout_type,
            "layoutno": f"{layout_type}-{layout_index}",
            "doc_type_kwd": doc_type,
        }
        if position_tag:
            evidence["position_tag"] = position_tag
        if asset_paths:
            evidence["asset_paths"] = asset_paths
        if block_type == "table":
            table_html = _mineru_table_html(item)
            if table_html:
                evidence["table_html"] = table_html
        blocks.append(
            ParsedBlock(
                text=text,
                block_type=block_type,
                page_number=page_number,
                section_title=current_section,
                evidence=evidence,
                position_tag=position_tag,
            )
        )
    return blocks


def _blocks_from_markdown(markdown: str, *, source_file: str) -> list[ParsedBlock]:
    """把 MinerU Markdown 输出按标题粗分块，保留最近标题作为章节。"""
    blocks: list[ParsedBlock] = []
    current_section: str | None = None
    buffer: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.startswith("#"):
            if buffer:
                blocks.append(_markdown_block("\n".join(buffer), source_file, current_section))
                buffer = []
            current_section = line.lstrip("#").strip() or current_section
            buffer.append(line)
            continue
        if line.strip():
            buffer.append(line)
    if buffer:
        blocks.append(_markdown_block("\n".join(buffer), source_file, current_section))
    return blocks


def _markdown_block(text: str, source_file: str, section_title: str | None) -> ParsedBlock:
    """构造 Markdown 兜底块，页码未知时只记录文件和章节。"""
    return ParsedBlock(
        text=text.strip(),
        block_type="markdown",
        page_number=None,
        section_title=section_title,
        evidence={"source_file": source_file, "page_number": None, "block_type": "markdown"},
    )


def _extract_item_text(item: dict[str, Any]) -> str:
    """从 MinerU 条目中抽取可读正文，兼容 text/html/table_body 等字段。"""
    block_type = str(item.get("type") or item.get("block_type") or "").strip()
    if _is_ignored_mineru_block_type(block_type):
        return ""
    if block_type == "table":
        return _extract_mineru_table_section(item)
    if block_type == "image":
        return "\n".join(
            part
            for part in (
                _content_items_text(item.get("image_caption", [])),
                _content_items_text(item.get("image_footnote", [])),
            )
            if part
        ).strip()
    if block_type == "equation":
        value = item.get("text")
        return _sanitize_mineru_inline_text(str(value)) if value else ""
    if block_type == "code":
        return "\n".join(
            part
            for part in (
                str(item.get("code_body") or "").strip(),
                _content_items_text(item.get("code_caption", [])),
            )
            if part
        ).strip()
    if block_type == "list":
        list_items = item.get("list_items", [])
        if isinstance(list_items, list):
            return "\n".join(str(value).strip() for value in list_items if str(value).strip())
        return _content_items_text(list_items)

    for key in ("text", "content", "md", "html"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _sanitize_mineru_inline_text(value)
    table_body = item.get("table_body")
    if isinstance(table_body, str) and table_body.strip():
        return _sanitize_mineru_inline_text(table_body)
    return ""


def _extract_mineru_table_section(item: dict[str, Any]) -> str:
    """按 RAGFlow MinerU table 转 section 方式拼接表格正文、标题和脚注。"""
    table_body = str(item.get("table_body") or "").strip()
    if table_body and _looks_like_html_table(table_body):
        body_text = _table_html_to_text(table_body)
    else:
        body_text = _sanitize_mineru_inline_text(table_body)
    parts = [
        body_text,
        _content_items_text(item.get("table_caption", [])),
        _content_items_text(item.get("table_footnote", [])),
    ]
    text = "\n".join(part for part in parts if part).strip()
    if not text and isinstance(item.get("text"), str):
        text = _sanitize_mineru_inline_text(item["text"])
    return text or "FAILED TO PARSE TABLE"


def _mineru_table_html(item: dict[str, Any]) -> str:
    """从 MinerU 表格块拿原始 HTML，前端富预览直接渲染表格。

    - v1 扁平：顶层 `table_body`
    - v2 嵌套：`content.html`
    """
    if not isinstance(item, dict):
        return ""
    table_body = item.get("table_body")
    if isinstance(table_body, str) and table_body.strip():
        return table_body.strip()
    content = item.get("content")
    if isinstance(content, dict):
        html = content.get("html")
        if isinstance(html, str) and html.strip():
            return html.strip()
    return ""


def _mineru_asset_paths(item: dict[str, Any]) -> dict[str, str]:
    """保留 MinerU 图片、表格、公式资产路径，供后续证据或预览使用。

    兼容 MinerU 两种结果格式（实测 2026-05 vlm 模式）：
    - content_list.json（扁平）：顶层 `img_path` / `table_img_path` / `equation_img_path`
    - content_list_v2.json（kb-packager 默认走这个）：所有视觉资产都在 `content.image_source.path`
      这个通用字段下，含义取决于父块 `type`（image/table/equation）
    """
    assets: dict[str, str] = {}
    block_type = str(item.get("type") or item.get("block_type") or "").strip().lower()
    if block_type == "table":
        nested_primary_target = "table_img_path"
    elif block_type in {"equation", "equation_interline"}:
        nested_primary_target = "equation_img_path"
    else:
        nested_primary_target = "img_path"

    def collect_from(node: Any) -> None:
        if not isinstance(node, dict):
            return
        # 扁平结构：顶层直接是 img_path 等字段；MinerU v1 里 table/equation 块
        # 也用通用的 img_path 存截图，所以按父块 type 把通用字段映射到合适 key
        generic_image_keys = ("img_path", "image_path")
        explicit_aliases = {
            "table_img_path": "table_img_path",
            "table_image_path": "table_img_path",
            "equation_img_path": "equation_img_path",
            "equation_image_path": "equation_img_path",
        }
        for src_key, target_key in explicit_aliases.items():
            if target_key in assets:
                continue
            value = node.get(src_key)
            if isinstance(value, str) and value.strip():
                assets[target_key] = value.strip()
        for src_key in generic_image_keys:
            if nested_primary_target in assets:
                continue
            value = node.get(src_key)
            if isinstance(value, str) and value.strip():
                assets[nested_primary_target] = value.strip()
        # v2 嵌套结构：image_source 是 v2 里所有可视化资产的通用字段名，
        # 真正含义取决于父块 type；table_source / equation_source 是兜底别名（若以后版本调整）
        nested_aliases = {
            "image_source": nested_primary_target,
            "table_source": "table_img_path",
            "equation_source": "equation_img_path",
        }
        for sub_key, target_key in nested_aliases.items():
            if target_key in assets:
                continue
            sub = node.get(sub_key)
            if isinstance(sub, dict):
                path_value = sub.get("path") or sub.get("img_path") or sub.get("image_path")
                if isinstance(path_value, str) and path_value.strip():
                    assets[target_key] = path_value.strip()

    collect_from(item)
    if isinstance(item, dict):
        collect_from(item.get("content"))
    return assets


def _ragflow_doc_type(layout_type: str, item: dict[str, Any]) -> str:
    """按 RAGFlow flow parser 的 layout_type 到 doc_type_kwd 映射。"""
    layout = layout_type.strip().lower()
    if layout == "table":
        return "table"
    if layout in {"figure", "image"}:
        return "image"
    if item.get("image") is not None and not layout:
        return "image"
    return "text"


def _extract_kb_text(block: dict[str, Any], block_type: str) -> str:
    """按 mineru-kb-packager 的结构化字段优先级抽取块文本。"""
    if block.get("text"):
        return str(block["text"]).strip()

    content = block.get("content")
    if not isinstance(content, dict):
        return _extract_item_text(block)

    if block_type == "title":
        return _content_items_text(content.get("title_content", []))
    if block_type == "paragraph":
        return _content_items_text(content.get("paragraph_content", []))
    if block_type == "list":
        lines = []
        for item in content.get("list_items", []):
            if isinstance(item, dict):
                text = _content_items_text(item.get("item_content", []))
                if text:
                    lines.append(text)
        return "\n".join(lines)
    if block_type == "table":
        return _extract_kb_table_text(content)
    if block_type == "image":
        caption = _content_items_text(content.get("image_caption", []))
        footnote = _content_items_text(content.get("image_footnote", []))
        return "\n".join(item for item in (caption, footnote) if item)
    if block_type == "equation_interline":
        return str(content.get("math_content", "")).strip()
    return _extract_item_text(block)


def _content_items_text(items: Any) -> str:
    """从 MinerU content item 列表中抽取 text/equation_inline 文本。"""
    if isinstance(items, str):
        return _sanitize_mineru_inline_text(items)
    if not isinstance(items, list):
        return ""
    texts = []
    for item in items:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("content") or item.get("text")
        if value:
            texts.append(str(value))
    return _sanitize_mineru_inline_text("".join(texts))


def _extract_kb_table_text(content: dict[str, Any]) -> str:
    """把 MinerU 表格内容转成适合检索的文本，长表后续按块合并策略处理。"""
    parts = []
    caption = _content_items_text(content.get("table_caption", []))
    if caption:
        parts.append(f"Table: {caption}")
    html = str(content.get("html", "") or "").strip()
    if html:
        table_text = _table_html_to_text(html)
        parts.append(table_text or _sanitize_mineru_inline_text(html))
    footnote = _content_items_text(content.get("table_footnote", []))
    if footnote:
        parts.append(f"Note: {footnote}")
    return "\n".join(parts).strip()


def _is_ignored_mineru_block_type(block_type: str) -> bool:
    """判断原始 MinerU 块是否应跳过，关键约束是只过滤正文候选、不改写页码证据规则。"""
    normalized = str(block_type or "").strip()
    if normalized in MINERU_PAGE_CHROME_TYPES:
        return True
    return normalized not in MINERU_CONTENT_TYPES


def _sanitize_mineru_inline_text(value: str) -> str:
    """清洗 MinerU 文本中的 HTML 噪音，同时保留中文尖括号等非 HTML 字面量。"""
    text = unescape(str(value or ""))
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</\s*(p|div|section|article|li|ul|ol|tr|table)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*/?\s*[A-Za-z][A-Za-z0-9:-]*(?:\s+[^<>]*)?\s*/?\s*>", "", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_html_table(value: str) -> bool:
    """识别 MinerU table_body 是否是 HTML 表格，避免普通 markdown 表格被误处理。"""
    return bool(re.search(r"<\s*table\b|<\s*tr\b|<\s*t[dh]\b", value, flags=re.I))


def _table_html_to_text(html: str) -> str:
    """把 HTML 表格转成行文本，供检索使用；原始 HTML 仍由 evidence.table_html 保留。"""
    rows: list[str] = []
    for raw_row in re.findall(r"<\s*tr\b[^>]*>(.*?)</\s*tr\s*>", html, re.S | re.I):
        cells = re.findall(r"<\s*t[dh]\b[^>]*>(.*?)</\s*t[dh]\s*>", raw_row, re.S | re.I)
        clean_cells = [_sanitize_mineru_inline_text(cell) for cell in cells]
        clean_cells = [cell for cell in clean_cells if cell]
        if clean_cells:
            rows.append(" | ".join(clean_cells))
    return "\n".join(rows).strip()


def _kb_block_type(block_type: str) -> str:
    """把 MinerU 块类型映射到知识库内容类型。"""
    mapping = {
        "paragraph": "text",
        "list": "text",
        "table": "table",
        "image": "figure",
        "equation_interline": "formula",
        "title": "title",
        "page_header": "noise",
        "page_footer": "noise",
        "page_number": "noise",
        "header": "noise",
        "footer": "noise",
        "page_aside_text": "noise",
        "text": "text",
    }
    return mapping.get(block_type, block_type or "text")


def _extract_title_level(block: dict[str, Any]) -> int:
    """读取标题层级，缺失或非法时按一级标题处理。"""
    content = block.get("content")
    value = content.get("level") if isinstance(content, dict) else block.get("level")
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _merge_packager_text_buffer(buffer: list[ParsedBlock]) -> list[ParsedBlock]:
    """合并同章节相邻短正文块，降低碎片化。"""
    if not buffer:
        return []
    text = "\n\n".join(block.text for block in buffer if block.text.strip()).strip()
    if not text:
        return []
    first = buffer[0]
    position_tags = [block.position_tag for block in buffer if block.position_tag]
    evidence = dict(first.evidence)
    if position_tags:
        evidence["position_tags"] = position_tags
    return [
        ParsedBlock(
            text=text,
            block_type="text",
            page_number=first.page_number,
            section_title=first.section_title,
            evidence=evidence,
            position_tag=position_tags[0] if position_tags else first.position_tag,
        )
    ]


def _clean_section_title(title: str) -> str:
    """清理章节标题，避免空白噪音影响 skip 判断和展示。"""
    return re.sub(r"\s+", " ", str(title or "")).strip()


def _should_skip_section(title: str) -> bool:
    """过滤目录、索引、修订历史等低价值章节。"""
    clean = _clean_section_title(title).lower()
    patterns = [
        r"^\s*contents\s*$",
        r"^\s*table of contents\s*$",
        r"^\s*list of figures\s*$",
        r"^\s*list of tables\s*$",
        r"^\s*rev\.\s*\w+",
        r"^\s*revision\s*history\s*$",
        r"^\s*document\s*history\s*$",
        r"^\s*change\s*history\s*$",
    ]
    return any(re.match(pattern, clean, re.I) for pattern in patterns)


def _extract_page_number(item: dict[str, Any]) -> int | None:
    """读取页码；MinerU 的 page_idx 按 0 起始时转换成 1 起始。"""
    if item.get("page_number") is not None:
        try:
            return int(item["page_number"])
        except (TypeError, ValueError):
            return None
    if item.get("page_idx") is not None:
        try:
            return int(item["page_idx"]) + 1
        except (TypeError, ValueError):
            return None
    return None


def _ragflow_position_tag(item: dict[str, Any]) -> str | None:
    """按 RAGFlow MinerUParser._line_tag 格式把页码和 bbox 编码成位置 tag。"""
    bbox = item.get("bbox")
    page_number = _extract_page_number(item)
    if page_number is None or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x0, top, x1, bottom = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if x0 > x1:
        x0, x1 = x1, x0
    if top > bottom:
        top, bottom = bottom, top
    return f"@@{page_number}\t{x0:.1f}\t{x1:.1f}\t{top:.1f}\t{bottom:.1f}##"


def _build_import_chunk(
    file_id: str,
    chunk_index: int,
    chunk: StructuredChunk,
    *,
    children_delimiter: str = "",
) -> dict[str, Any]:
    """构造 import_chunks 行，keywords 只用于审核扫描，不参与事实判断。"""
    keywords = _chunk_keywords(chunk.source_blocks)
    return {
        "id": f"chunk_{uuid.uuid4().hex[:12]}",
        "file_id": file_id,
        "chunk_index": chunk_index,
        "section_path": chunk.section_path,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "block_type": chunk.block_type,
        "source_offsets": chunk.source_offsets,
        "source_blocks": chunk.source_blocks,
        "children_delimiter": children_delimiter,
        "start_at": None,
        "end_at": None,
        "message_count": len(chunk.source_blocks),
        "keywords": json.dumps(keywords, ensure_ascii=False),
        "source_text": chunk.text,
        "status": "pending",
        "candidate_count": 0,
    }


def _source_block_payload(block: ParsedBlock) -> dict[str, Any]:
    """把 ParsedBlock 转成可落库 JSON，供后续 chunker 派生 child。"""
    evidence = dict(block.evidence)
    payload = {
        "text": block.text,
        "block_type": block.block_type,
        "page_number": block.page_number,
        "section_title": block.section_title,
        "evidence": evidence,
    }
    if block.position_tag:
        payload["position_tag"] = block.position_tag
    for key in ("layout_type", "layoutno", "doc_type_kwd", "asset_paths", "pdf_positions"):
        if key in evidence:
            payload[key] = evidence[key]
    return payload


def _chunk_keywords(blocks: Iterable[dict[str, Any]], limit: int = 6) -> list[str]:
    """从来源文件、章节和块类型提取轻量关键词，帮助导入审核列表扫描。"""
    values: list[str] = []
    for block in blocks:
        evidence = block.get("evidence") if isinstance(block.get("evidence"), dict) else {}
        section_title = block.get("section_title")
        block_type = block.get("block_type")
        source_file = evidence.get("source_file")
        if source_file:
            values.append(str(source_file))
        if section_title:
            values.append(str(section_title))
        if block_type:
            values.append(str(block_type))
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique[:limit]
