# 第二阶段检索精度优化计划

> [!IMPORTANT]
> 本文是历史阶段记录。当前文档 child 来源顺序固定为 structured blocks → delimiter → full text，详见
> `docs/changes/20260714-092948-knowledge-graph-retrieval-review/`；不得恢复 delimiter 优先逻辑。

## 背景结论

第一阶段已经完成统一 `knowledge_chunks`、意图识别、向量 + 关键词混合召回、RRF 融合、检索评测用例和智能问答调试抽屉。当前目标从“公司级完整落地”收窄为“单人调试下尽快提高真实回答准确率”。

因此本阶段不优先做权限、审计、RBAC、复杂发布治理和完整知识图谱，优先围绕“召回准、上下文够、答案不乱编”推进。

## 修改目标

本阶段目标是把文档类知识从“单层粗 chunk 召回”升级为“RAGFlow 式解析/切片分层 + 结构化 chunk + 上下文增强 + 父级回填”的检索基础。

核心目标：

1. 为 `knowledge_chunks` 增加父子 chunk 和结构化来源字段。
2. 让文档类 `embedding_text` 带上文件名、章节路径、页码、块类型和关键词，减少孤立片段缺主语的问题。
3. 在检索结果对象中保留父子 chunk 字段，为命中 child 后回填 parent/sibling 做准备。
4. 增加最小父级上下文回填接口，后续可接入问答链路和 rerank。
5. 保持现有 FAQ、文档导入和智能问答链路可用，不做 UI 大改。
6. 按 RAGFlow 方式把 MinerU 定位为解析器 backend：解析器输出结构块和位置 tag，chunk/parent-child 不由 MinerU 负责。
7. 继续补齐 RAGFlow 对 MinerU 输出的后处理层：content type 转写、PDF 坐标规范化、媒体上下文按位置补全、zip 资产路径证据保留。

## 影响范围

- 数据库：`knowledge_chunks` 增加 `parent_chunk_id`、`chunk_level`、`section_path`、`page_start`、`page_end`、`block_type`、`source_offsets` 等字段和索引；`import_chunks` 保存解析审核层的 `source_blocks`，不保存父子关系。
- 数据映射：文档切片写入 `knowledge_chunks` 时补充结构化 metadata 和 contextual `embedding_text`。
- 检索结果：`RetrievedKnowledgeChunk` 增加父子 chunk 字段，避免后续只靠 metadata 取值。
- 检索链路：新增最小 parent context lookup，用于后续 child 命中后扩展上下文。
- 测试：先补失败测试，再实现代码。

## 具体步骤

1. 写测试锁定 `knowledge_chunks` 新字段、索引和 upsert SQL。
2. 写测试锁定文档 chunk 映射时的 contextual `embedding_text`。
3. 写测试锁定 `RetrievedKnowledgeChunk` 能携带父子 chunk 字段。
4. 写测试锁定 parent context 查询 SQL 只读取同源可用 parent。
5. 修改 `sql/001_init.sql`，新增字段、兼容性 `ALTER TABLE` 和索引。
6. 修改 `customer_service_agent/db.py`，更新文档映射、upsert、检索读取和 parent 查询方法。
7. 必要时修改 `customer_service_agent/admin_server.py` 的问答链路，保持现有返回结构不破坏前端。
8. 运行聚焦测试、全量测试、ruff 和配置检查。

## RAGFlow 对齐口径

