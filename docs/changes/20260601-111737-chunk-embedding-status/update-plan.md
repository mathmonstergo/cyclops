# 切片级 embedding 状态修复

> [!IMPORTANT]
> 本文是历史缺陷记录。当前文档身份、parent/child 和 embedding 状态契约以
> `.trellis/spec/backend/cyclops-db-contracts.md` 为准；不得从本文恢复旧 `RetrievedDocument` 或旧关联推断。

- 时间：2026-06-01 11:17
- 类型：bug 修复（取数 SQL / API 输出派生字段），不改 schema、不改写已存数据
- 来源：用户验收时发现 —— #15 点「重新向量」提示「已重新生成（5 条）」，但切片仍显示「未索引」，且横滚轴圆点与工具栏状态点颜色不同步

## 现象与根因（已用代码 + 线上库确认）

1. **「5 条」**：单切片重新向量走 RAGFlow 父/子切块展开 —— 1 个 parent（整片，上下文召回）+ 每个非空解析块 1 个 child（精确召回）。#15 有 4 个非空块 → 1+4=5 条。属预期行为，非 bug。
   - 证据：`document_knowledge_rows_for_embedding()`（admin_server.py:483）对 4 块切片返回 5 行；线上一条假设问题数为 0 的切片同样产出 5 条，证明「5」与假设问题无关。

2. **仍显示「未索引」（真 bug）**：向量确实写入了 `knowledge_chunks`，但切片徽标读的是 `import_chunks.embedding_status`，而**该表根本没有这一列**（sql/001_init.sql:158–195，线上 `information_schema` 确认 `-> False`）。`list_import_chunks` 是 `SELECT *`（imports.py:270），于是前端 `chunk.embedding_status` 永远 `undefined` → 固定「未索引」，与是否重新向量无关。

3. **两颗点不同步**：横滚轴 `#n` 后的点 = 橙(`embedding_status==='stale'`，因字段 undefined 永不亮) + 蓝(`questions_status==='ready'`)；工具栏状态点 = `mapEmbed(embedding_status)`（永远灰「未索引」）。两者本就表示不同维度，叠加 ② 的死字段，自然永远对不上。

## 改法（用户已确认走「显示真实索引状态」）

只改后端取数：`list_import_chunks` 改为 LEFT JOIN `knowledge_chunks`，按该切片的 parent + child 知识单元聚合派生真实 `embedding_status`，规则与文件级摘要（imports.py:714-722）一致：

- 无向量 → `pending`（界面「未索引」）
- 任一 `stale` → `stale`（界面「过期」橙，编辑后即触发）
- 全 `failed` 且无 `ready` → `failed`（界面「索引失败」红）
- 全 `ready` → `ready`（界面「已索引」绿）
- 其余 → `partial`

关联口径沿用既有 stale/删除 SQL：parent 的 `source_chunk_id = 切片id`，child 的 `parent_chunk_id = 'kc_document_' || 切片id`（已用线上数据验证该 OR 条件正好覆盖 1 parent + N child）。

**前端零改动**：`mapEmbed` / `embeddingStatusLabel`（labels.ts:22-28）已能渲染上述全部状态值；状态点、横滚轴橙点、`needsEmbed` 按钮高亮三处都读同一字段，后端喂入真实值后一并自愈。因无前端代码变更，`dist` 不需重建。

## 改动文件
- `customer_service_agent/db/imports.py`（`list_import_chunks` + 新增 `_list_import_chunks_sql`）

## 验证
- ruff 静态检查
- 重启 admin server，调 `list_import_chunks` 实测：已 embed 的切片 → `ready`；从未 embed → `pending`；模拟编辑（标 stale）→ `stale`
- 之后交用户在浏览器验收：重新向量后 #15 应变「已索引(绿)」

## 测试策略
- 按用户约定：本次不补单测，等浏览器验收通过后再议。

## 安全 / 数据
- 纯只读联表查询，不写库、不改 schema、不触碰 `.env` / system_prompt / 上传原件 / 客户数据 / 微信 token。

## 追加修复：切片「保存原文」一直 500（验收中发现）

