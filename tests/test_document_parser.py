import io
import json
import zipfile

import pytest

from cyclops.document_parser import (
    MineruClient,
    MineruParseError,
    MineruParseStatus,
    ParsedBlock,
    build_import_chunks_from_blocks,
    extract_blocks_from_mineru_payload,
    package_mineru_payload_for_kb,
)


def test_extract_blocks_from_mineru_content_list_preserves_page_and_type():
    """MinerU content_list 会被转成本项目统一块结构。"""
    payload = {
        "content_list": [
            {"type": "title", "text": "账号登录", "page_idx": 0},
            {"type": "text", "text": "用户无法登录时先检查账号状态。", "page_idx": 1},
            {"type": "table", "text": "原因 | 处理\n密码错误 | 重置密码", "page_idx": 1},
        ]
    }

    blocks = extract_blocks_from_mineru_payload(payload, source_file="manual.pdf")

    assert [(block.text, block.block_type, block.page_number, block.section_title) for block in blocks] == [
        ("账号登录", "title", 1, "账号登录"),
        ("用户无法登录时先检查账号状态。", "text", 2, "账号登录"),
        ("原因 | 处理\n密码错误 | 重置密码", "table", 2, "账号登录"),
    ]
    assert blocks[0].evidence["layoutno"] == "title-0"
    assert blocks[1].evidence["doc_type_kwd"] == "text"
    assert blocks[2].evidence["doc_type_kwd"] == "table"


def test_build_import_chunks_from_blocks_batches_text_with_evidence():
    """解析块进入导入审核前会把结构单独保存，审核正文不混入标签。"""
    blocks = [
        ParsedBlock(
            text="账号登录",
            block_type="title",
            page_number=1,
            section_title="账号登录",
            evidence={"source_file": "manual.pdf", "page_number": 1, "block_type": "title"},
        ),
        ParsedBlock(
            text="用户无法登录时先检查账号状态。",
            block_type="text",
            page_number=1,
            section_title="账号登录",
            evidence={"source_file": "manual.pdf", "page_number": 1, "block_type": "text"},
        ),
    ]

    chunks = build_import_chunks_from_blocks("imp_1", blocks, max_chars=1000)

    assert len(chunks) == 1
    assert chunks[0]["file_id"] == "imp_1"
    assert chunks[0]["chunk_index"] == 1
    assert "章节：账号登录" not in chunks[0]["source_text"]
    assert "页码：1" not in chunks[0]["source_text"]
    assert "类型：text" not in chunks[0]["source_text"]
    assert "账号登录" in chunks[0]["source_text"]
    assert "用户无法登录时先检查账号状态。" in chunks[0]["source_text"]
    assert "manual.pdf" in chunks[0]["keywords"]
    assert chunks[0]["section_path"] == ["账号登录"]
    assert chunks[0]["page_start"] == 1
    assert chunks[0]["page_end"] == 1
    assert chunks[0]["block_type"] == "mixed"
    assert chunks[0]["source_offsets"] == {}
    assert chunks[0]["source_blocks"] == [
        {
            "text": "账号登录",
            "block_type": "title",
            "page_number": 1,
            "section_title": "账号登录",
            "evidence": {"source_file": "manual.pdf", "page_number": 1, "block_type": "title"},
        },
        {
            "text": "用户无法登录时先检查账号状态。",
            "block_type": "text",
            "page_number": 1,
            "section_title": "账号登录",
            "evidence": {"source_file": "manual.pdf", "page_number": 1, "block_type": "text"},
        },
    ]


def test_build_import_chunks_from_blocks_rejects_dictionary_alias():
    """解析完成后的 chunker 只接受 ParsedBlock，不为测试或调用层保留字典别名。"""
    with pytest.raises(TypeError, match="blocks must contain ParsedBlock"):
        build_import_chunks_from_blocks(
            "imp_1",
            [{"text": "账号登录", "block_type": "title"}],
        )