- RAGFlow 中 `layout_recognize = MinerU` 只决定 PDF 解析 backend；真正切片由 `rag/app/naive.py`、`rag/app/manual.py` 和 `rag/nlp` 的 merge/tokenize 流程完成。
- MinerU 输出的 `content_list` 被转成 section/table/image/formula/code/list 等结构块，并用 `@@page\tleft\tright\ttop\tbottom##` 形式保留页面位置。
- Parent-child 不是 MinerU 提供的字段，而是在 token/chunk 阶段通过结构块或 child delimiter 生成，父内容类似 RAGFlow 的 `mom_with_weight` 作为 child 的上级上下文。
- 本项目执行同样边界：`document_parser.py` 只保留结构块、页码、块类型、来源证据和 RAGFlow position tag；`admin_server.py` 在 embedding/indexing 时从 `source_blocks` 派生 parent/child knowledge rows。
- RAGFlow 新版 flow parser 会把 MinerU pipeline 输出转成 bbox/json item，再补 `layout_type`、`doc_type_kwd`、`layoutno`、positions、media context 等下游 chunker 需要的字段；本项目在 `source_blocks` / `source_offsets` 中保留这些可追溯信息。
- 不再把解析块渲染成“章节 / 页码 / 类型 / 正文”后再解析；审核正文只保存用户可读正文，结构信息单独保存。

## 预期效果

- 新生成的文档 embedding 不再只包含切片正文，而是带有文件、章节、页码和块类型上下文。
- 后续可以把文档切成 child chunk 做精准召回，再把 parent/sibling 作为回答上下文。
- 当前智能问答、评测和混合召回不需要推倒重写。

## 暂不包含

- 不做完整 KG、实体关系审核 UI 或图谱可视化。
- 不做登录鉴权、RBAC、审计和公司级发布流程。
- 不引入外部 ES、Redis 或新向量库。
- 不大改管理后台布局。

## 完成记录

- 已为 `knowledge_chunks` 增加父子 chunk 和结构化来源字段：`parent_chunk_id`、`chunk_level`、`section_path`、`page_start`、`page_end`、`block_type`、`source_offsets`。
- 已为 `import_chunks` 增加结构字段和 `source_blocks`，让文档解析/审核阶段保存章节、页码、块类型、位置 tag 和来源证据。
- 已移除 `import_chunks` 层面的 `parent_chunk_id` / `chunk_level`，父子关系只在 `knowledge_chunks` 检索索引层表达。
- 已让 MinerU 解析块进入导入审核切片时保存 `section_path`、页码范围、块类型和来源偏移字段。
- 已按 RAGFlow 的 `@@page\tleft\tright\ttop\tbottom##` 形式保存 MinerU bbox 位置 tag，但不混入审核正文。
- 已把文档类 `embedding_text` 从裸正文升级为 contextual 格式，包含文件名、章节路径、页码、块类型、关键词和正文。
- 已让文档 embedding 阶段把一个审核切片写成 parent 知识单元；当切片中包含多个 `source_blocks` 时，额外从结构块派生 child 知识单元用于更精确召回。
- 已在手工编辑审核正文时清空旧 `source_blocks`，避免用户改正文后继续从旧解析块派生 child。
- 已新增 `customer_service_agent/chunking.py`，按 RAGFlow `naive_merge` / `split_with_pattern` 口径实现 token 预算合并、反引号自定义分隔符和 `children_delimiter` child 拆分。
- 已把 MinerU 导入审核切片改为走 RAGFlow naive merge 配置：`DOCUMENT_CHUNK_TOKEN_NUM`、`DOCUMENT_CHUNK_DELIMITER`、`DOCUMENT_CHUNK_OVERLAP_PERCENT`、`DOCUMENT_CHILDREN_DELIMITER`。
- 已在 `import_chunks` 保存 `children_delimiter`，embedding/indexing 时优先按 delimiter 从 parent 正文生成 child；child metadata 记录 `parent_content`，对齐 RAGFlow `mom_with_weight` 的上下文语义。
- 已按 RAGFlow `attach_media_context` / `append_context2table_image4pdf` 口径补充表格、图片、figure 块的邻近文本上下文；上下文进入 `source_blocks` 的 `context_above`、`context_below` 和 `evidence.media_context`，同时参与后续 naive merge。
- 已新增 `DOCUMENT_TABLE_CONTEXT_SIZE`、`DOCUMENT_IMAGE_CONTEXT_SIZE`，允许按 token 预算控制表格和图片块分别吸收多少同页前后文本；默认值为 `0`，避免未确认时改变已有导入表现。
- 已继续补齐 RAGFlow MinerU 后处理核心能力：`table/image/equation/code/list/discarded` content type 转写、`layout_type/layoutno/doc_type_kwd` 标准化、`pdf_positions` 规范化、按 PDF 坐标排序和重叠文本上下文补全。
- 已让 MinerU 结果 zip 中的 `img_path/table_img_path/equation_img_path` 资产安全解压到 `UPLOAD_DIR/mineru-assets`，并把本地路径写入 `source_blocks.asset_paths` / `evidence.asset_paths`。
- 已把 `tiktoken>=0.7.0` 加入项目依赖，并在当前 conda 环境安装，用 RAGFlow 同款 `cl100k_base` token 计数。
- 已新增 parent context 回填查询：命中 child 后可读取同来源、可用且 ready 的 parent chunk。
- 已把智能问答链路接入 parent context 回填，命中 child 时会把 parent 上下文追加到回答 prompt 和命中来源事件中。
- 已确认裸 `conda` 不在当前工具 shell 的 PATH 中；后续验证命令统一使用 `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run ...`。