- 现象：编辑切片点「保存切片」没反应。
- 根因：`_mark_document_chunk_knowledge_stale_sql`（imports.py）里 `concat_ws(E'\n', source_title, tags::text, %(source_text)s)` 的 `%(source_text)s` 在 `concat_ws` 中无法被 Postgres 推断类型 → `psycopg.errors.AmbiguousParameter: could not determine data type of parameter $1` → 保存接口 500。前端保存按钮 `onClick` 既无 try/catch 也无 toast，错误被静默吞掉，表现为「点击没反应」。（保存三步在同一事务，第三步失败整体回滚，故用户数据未被破坏。）
- 修法：把该参数显式转型 `%(source_text)s::text`。
- 验证：用回滚事务重放保存三步（UPDATE 正文 → 删 child → 标 parent stale），修前第三步抛 AmbiguousParameter、修后三步全过且 parent 正确变 `stale`；ruff 通过。未改动真实数据。
- 改动文件：`customer_service_agent/db/imports.py`（同上文件）。

## 前端：横滚轴圆点同步 + 保存 toast（已完成，已 rebuild dist）

- 用户选定方案：**只同步状态点** —— 横滚轴每片恒显一个圆点，颜色 = `mapEmbed(embedding_status)`，与工具栏 StatusDot 同款；假设问题不再上滚轴（仍由工具栏「N问」标签呈现）。
- 改动：
  - `web/src/components/ui/status-dot.tsx`：导出 `TONE_COLOR`（供滚轴复用同款配色）。
  - `web/src/pages/documents/chunk-browser.tsx`：`ChunkNav` 圆点改为 `TONE_COLOR[mapEmbed(c.embedding_status)]`，去掉原 stale/questions 双点逻辑；hover 提示改为 段落/块 + 索引状态文案。
  - `web/src/pages/documents/chunk-browser.tsx`：「保存切片」按钮 `onClick` 补 try/catch + 成功/失败 toast，杜绝静默失败。
- 验证（Playwright headless，对生产 dist）：
  - 已索引文件（用户使用手册）：滚轴 #1–#16 每片**绿点**，与工具栏「已索引」同色；
  - 从未索引文件（京师筑心2026春.md）：滚轴每片**橙点**＝未索引；
  - 抽屉打开周期内 **console error/warning = 0**；`tsc -b && vite build` 通过，dist 仍 1 JS + 1 CSS。

## 前端：圆点三态重定义（禁用 / 已嵌入 / 其余）（已完成，已 rebuild dist）

- 用户要求：把「禁用」也纳入圆点状态，且**禁用包含文件层与切片层**——只要任一为禁用就直接**覆盖**其余状态，用**灰**表示；「已嵌入且无新改动（ready）」用**绿**；其余所有状态（未索引/过期/失败/部分）统一用**黄**。
- 优先级：`禁用(灰) > ready(绿) > 其余(黄)`。
- 改动：
  - `web/src/pages/documents/chunk-browser.tsx`：以 `dotTone(embedding_status, disabled)` 取代旧 `mapEmbed`（旧函数四态：ready/failed/stale/pending）。`disabled = fileDisabled || chunk.is_disabled`。横滚轴 chip 点、工具栏 `StatusDot` 同走该三态；工具栏被禁用时标签文案与提示一并显示「已禁用」（覆盖原索引文案）。
  - `ChunkBrowser`/`ChunkNav`/`ChunkToolbar` 新增 `fileDisabled?: boolean` 透传。
  - `web/src/pages/documents/document-drawer.tsx`：`<ChunkBrowser ... fileDisabled={file.is_disabled} />`。
  - 复用 `status-dot.tsx` 的 `TONE_COLOR`（`muted`→灰 `--color-text-faint`、`ready`→绿 `--color-success`、`warning`→黄 `--color-warning`）；新增导出 `type DotTone`。
- 验证（Playwright headless 对生产 dist，**直接读每颗圆点 computed background-color** 作为基准，非肉眼）：
  - flexCAT-English.pdf（#8 为切片层禁用）：滚轴 #8 = **灰**，其余 13 片 = **黄**；切到 #8 工具栏 = 「已禁用」**灰**。证明切片层禁用覆盖 + 单点独立生效。
  - 用户使用手册.pdf（全 ready）：滚轴 29 片全 **绿**，工具栏「已索引」**绿**。
  - 京师筑心2026春.md（全未索引）：滚轴 185 片全 **黄**，工具栏「未索引」**黄**。
  - 文件层禁用（临时把「用户使用手册」`is_disabled` 置 true，验毕**已还原** false）：29 片全 **灰**，覆盖原 ready 绿；工具栏「已禁用」灰。
  - 四个用例 **console error/warning = 0**；`tsc -b && vite build` 通过，dist 仍 1 JS + 1 CSS。