def test_extract_blocks_from_mineru_content_list_preserves_ragflow_position_tag():
    """MinerU bbox 会按 RAGFlow 的 @@page 坐标 tag 保存到结构字段。"""
    payload = {
        "content_list": [
            {
                "type": "text",
                "text": "退款规则以订单状态为准。",
                "page_idx": 0,
                "bbox": [200, 80, 100, 160],
            }
        ]
    }

    blocks = extract_blocks_from_mineru_payload(payload, source_file="manual.pdf")
    chunks = build_import_chunks_from_blocks("imp_1", blocks, max_chars=1000)

    assert blocks[0].position_tag == "@@1\t100.0\t200.0\t80.0\t160.0##"
    assert blocks[0].evidence["position_tag"] == "@@1\t100.0\t200.0\t80.0\t160.0##"
    assert chunks[0]["source_offsets"] == {
        "position_tags": ["@@1\t100.0\t200.0\t80.0\t160.0##"],
        "pdf_positions": [[1, 100.0, 200.0, 80.0, 160.0]],
    }
    assert chunks[0]["source_blocks"][0]["position_tag"] == "@@1\t100.0\t200.0\t80.0\t160.0##"


def test_extract_blocks_from_mineru_content_list_transfers_ragflow_content_types():
    """MinerU table/image/equation/code/list 会按 RAGFlow _transfer_to_sections 口径转文本。"""
    payload = {
        "content_list": [
            {
                "type": "table",
                "table_body": "状态 | 处理\n待发货 | 可退款",
                "table_caption": ["退款规则"],
                "table_footnote": ["以订单状态为准"],
                "page_idx": 0,
                "table_img_path": "tables/t1.png",
            },
            {
                "type": "image",
                "image_caption": ["退款流程图"],
                "image_footnote": ["仅示意"],
                "page_idx": 0,
                "img_path": "images/i1.png",
            },
            {
                "type": "equation",
                "text": "refund = paid - fee",
                "page_idx": 0,
                "equation_img_path": "equations/e1.png",
            },
            {
                "type": "code",
                "code_body": "print('refund')",
                "code_caption": ["示例代码"],
                "page_idx": 0,
            },
            {"type": "list", "list_items": ["第一步", "第二步"], "page_idx": 0},
            {"type": "discarded", "text": "页脚噪音", "page_idx": 0},
        ]
    }

    blocks = extract_blocks_from_mineru_payload(
        payload,
        source_file="manual.pdf",
        use_kb_packager=False,
    )

    assert [block.block_type for block in blocks] == ["table", "image", "equation", "code", "list"]
    assert blocks[0].text == "状态 | 处理\n待发货 | 可退款\n退款规则\n以订单状态为准"
    assert blocks[0].evidence["asset_paths"] == {"table_img_path": "tables/t1.png"}
    assert blocks[1].text == "退款流程图\n仅示意"
    assert blocks[1].evidence["asset_paths"] == {"img_path": "images/i1.png"}
    assert blocks[2].text == "refund = paid - fee"
    assert blocks[2].evidence["asset_paths"] == {"equation_img_path": "equations/e1.png"}
    assert blocks[3].text == "print('refund')\n示例代码"
    assert blocks[4].text == "第一步\n第二步"

    chunks = build_import_chunks_from_blocks("imp_1", blocks, chunk_token_num=512)
    assert chunks[0]["source_blocks"][0]["doc_type_kwd"] == "table"
    assert chunks[0]["source_blocks"][0]["layoutno"] == "table-0"
    assert chunks[0]["source_blocks"][0]["asset_paths"] == {"table_img_path": "tables/t1.png"}


def test_extract_blocks_from_mineru_content_list_filters_page_chrome_and_unknown_types():
    """RAGFlow v0.26 口径下页眉页脚页码和未知块不应污染切片正文，但正文页码仍保留。"""
    payload = {
        "content_list": [
            {"type": "header", "text": "Online Edition for Part no. 123", "page_idx": 76},
            {"type": "text", "text": "打开和关闭", "page_idx": 76, "bbox": [10, 20, 100, 40]},
            {"type": "page_number", "text": "77", "page_idx": 76},
            {"type": "footer", "text": "BMW AG", "page_idx": 76},
            {"type": "sidebar", "text": "不支持的侧栏不应重复正文", "page_idx": 76},
        ]
    }

    blocks = extract_blocks_from_mineru_payload(
        payload,
        source_file="manual.pdf",
        use_kb_packager=False,
    )
    chunks = build_import_chunks_from_blocks("imp_1", blocks, max_chars=1000)

    assert [block.text for block in blocks] == ["打开和关闭"]
    assert blocks[0].page_number == 77
    assert chunks[0]["page_start"] == 77
    assert chunks[0]["page_end"] == 77
    assert "打开和关闭" in chunks[0]["source_text"]
    assert "Online Edition" not in chunks[0]["source_text"]
    assert "BMW AG" not in chunks[0]["source_text"]
    assert "不支持的侧栏" not in chunks[0]["source_text"]
    assert chunks[0]["source_offsets"]["pdf_positions"] == [[77, 10.0, 100.0, 20.0, 40.0]]


