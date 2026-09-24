# AGENTS.md

本文件是本项目的项目级开发约定。任何 agent 或开发者在修改本仓库前，都必须先阅读并遵守这些规则。

## 项目定位

- 本项目是本地客服知识库与 RAG 服务，当前核心能力包括 FAQ 管理、向量检索、RAG 答案草稿、AI 辅助编辑和微信服务。
- 项目默认处理业务知识和客服话术，真实 FAQ、客户资料、生产提示词、密钥、微信 token、上传文件原件不得提交到 Git。
- 当前架构以 Python 后端、PostgreSQL + pgvector、OpenAI-compatible Chat / Embedding 接口、静态本地管理页为主。新增能力优先沿用现有模块边界。

## 开发前沟通规则

- 涉及数据库 schema、API、知识库导入流程、AI 生成逻辑、检索策略、微信服务、UI 布局等较大改动时，必须先和用户互动确认具体修改方式和预期效果。
- 较大改动开始前，在 `docs/changes/<YYYYMMDD-HHMMSS>-<topic>/` 创建本次改动目录。
- 改动目录至少包含：
  - `user-materials/`：存放用户提供的资料；如果本次没有外部资料，放一个 `README.md` 说明“本次无用户外部资料”。
  - `update-plan.md`：写清楚修改目标、影响范围、具体步骤、预期效果、需要用户确认的问题。
  - `confirmation.md`：记录用户确认内容、确认时间、后续变更点。
- 未拿到用户确认前，不直接实现较大功能；可以先做 review、调研、方案、草图说明和文档准备。

## 代码风格

- 每个新写或修改的方法、函数、类方法都需要有中文注释或中文 docstring，说明“做什么”和“关键约束”。注释要服务理解，不写空泛说明。
- 优先使用简洁直接的实现。不要因为担心所有极端情况而写过量兜底；兜底前先判断它是否真实必要。
- 0→1 新功能只保留唯一的当前契约；不得为不存在的旧版本增加方法别名、同步适配入口、双路径、双写或静默 fallback，除非用户明确要求。
- 防御性编程只覆盖明确会发生、影响用户体验或数据正确性的场景。没有清晰收益的多层包装、重复校验、宽泛异常吞噬都应避免。
- 遵循现有模块职责：配置在 `config.py`，数据库读写在 `db.py`，RAG 逻辑在 `rag.py` / `rag_tool.py`，AI 辅助在 `ai_assist.py`，本地管理 API 在 `admin_server.py`，静态页面在 `cyclops/static/`。
- 不做无关重构，不混入格式化噪音，不回滚用户已有改动。

## 测试和验证

- 修改 Python 行为时，优先补充或更新对应测试；没有测试覆盖的行为，至少说明手工验证方式。
- 常用验证命令：
  - `python -m pytest`
  - `python -m ruff check .`
  - `python -m cyclops check-config`
- 如果本地缺少依赖、数据库或环境变量导致命令无法运行，必须在最终说明中写清楚。
- 测试文件应作为项目质量资产保留；不要把应提交的测试当作临时本地文件处理。

## UI 开发规则

- UI 功能开始前，先和用户讨论最终功能、信息层级、主要布局和操作流程。
- 布局方向确认后，先给用户一段可用于和另一个 AI 沟通的 UI prompt。用户拿到 UI 布局图片后，再根据图片和确认后的功能实现代码。
- 管理后台 UI 要偏工具型、信息密集但清晰，不做营销式首页，不使用无意义装饰。
- 文件上传、AI 解析、问答审核等流程必须显式呈现状态：待解析、解析中、待审核、已保存、向量待生成、向量可检索、失败可重试。

## 知识库功能方向

- 知识库导入应支持“文件原件管理”和“AI 解析结果审核”分离：上传文件后先解析、切块、生成候选问答，用户编辑确认后再保存为正式 FAQ。
- Word、PDF、xlsx、Markdown、微信聊天记录 Markdown 等来源要保留来源信息，包括文件名、页码/表格行号/章节/消息时间等可追溯证据。
- AI 生成问答默认进入 `needs_review` 或等价待审核状态，不直接进入可检索状态。
- 保存 FAQ 只保存正文和元数据；向量生成继续保持独立步骤，避免未审核内容直接进入检索。
- 分块策略要能解释：按标题、段落、表格行、问答行、聊天轮次等结构优先，必要时再按长度切分。
- 对“平台使用手册、注意事项、SOP”类资料，优先生成候选问答和引用证据；对 xlsx 问答表，优先做字段映射、去重和批量导入审核。

## 数据和安全

- `.env`、`system_prompt.txt`、`*.jsonl`、`*.csv`、用户上传原件、客户聊天记录、微信 token 都不能提交。
- 管理页面当前定位为本地内部工具。若需要暴露到局域网或公网，必须先补登录鉴权、上传大小限制、审计日志和敏感数据处理策略。
- AI 处理用户材料时，不默认改写事实；模型输出必须经过人工确认才能进入正式知识库。

## 提交和交付

- 最终说明要包含：改了什么、验证了什么、还有什么风险或未做事项。
- 如果做了较大改动，要同步更新对应 `docs/changes/<timestamp>-<topic>/` 中的计划和确认记录。
- 不主动提交 Git commit，除非用户明确要求。

<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->