- 范围说明：本次只改**切片层**圆点（横滚轴 chip + 切片工具栏）。**文件层**圆点（文档列表行 `document-list.tsx`、抽屉头部 `document-drawer.tsx`）当前仍表示**解析状态**（`mapStatus(file.status)`），未改其语义——是否也切到「禁用/嵌入」口径待用户确认。

## 前端 + 后端：文件层圆点也切到三态（已完成，已 rebuild dist + 重启 server）

- 用户确认（选择题）：文件层圆点（文档列表每行、抽屉头部）**也改成**「禁用灰 / 已嵌入绿 / 其余黄」，接受语义从「解析状态」变为「嵌入状态」、丢解析进度信号。
- 文件级「已嵌入」判定：复用后端早已派生的文档级摘要 `embedding_summary.status`（`_import_file_embedding_summaries_sql`，取值 none/pending/partial/stale/failed/ready）。`status==='ready'`（ready_count≥total 且无 stale/failed）→ 绿；其余 → 黄；`file.is_disabled` → 灰覆盖。
- 改动：
  - `web/src/pages/documents/document-list.tsx`、`document-drawer.tsx`：各自本地新增 `fileEmbedDotTone(summary, disabled)`（沿用本仓「每页本地 mapper」惯例），替换原 `mapStatus(file.status)`；标签改读 `embeddingStatusLabel`，禁用时显示「已禁用」；移除已无引用的 `mapStatus` 与 `importFileStatusLabel` 引入。抽屉头部移除与新灰点重复的红色「已禁用」Badge。
  - `web/src/lib/labels.ts`：`embeddingStatusLabel` 补 `partial: '部分索引'`，避免 partial 漏译露出英文。
  - `customer_service_agent/admin_server.py`：`_import_parse_status_payload` 给返回的 `file` 附 `embedding_summary`（原解析轮询接口不带，抽屉 `file` 来自该接口，否则头部点永远拿不到 ready）。与列表接口同形，纯附加字段、不改 schema。
- 验证（Playwright headless 直读 computed 颜色 + curl 验接口）：
  - 接口：`/parse-status` 现已带 `embedding_summary.status`（用户使用手册=ready、京师筑心=pending）。
  - 列表行 +（同一文件）抽屉头部点一致：L站凡人intro=ready→**绿**、flexCAT=stale→**黄**、用户使用手册=ready→**绿**、京师筑心=pending→**黄**；标签 已索引/过期/未索引。
  - 文件层禁用（临时置「用户使用手册」`is_disabled=true`，验毕**已还原** false）：列表行 + 抽屉头部均 **灰**「已禁用」，覆盖原 ready 绿。
  - 四文件抽屉 **console error/warning = 0**；`tsc -b && vite build` 通过、ruff 通过，dist 仍 1 JS + 1 CSS。
- 一处可再权衡（已在交付里告知用户）：列表「切片 / Embedding」列的 `EmbeddingMini` 已显示嵌入数字，现在「状态」列圆点也变嵌入态，二者略重复；若更想让列表行保留解析状态、仅抽屉头部走嵌入态，可再调。


## 前端：切片圆点一色一态 + chip tooltip + 工具栏精简 + 状态筛选（已构建，待用户强刷验收）