def test_extract_blocks_from_mineru_content_list_sanitizes_html_without_losing_raw_table():
    """HTML 标签应从正文清洗掉，原始表格 HTML 仍保留在 evidence 供前端预览。"""
    payload = {
        "content_list": [
            {
                "type": "table",
                "table_body": "<table><tr><th>状态</th><th>处理</th></tr><tr><td>待发货</td><td>可退款</td></tr></table>",
                "table_caption": ["&lt;退款规则&gt;"],
                "page_idx": 0,
            },
            {
                "type": "text",
                "text": "<p>用户&nbsp;无法登录<br/>先检查账号状态。</p>",
                "page_idx": 0,
            },
        ]
    }

    blocks = extract_blocks_from_mineru_payload(
        payload,
        source_file="manual.pdf",
        use_kb_packager=False,
    )

    assert blocks[0].text == "状态 | 处理\n待发货 | 可退款\n<退款规则>"
    assert blocks[0].evidence["table_html"].startswith("<table>")
    assert blocks[1].text == "用户 无法登录\n先检查账号状态。"
    assert "<td>" not in blocks[0].text
    assert "<p>" not in blocks[1].text


def test_mineru_client_standard_mode_extracts_zip_assets_to_evidence(tmp_path):
    """标准 API zip 里的图片资产会落到本地目录，并写入 MinerU block 证据。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr(
            "manual_content_list.json",
            json.dumps(
                [
                    {
                        "type": "image",
                        "image_caption": ["退款流程图"],
                        "page_idx": 0,
                        "img_path": "images/flow.png",
                    }
                ],
                ensure_ascii=False,
            ),
        )
        archive.writestr("images/flow.png", b"png-bytes")

    class FakeResponse:
        def __init__(self, payload=None, status_code=200, content=b""):
            self.payload = payload or {}
            self.status_code = status_code
            self.text = ""
            self.content = content

        def json(self):
            return self.payload

    class FakeSession:
        def post(self, url, **kwargs):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "batch_id": "batch_1",
                        "file_urls": [{"file_name": "manual.pdf", "upload_url": "https://oss.example/upload"}],
                    },
                }
            )

        def put(self, url, **kwargs):
            return FakeResponse(status_code=200)

        def get(self, url, **kwargs):
            if url.endswith("/batch_1"):
                return FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "extract_result": [
                                {
                                    "file_name": "manual.pdf",
                                    "state": "done",
                                    "full_zip_url": "https://cdn.example/result.zip",
                                }
                            ]
                        },
                    }
                )
            if url == "https://cdn.example/result.zip":
                return FakeResponse(content=zip_buffer.getvalue())
            raise AssertionError(url)

    blocks = MineruClient(
        api_token="mineru-token",
        timeout_seconds=1,
        asset_output_dir=tmp_path / "assets",
        use_kb_packager=False,
        session=FakeSession(),
    ).parse_file(source)

    asset_path = blocks[0].evidence["asset_paths"]["img_path"]
    assert asset_path.endswith("images/flow.png")
    assert (tmp_path / "assets" / "images" / "flow.png").read_bytes() == b"png-bytes"


def test_build_import_chunks_from_blocks_applies_table_context_window():
    """导入审核切片会在表格块上应用 RAGFlow 表格上下文窗口。"""
    blocks = [
        ParsedBlock(
            text="退款规则如下。",
            block_type="text",
            page_number=1,
            section_title="售后",
            evidence={"source_file": "manual.pdf", "page_number": 1, "block_type": "text"},
        ),
        ParsedBlock(
            text="状态 | 处理\n待发货 | 可退款",
            block_type="table",
            page_number=1,
            section_title="售后",
            evidence={"source_file": "manual.pdf", "page_number": 1, "block_type": "table"},
        ),
    ]

    chunks = build_import_chunks_from_blocks(
        "imp_1",
        blocks,
        chunk_token_num=128,
        table_context_size=100,
    )

    assert chunks[0]["source_blocks"][1]["context_above"] == "退款规则如下。"
    assert "退款规则如下。" in chunks[0]["source_blocks"][1]["text"]
    assert "待发货 | 可退款" in chunks[0]["source_text"]


def test_build_import_chunks_table_chunker_outputs_one_chunk_per_row_with_evidence():
    """table chunker 应按 RAGFlow table.py 口径把每个数据行变成一个审核切片。"""
    blocks = [
        ParsedBlock(
            text="商品 | 状态 | 处理\n订单A | 待发货 | 可退款\n订单B | 已签收 | 走售后",
            block_type="table",
            page_number=3,
            section_title="售后表",
            evidence={
                "source_file": "rules.xlsx",
                "sheet_name": "售后",
                "page_number": 3,
                "block_type": "table",
                "table_html": "<table><tr><th>商品</th><th>状态</th><th>处理</th></tr></table>",
            },
        )
    ]

    chunks = build_import_chunks_from_blocks("imp_table", blocks, chunker_type="table")

    assert [chunk["source_text"] for chunk in chunks] == [
        "- 商品: 订单A\n- 状态: 待发货\n- 处理: 可退款",
        "- 商品: 订单B\n- 状态: 已签收\n- 处理: 走售后",
    ]
    assert chunks[0]["block_type"] == "table_row"
    assert chunks[0]["page_start"] == 3
    assert chunks[0]["source_offsets"]["chunker"]["type"] == "table"
    assert chunks[0]["source_offsets"]["sheet_name"] == "售后"
    assert chunks[0]["source_offsets"]["row_index"] == 1
    assert chunks[0]["source_offsets"]["headers"] == ["商品", "状态", "处理"]
    assert chunks[0]["source_offsets"]["table_html"].startswith("<table>")
    assert chunks[0]["source_blocks"][0]["evidence"]["table_html"].startswith("<table>")


def test_build_import_chunks_qa_chunker_appends_malformed_rows_to_current_answer():
    """qa chunker 应复刻 RAGFlow txt/csv 坏行处理：已有问题后坏行追加到当前回答。"""
    blocks = [
        ParsedBlock(
            text="登录失败\t先检查账号状态",
            block_type="text",
            page_number=1,
            section_title="FAQ",
            evidence={"source_file": "faq.txt", "row_index": 1},
        ),
        ParsedBlock(
            text="如果仍失败，重置密码后重试",
            block_type="text",
            page_number=1,
            section_title="FAQ",
            evidence={"source_file": "faq.txt", "row_index": 2},
        ),
        ParsedBlock(
            text="怎么退款\t进入订单详情申请退款",
            block_type="text",
            page_number=2,
            section_title="FAQ",
            evidence={"source_file": "faq.txt", "row_index": 3},
        ),
    ]

    chunks = build_import_chunks_from_blocks("imp_qa", blocks, chunker_type="qa")

    assert [chunk["source_text"] for chunk in chunks] == [
        "问题：登录失败\n回答：先检查账号状态\n如果仍失败，重置密码后重试",
        "问题：怎么退款\n回答：进入订单详情申请退款",
    ]
    assert chunks[0]["block_type"] == "qa"
    assert chunks[0]["section_path"] == ["FAQ"]
    assert chunks[0]["source_offsets"]["chunker"]["type"] == "qa"
    assert chunks[0]["source_offsets"]["question"] == "登录失败"
    assert chunks[0]["source_offsets"]["answer"] == "先检查账号状态\n如果仍失败，重置密码后重试"
    assert chunks[0]["source_offsets"]["rows"] == [1, 2]
    assert chunks[1]["page_start"] == 2


def test_build_import_chunks_manual_chunker_groups_blocks_by_section_path():
    """manual chunker 应按 RAGFlow manual.py 思路优先保留标题层级和章节聚合。"""
    blocks = [
        ParsedBlock(
            text="账号登录",
            block_type="title",
            page_number=1,
            section_title="账号登录",
            evidence={"source_file": "manual.pdf", "layout_type": "title"},
        ),
        ParsedBlock(
            text="用户无法登录时先检查账号状态。",
            block_type="text",
            page_number=1,
            section_title="账号登录",
            evidence={"source_file": "manual.pdf", "layout_type": "text"},
        ),
        ParsedBlock(
            text="售后处理",
            block_type="title",
            page_number=2,
            section_title="售后处理",
            evidence={"source_file": "manual.pdf", "layout_type": "title"},
        ),
        ParsedBlock(
            text="待发货订单可以直接申请退款。",
            block_type="text",
            page_number=2,
            section_title="售后处理",
            evidence={"source_file": "manual.pdf", "layout_type": "text"},
        ),
    ]

    chunks = build_import_chunks_from_blocks("imp_manual", blocks, chunker_type="manual")

    assert len(chunks) == 2
    assert chunks[0]["section_path"] == ["账号登录"]
    assert chunks[0]["source_text"] == "账号登录\n用户无法登录时先检查账号状态。"
    assert chunks[0]["source_offsets"]["chunker"]["type"] == "manual"
    assert chunks[0]["source_offsets"]["chunker"]["group"] == "账号登录"
    assert chunks[1]["section_path"] == ["售后处理"]
    assert chunks[1]["page_start"] == 2
    assert chunks[1]["source_text"] == "售后处理\n待发货订单可以直接申请退款。"


def test_extract_blocks_from_mineru_payload_rejects_empty_result():
    """MinerU 没有可用正文时明确报错，避免保存空切块。"""
    with pytest.raises(MineruParseError, match="no parseable text"):
        extract_blocks_from_mineru_payload({"content_list": []}, source_file="manual.pdf")


def test_package_mineru_payload_for_kb_skips_noise_and_tracks_sections():
    """可选再处理层按 mineru-kb-packager 思路生成知识库友好的块。"""
    payload = {
        "content_list_v2": [
            [
                {
                    "type": "title",
                    "content": {
                        "level": 1,
                        "title_content": [{"type": "text", "content": "账号登录"}],
                    },
                },
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "用户无法登录时先检查账号状态。"}
                        ]
                    },
                },
                {"type": "page_footer", "content": {"text": "1"}},
            ],
            [
                {
                    "type": "title",
                    "content": {
                        "level": 1,
                        "title_content": [{"type": "text", "content": "Contents"}],
                    },
                },
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [{"type": "text", "content": "这一段目录不应进入知识库。"}]
                    },
                },
            ],
        ]
    }

    blocks = package_mineru_payload_for_kb(payload, source_file="manual.pdf")

    assert len(blocks) == 1
    assert blocks[0].text == "用户无法登录时先检查账号状态。"
    assert blocks[0].block_type == "text"
    assert blocks[0].page_number == 1
    assert blocks[0].section_title == "账号登录"
    assert blocks[0].evidence["postprocess"] == "mineru-kb-packager"


def test_extract_blocks_from_mineru_payload_can_use_kb_packager_mode():
    """开关开启后优先使用知识库再处理块，而不是原始 MinerU 扁平块。"""
    payload = {
        "content_list": [
            [
                {
                    "type": "title",
                    "content": {
                        "level": 1,
                        "title_content": [{"type": "text", "content": "账号登录"}],
                    },
                },
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "按章节整理后的正文。"}
                        ]
                    },
                },
            ]
        ]
    }

    blocks = extract_blocks_from_mineru_payload(
        payload,
        source_file="manual.pdf",
        use_kb_packager=True,
    )

    assert blocks[0].text == "按章节整理后的正文。"
    assert blocks[0].evidence["postprocess"] == "mineru-kb-packager"


def test_mineru_client_standard_mode_downloads_zip_content_list(tmp_path):
    """第三方精准 API 使用 Token、签名上传、轮询任务并读取 zip 里的结构化结果。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr(
            "manual_content_list.json",
            json.dumps(
                [
                    {"type": "title", "text": "账号登录", "page_idx": 0},
                    {"type": "text", "text": "先检查账号状态。", "page_idx": 0},
                ],
                ensure_ascii=False,
            ),
        )

    class FakeResponse:
        def __init__(self, payload=None, status_code=200, text="", content=b""):
            self.payload = payload or {}
            self.status_code = status_code
            self.text = text
            self.content = content

        def json(self):
            return self.payload

    class FakeSession:
        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            assert url == "https://mineru.net/api/v4/file-urls/batch"
            assert kwargs["headers"]["Authorization"] == "Bearer mineru-token"
            assert kwargs["json"]["files"][0]["name"] == "manual.pdf"
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "batch_id": "batch_1",
                        "file_urls": [
                            {
                                "file_name": "manual.pdf",
                                "upload_url": "https://oss.example/upload",
                            }
                        ],
                    },
                }
            )

        def put(self, url, **kwargs):
            calls.append(("put", url, kwargs))
            assert url == "https://oss.example/upload"
            return FakeResponse(status_code=200)

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            if url == "https://mineru.net/api/v4/extract-results/batch/batch_1":
                return FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "extract_result": [
                                {
                                    "file_name": "manual.pdf",
                                    "state": "done",
                                    "full_zip_url": "https://cdn.example/result.zip",
                                }
                            ]
                        },
                    }
                )
            if url == "https://cdn.example/result.zip":
                return FakeResponse(content=zip_buffer.getvalue())
            raise AssertionError(url)

    blocks = MineruClient(
        api_token="mineru-token",
        timeout_seconds=1,
        session=FakeSession(),
    ).parse_file(source)

    assert blocks[0].section_title == "账号登录"
    assert blocks[0].text == "先检查账号状态。"
    assert [call[0] for call in calls] == ["post", "put", "get", "get"]