## 验证记录

- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python --version`：`Python 3.11.15`
- 聚焦红灯验证：新增父子 chunk、contextual embedding、parser 结构字段和 parent context 测试在实现前按预期失败。
- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_document_parser.py::test_build_import_chunks_from_blocks_batches_text_with_evidence tests/test_db.py::test_build_document_knowledge_chunk_row_adds_contextual_embedding_text tests/test_db.py::test_knowledge_chunks_schema_supports_vector_and_keyword_retrieval tests/test_db.py::test_insert_knowledge_chunk_sql_uses_single_upsert_shape tests/test_db.py::test_search_knowledge_sql_reads_unified_chunks_without_confidence_filter tests/test_db.py::test_search_knowledge_text_sql_reads_keyword_fields tests/test_db.py::test_import_chunks_schema_preserves_parser_structure_for_retrieval tests/test_db.py::test_parent_context_sql_reads_same_source_parent_chunks tests/test_admin_server.py::test_admin_app_embed_import_file_writes_document_chunks_to_knowledge_chunks tests/test_admin_server.py::test_admin_app_embed_import_file_derives_child_chunks_from_structured_blocks -q`：`10 passed`
- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_admin_server.py::test_admin_app_iter_assistant_chat_events_expands_child_hits_with_parent_context -q`：`1 passed`
- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_db.py tests/test_document_parser.py tests/test_admin_server.py tests/test_retrieval.py tests/test_rag.py -q`：`72 passed`
- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest -q`：`184 passed`
- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check .`：`All checks passed!`
- `node --check customer_service_agent/static/admin.js`：通过
- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli check-config`：`config ok`
- `source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli init-db`：`database schema ok`

## 2026-05-15 RAGFlow 对齐修正验证

- 红灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_document_parser.py::test_build_import_chunks_from_blocks_batches_text_with_evidence tests/test_db.py::test_import_chunks_schema_preserves_parser_structure_for_retrieval tests/test_admin_server.py::test_admin_app_embed_import_file_derives_child_chunks_from_structured_blocks -q`：实现前 `3 failed`，失败点分别为缺少 `source_blocks`、schema 未保存 `source_blocks`、child 仍从审核正文派生。
- 红灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_db.py::test_update_import_chunk_text_sql_marks_existing_knowledge_chunk_stale -q`：实现前失败，确认手工编辑正文后旧 `source_blocks` 未清空。
- 绿灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_document_parser.py::test_build_import_chunks_from_blocks_batches_text_with_evidence tests/test_document_parser.py::test_extract_blocks_from_mineru_content_list_preserves_ragflow_position_tag tests/test_db.py::test_import_chunks_schema_preserves_parser_structure_for_retrieval tests/test_admin_server.py::test_admin_app_embed_import_file_derives_child_chunks_from_structured_blocks -q`：`4 passed`
- 绿灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_db.py::test_update_import_chunk_text_sql_marks_existing_knowledge_chunk_stale -q`：`1 passed`
- 聚焦验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_document_parser.py tests/test_db.py tests/test_admin_server.py tests/test_retrieval.py tests/test_rag.py -q`：`73 passed`
- 语法验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m py_compile customer_service_agent/document_parser.py customer_service_agent/admin_server.py customer_service_agent/db.py`：通过
- 全量测试：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest -q`：`185 passed`
- 全量 lint：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check .`：`All checks passed!`
- 配置检查：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli check-config`：`config ok`
- schema 初始化：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli init-db`：`database schema ok`

## 2026-05-15 RAGFlow Chunker 继续迁移验证

- 红灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py tests/test_admin_server.py::test_admin_app_embed_import_file_splits_children_with_delimiter -q`：实现前因缺少 `customer_service_agent.chunking` 失败。
- 红灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py tests/test_admin_server.py::test_admin_app_embed_import_file_splits_children_with_delimiter tests/test_config.py::test_settings_from_env_parses_document_chunking_values tests/test_db.py::test_import_chunks_schema_preserves_parser_structure_for_retrieval -q`：实现中曾失败于 `Settings` 未暴露文档 chunk 配置、child 文本被 `.strip()` 去掉 RAGFlow 保留分隔符。
- 绿灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py tests/test_document_parser.py tests/test_admin_server.py::test_admin_app_settings_snapshot_exposes_runtime_config_for_local_modal tests/test_admin_server.py::test_admin_app_update_settings_persists_local_tenant_settings_and_refreshes_runtime_config tests/test_admin_server.py::test_admin_app_embed_import_file_splits_children_with_delimiter tests/test_config.py::test_settings_from_env_parses_required_values tests/test_config.py::test_settings_from_env_parses_document_chunking_values tests/test_db.py::test_import_chunks_schema_preserves_parser_structure_for_retrieval -q`：`18 passed`
- 聚焦验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py tests/test_document_parser.py tests/test_db.py tests/test_admin_server.py tests/test_config.py tests/test_retrieval.py tests/test_rag.py -q`：`86 passed`
- 聚焦 lint：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check customer_service_agent/chunking.py customer_service_agent/document_parser.py customer_service_agent/admin_server.py customer_service_agent/db.py customer_service_agent/config.py tests/test_chunking.py tests/test_document_parser.py tests/test_admin_server.py tests/test_db.py tests/test_config.py`：`All checks passed!`
- 语法验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m py_compile customer_service_agent/chunking.py customer_service_agent/document_parser.py customer_service_agent/admin_server.py customer_service_agent/db.py customer_service_agent/config.py`：通过
- 依赖安装：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pip install 'tiktoken>=0.7.0'`：安装 `tiktoken-0.13.0` 和 `regex-2026.5.9`。
- 安装 `tiktoken` 后全量测试：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest -q`：`190 passed`
- 安装 `tiktoken` 后全量 lint：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check .`：`All checks passed!`
- 安装 `tiktoken` 后配置/schema 验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli check-config && source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli init-db`：`config ok` / `database schema ok`
- 前端语法和 diff 空白检查：`node --check customer_service_agent/static/admin.js && git diff --check`：通过

## 2026-05-15 RAGFlow Media Context 继续迁移验证

- 红灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py::test_attach_media_context_to_blocks_adds_neighbor_text_to_table tests/test_document_parser.py::test_build_import_chunks_from_blocks_applies_table_context_window tests/test_config.py::test_settings_from_env_parses_required_values tests/test_config.py::test_settings_from_env_parses_document_chunking_values tests/test_admin_server.py::test_admin_app_update_settings_persists_local_tenant_settings_and_refreshes_runtime_config -q`：实现前失败于缺少 `attach_media_context_to_blocks` 和上下文配置字段。
- 绿灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py::test_attach_media_context_to_blocks_adds_neighbor_text_to_table tests/test_document_parser.py::test_build_import_chunks_from_blocks_applies_table_context_window tests/test_config.py::test_settings_from_env_parses_required_values tests/test_config.py::test_settings_from_env_parses_document_chunking_values tests/test_admin_server.py::test_admin_app_update_settings_persists_local_tenant_settings_and_refreshes_runtime_config -q`：`5 passed`
- 聚焦验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py tests/test_document_parser.py tests/test_db.py tests/test_admin_server.py tests/test_config.py tests/test_retrieval.py tests/test_rag.py -q`：`88 passed`
- 聚焦 lint：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check customer_service_agent/chunking.py customer_service_agent/document_parser.py customer_service_agent/admin_server.py customer_service_agent/config.py customer_service_agent/db.py tests/test_chunking.py tests/test_document_parser.py tests/test_admin_server.py tests/test_config.py tests/test_db.py`：`All checks passed!`
- 语法验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m py_compile customer_service_agent/chunking.py customer_service_agent/document_parser.py customer_service_agent/admin_server.py customer_service_agent/config.py customer_service_agent/db.py`：通过