- 用户要求：把切片状态统一到「#数字后的圆点」，不同颜色代表不同状态、hover 看具体信息；并经选择题确认 ①圆点拆成每状态一色 ②精简切片工具栏 ③加状态筛选下拉。（与上一轮「三态收敛」相反——本轮按用户新指示重新拆细。）
- 改动：
  - `web/src/index.css`：新增橙色 token `--color-stale: #ec8e3c`（过期专用，色相介于黄 `warning` 与红 `danger` 之间，小圆点上可区分）。
  - `web/src/components/ui/status-dot.tsx`：`TONE_COLOR` 改为一色一态 —— `pending` 灰(原黄)、`stale` 橙(原灰)、`ready` 绿、`warning` 黄、`failed` 红、`muted` 灰。**注：此为全局共享配色**，FAQ 列表/抽屉的 `StatusDot`（经 `mapEmbed` 同样产出 `stale`/`pending` tone）随之同步：FAQ 未索引 黄→灰、过期 灰→橙（属朝一致性的改进，已告知用户可否决）。文件层 `fileEmbedDotTone` 只用 `muted/ready/warning`，视觉不变。
  - `web/src/pages/documents/chunk-browser.tsx`：
    - `dotTone` 由三态改为按 `embedding_status` 细分：ready绿 / stale橙 / failed红 / partial黄 / 未索引·未生成灰；禁用（文件层或切片层任一）→ 灰覆盖。
    - ChunkNav chip 的 hover 从浏览器原生 `title` 升级为 radix `Tooltip`：显示 段落路径/块类型 + 索引状态中文 + N 个假设问题。
    - ChunkToolbar 移除常驻 `StatusDot` 文字 +「已禁」+「N问」徽章，状态只留圆点+hover 一处；保留段落/页码 meta 与操作按钮（「重新向量」needsEmbed 高亮保留，作为当前切片的可执行信号）。`fileDisabled` 不再传入工具栏。
    - 新增状态筛选：滚轴右侧「漏斗」(ListFilter) 按钮 + `Popover` 多选（按数据动态列出实际出现的 embedding 状态 + 禁用，每项带同色圆点与计数）。**口径**：选中后滚轴只显示命中 chip（保留绝对 #编号），自动定位到首个命中切片，prev/next 在命中集内移动，「切片 N/总数」提示，支持「清除」。filter 随文件切换复位（`<ChunkNav key={fileId}>`）。
  - `web/src/components/ui/popover.tsx`：新增 `@radix-ui/react-popover` wrapper（风格对齐 `tooltip.tsx`：surface / radius / shadow / portal）。
- 构建：`tsc -b && vite build` 通过、无类型错误；dist 仍 1 JS + 1 CSS；产物 CSS 已含 `--color-stale` 且被 `var(--color-stale)` 引用。后端起 `:8765` 自检：根路径服务新 JS(`index-CgJjjdpZ.js`)、`/api/import/files` 返回真实数据（DB 连通）。
- 待验收：本轮视觉/交互（每状态颜色、hover 文案、筛选）交用户浏览器强刷验收 —— 本会话未暴露视觉 MCP，无法自动截图；Playwright 也未安装进本项目。沿用 [[feedback_lightweight_verification]] 暂不补单测。
- 两处待用户拍板：① FAQ 圆点配色随全局 `TONE_COLOR` 一并改动（如不想动 FAQ 可单独还原）；② 筛选口径取「隐藏未命中」，若更想「保留全部、仅高亮/淡化」可切换。


## Embedding 按钮统一 + FAQ 禁用功能（后端已自检，前端待浏览器验收）

- 用户要求（经选择题确认）：① 文档**头部**「生成 embedding」按钮改名 **Embedding** + 换 **Waypoints**（交错线）图标 + 逻辑改成「只嵌非绿」（自动跳过 ready 与禁用切片）；切片工具栏的**单片**「重新向量」保持不动（按 `改头部·工具栏留单片`）。② FAQ 侧同步：embed 按钮同样 Embedding+Waypoints；并**新增 `is_disabled` 列**给 FAQ 做禁用功能（与切片禁用正交、完全平行，按 `新增 is_disabled 列`）。③ 顺带答疑「部分索引」= 某切片的向量单元（1 parent + N child）只嵌了一部分（部分 ready、其余未生成且无 stale/全失败），属一种「非绿」态。

### 后端
- `sql/001_init.sql`：`faq_documents` 末尾追加幂等 `ALTER TABLE … ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT false`（沿用本仓 import_files/import_chunks 的迁移惯例）。已 `init-db` 应用到线上库（60 条 FAQ 全部 default false）。
- `customer_service_agent/db/faq.py`：`list_faqs` 取数列补 `is_disabled`；`list_embedding_candidates` 加 `AND COALESCE(is_disabled,false)=false`（批量/候选不再嵌禁用项）；新增 `set_faq_disabled(faq_id, is_disabled)`（UPDATE + RETURNING 同列形状）。
- `customer_service_agent/db/knowledge.py`：**两条检索路径都过滤 FAQ 禁用**，保证助手真的查不到——
  - 旧路径 直查 `search()`（`rag.py`/`rag_tool.py`/`cli.py` 用）：WHERE 加 `AND COALESCE(is_disabled,false)=false`。
  - 新路径 统一 `knowledge_chunks`（`_search_knowledge_sql` 向量 + `_search_knowledge_text_sql` 关键词）：各加 `LEFT JOIN faq_documents fq ON kc.source_type='faq' AND fq.id=kc.source_id` + `AND COALESCE(fq.is_disabled,false)=false`（FAQ 投影 source_id=faq.id，JOIN 口径正确）。docstring 同步更新。