def test_mineru_client_standard_mode_uses_configured_batch_urls_for_local_file(tmp_path):
    """本地文件导入走用户配置的批量文件 URL，参数由 MinerU 适配器内部构造。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr(
            "manual_content_list.json",
            json.dumps(
                [
                    {"type": "title", "text": "地址配置", "page_idx": 0},
                    {"type": "text", "text": "具体接口地址不会拼接重复路径。", "page_idx": 0},
                ],
                ensure_ascii=False,
            ),
        )

    class FakeResponse:
        def __init__(self, payload=None, status_code=200, content=b""):
            self.payload = payload or {}
            self.status_code = status_code
            self.text = ""
            self.content = content

        def json(self):
            return self.payload

    class FakeSession:
        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            assert url == "https://proxy.example/mineru/file-urls/batch"
            assert kwargs["json"]["files"] == [{"name": "manual.pdf", "data_id": "manual.pdf"}]
            assert kwargs["json"]["enable_formula"] is True
            assert kwargs["json"]["enable_table"] is True
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "batch_id": "batch_1",
                        "file_urls": [
                            {
                                "file_name": "manual.pdf",
                                "upload_url": "https://oss.example/upload",
                            }
                        ],
                    },
                }
            )

        def put(self, url, **kwargs):
            calls.append(("put", url, kwargs))
            return FakeResponse(status_code=200)

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            if url == "https://proxy.example/mineru/extract-results/batch/batch_1":
                return FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "extract_result": [
                                {
                                    "file_name": "manual.pdf",
                                    "state": "done",
                                    "full_zip_url": "https://cdn.example/result.zip",
                                }
                            ]
                        },
                    }
                )
            if url == "https://cdn.example/result.zip":
                return FakeResponse(content=zip_buffer.getvalue())
            raise AssertionError(url)

    blocks = MineruClient(
        api_token="mineru-token",
        batch_file_url="https://proxy.example/mineru/file-urls/batch",
        batch_result_url_template="https://proxy.example/mineru/extract-results/batch/{batch_id}",
        timeout_seconds=1,
        session=FakeSession(),
    ).parse_file(source)

    assert blocks[0].text == "具体接口地址不会拼接重复路径。"
    assert [call[1] for call in calls if call[0] in {"post", "get"}] == [
        "https://proxy.example/mineru/file-urls/batch",
        "https://proxy.example/mineru/extract-results/batch/batch_1",
        "https://cdn.example/result.zip",
    ]


def test_mineru_client_can_start_file_and_read_running_progress(tmp_path):
    """MinerU 批量上传和状态查询要拆开，前端才能展示动态解析进度。"""
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF")
    calls = []

    class FakeResponse:
        def __init__(self, payload=None, status_code=200):
            self.payload = payload or {}
            self.status_code = status_code
            self.text = ""
            self.content = b""

        def json(self):
            return self.payload

    class FakeSession:
        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "batch_id": "batch_1",
                        "file_urls": [
                            {
                                "file_name": "manual.pdf",
                                "upload_url": "https://oss.example/upload",
                            }
                        ],
                    },
                }
            )

        def put(self, url, **kwargs):
            calls.append(("put", url, kwargs))
            return FakeResponse(status_code=200)

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "batch_id": "batch_1",
                        "extract_result": [
                            {
                                "file_name": "manual.pdf",
                                "state": "running",
                                "extract_progress": {
                                    "extracted_pages": 3,
                                    "total_pages": 12,
                                    "start_time": "2026-05-13 10:00:00",
                                },
                            }
                        ],
                    },
                }
            )

    client = MineruClient(api_token="mineru-token", timeout_seconds=1, session=FakeSession())

    started = client.start_file(source)
    status = client.get_task_status(started.batch_id, started.file_name)

    assert started == MineruParseStatus(
        batch_id="batch_1",
        file_name="manual.pdf",
        state="waiting-file",
        progress={},
        result={},
        error=None,
        zip_url=None,
    )
    assert status.state == "running"
    assert status.progress == {
        "extracted_pages": 3,
        "total_pages": 12,
        "start_time": "2026-05-13 10:00:00",
    }
    assert [call[0] for call in calls] == ["post", "put", "get"]