## 2026-05-15 最终验收验证

- 全量测试：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest -q`：`192 passed`
- 全量 lint：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check .`：`All checks passed!`
- 配置检查：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli check-config`：`config ok`
- schema 初始化：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli init-db`：`database schema ok`
- 前端语法检查：`node --check customer_service_agent/static/admin.js`：通过
- diff 空白检查：`git diff --check`：通过

## 2026-05-15 RAGFlow MinerU 后处理继续迁移验证

- 红灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py::test_extract_pdf_positions_matches_ragflow_position_sources tests/test_chunking.py::test_attach_media_context_to_blocks_uses_overlapping_pdf_text tests/test_chunking.py::test_ragflow_naive_merge_blocks_source_offsets_include_pdf_positions tests/test_document_parser.py::test_extract_blocks_from_mineru_content_list_transfers_ragflow_content_types tests/test_document_parser.py::test_mineru_client_standard_mode_extracts_zip_assets_to_evidence -q`：实现前失败于缺少 `extract_pdf_positions`、content type 转写和 zip 资产路径处理。
- 绿灯验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py::test_extract_pdf_positions_matches_ragflow_position_sources tests/test_chunking.py::test_attach_media_context_to_blocks_uses_overlapping_pdf_text tests/test_chunking.py::test_ragflow_naive_merge_blocks_source_offsets_include_pdf_positions tests/test_document_parser.py::test_extract_blocks_from_mineru_content_list_transfers_ragflow_content_types tests/test_document_parser.py::test_mineru_client_standard_mode_extracts_zip_assets_to_evidence -q`：`5 passed`
- 聚焦验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest tests/test_chunking.py tests/test_document_parser.py tests/test_db.py tests/test_admin_server.py tests/test_config.py tests/test_retrieval.py tests/test_rag.py -q`：`93 passed`
- 聚焦 lint：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check customer_service_agent/chunking.py customer_service_agent/document_parser.py customer_service_agent/admin_server.py tests/test_chunking.py tests/test_document_parser.py`：`All checks passed!`
- 语法验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m py_compile customer_service_agent/chunking.py customer_service_agent/document_parser.py customer_service_agent/admin_server.py`：通过
- 最终全量测试：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest -q`：`197 passed`
- 最终全量 lint：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check .`：`All checks passed!`
- 最终配置/schema 验证：`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli check-config`：`config ok`；`source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli init-db`：`database schema ok`
- 最终前端语法和 diff 空白检查：`node --check customer_service_agent/static/admin.js`：通过；`git diff --check`：通过