- `customer_service_agent/admin_server.py`：① `embed_import_file` 在取到 `list_import_chunks`（返回 `ic.*` 含 is_disabled + 派生 embedding_status）后，先过滤出 `not is_disabled and embedding_status != 'ready'` 的「非绿」切片再嵌入，跳过 ready/禁用，省重复调用。② 新增 `set_faq_disabled` handler（镜像 `set_import_chunk_disabled`：校验 is_disabled 必填、NotFound 处理、返回 `{item}`）。③ do_POST 加路由 `/api/faqs/{id}/disabled`（放在 `/embed` 之前，endswith 区分）。

### 前端（已 `tsc -b && vite build` 通过，dist 仍 1 JS + 1 CSS：index-8rWQzo02.js / index-Bec67WiE.css）
- `web/src/components/ui/status-dot.tsx`：新增导出共享 `embedDotTone(status, disabled)`（一色一态：禁用灰覆盖 / ready 绿 / stale 橙 / failed 红 / partial 黄 / 其余灰）。`chunk-browser.tsx` 改用该共享函数（删本地同名副本），FAQ 列表/抽屉同源复用，三处配色彻底统一。
- `web/src/api/schemas.ts`：`Faq` 加 `is_disabled: boolean`。
- `web/src/api/hooks.ts`：新增 `useToggleFaqDisabled`（乐观更新 `['faqs']` 列表 + `['faq', id]` 详情，失败回滚）与 `useEmbedPendingFaqs`（POST `/api/faqs/embed-pending`，一次 ≤200 条）。
- `web/src/pages/documents/document-drawer.tsx`：头部按钮 Sparkles→**Waypoints**、文案「生成 embedding」→**Embedding**；`staleCount`→`nonGreenCount`（`!is_disabled && embedding_status!=='ready'`）；徽章显示 nonGreenCount；全绿时（chunks 已加载且 nonGreenCount===0）禁用按钮。切片工具栏单片「重新向量」**未改**。
- `web/src/pages/faqs/faq-drawer.tsx` / `faq-list.tsx`：embed 按钮 Sparkles→Waypoints + 文案 Embedding；状态点改 `embedDotTone(status, is_disabled)`、禁用显「已禁用」灰点；抽屉底部新增 禁用/启用 切换按钮（Power/PowerOff，沿用切片禁用交互）；列表禁用行 `opacity-60` 变暗；删两处本地 `mapEmbed`。
- `web/src/pages/FaqsPage.tsx`：顶部工具栏「新建 FAQ」左侧新增批量 **Embedding**（Waypoints）按钮 → `useEmbedPendingFaqs`，徽章提示当前页「非绿」数（真正处理范围是全库候选，故仅作提示）。

### 自检（后端 + HTTP，均通过）
- ruff：3 个改动后端文件全过。
- DB 只读校验脚本：`is_disabled` 列 boolean/NOT NULL/default false；`list_faqs` 带 is_disabled；候选过滤、两条统一检索（新 FAQ JOIN）、直查 `search()`、`set_faq_disabled` RETURNING 均执行无 SQL 错。
- 重启 server 后 HTTP：首页服务新 JS；`/api/faqs` 列表项带 is_disabled；`POST /api/faqs/{id}/disabled` 置真→item.is_disabled=true、置假→回滚（**测试用的 jszx_qa_0059 已还原 false**）、缺字段→HTTP 400。
- **待用户浏览器强刷验收**：本会话未暴露视觉 MCP，Waypoints 图标观感、Embedding 按钮非绿计数/禁用态、FAQ 禁用灰点与行变暗、批量 Embedding toast，交用户实测；沿用 [[feedback_lightweight_verification]] 暂不补单测。

### 已知技术债（本次未动，单独记一笔）
- **两条 FAQ 检索栈并存**：旧 `db.search()` 直查 `faq_documents`（`rag.py`/`rag_tool.py`/`cli.py` 链路）与新 `knowledge_chunks` 统一检索（`retrieval.py` 链路）是历史并行实现。本次为保证「FAQ 禁用真正生效」**两条都加了过滤**；后续应收敛为单一检索栈（统一走 knowledge_chunks），删除直查路径，避免每加一个过滤条件都要改两处。属独立清理项，需用户排期，**不在本次范围**。

