# 企业级列表多选与批量操作调研

调研日期：2026-07-15（Asia/Shanghai）

## 问题

用户提出“只要有列表就得支持多选操作”。需要确定成熟设计系统如何定义多选、批量工具栏、跨页范围和后端批量语义，并明确本项目中哪些“列表”真正属于可批量操作资源。

## 官方设计系统观察

### IBM Carbon Data Table

Carbon 把 basic、selection、expansion 定义为不同变体，并没有要求所有列表默认多选。Selection 变体用于用户需要对所选资源执行单项或批量动作时：

- 行首 checkbox 选择；表头 checkbox 支持未选、全选和 indeterminate 三态；
- 选中第一项后，表格顶部切换为 batch action bar，并显示可应用于全部所选项的动作；
- 批量模式下应禁用行级单项图标和 overflow menu，避免作用域混淆；
- 取消按钮或清空选择退出批量模式；
- 常驻 toolbar 用于搜索、过滤、导出、表格设置等全局动作，批量动作与全局动作不混为一谈。

Carbon 的核心判断不是“视觉上像列表”，而是“用户是否在操作一组同类资源”。

### Microsoft Fluent DataGrid

Fluent DataGrid 通过可选的 `selectionMode="multiselect"` 启用多选；表头和行分别提供 “Select all rows” / “Select row” 的可访问标签，并支持 controlled selected item set。它同样把 selection 作为显式能力，不是 DataGrid 的无条件默认行为。

### Ant Design Table

Ant Design 通过可选 `rowSelection` 契约开启选择，使用稳定 `rowKey` 管理 selected keys，支持：

- checkbox 或 radio；
- 表头全选、隐藏全选、自定义 selection；
- controlled `selectedRowKeys`；
- `preserveSelectedRowKeys`，用于数据源分页/刷新后是否保留已选 ID；
- 行级 checkbox 禁用；
- 全选、反选、清空等自定义选择策略。

官方示例在操作按钮旁显示 `Selected N items`，动作执行完成后显式清空选择。这个模式强调：选择状态必须由资源稳定 ID 驱动，不能由当前数组下标驱动。

### Gmail 的跨页选择

Google Gmail 不会让表头 checkbox 无提示地代表整个查询结果。官方帮助文档描述的是两步模型：

1. 表头 checkbox 先选择当前页。
2. 页面显示“当前页 N 条已选”，再提供“选择此查询的全部 M 条”链接。

这避免用户误以为只操作当前可见页，却实际影响全部筛选结果。它也说明“选择当前页”和“选择所有匹配结果”必须是两个明确作用域。

## 后端批量契约观察

Google API Improvement Proposals 将 batch create/update/delete 设计为集合级显式方法，而不是前端并发调用 N 次单项接口：

- 同步 batch 必须是原子的；一个事务内全部成功或全部失败；
- 异步 batch 可以采用原子或 partial success，但 partial success 必须有可查询的 operation metadata 和逐项结果；
- 简单数据库 passthrough 更适合原子事务，跨系统或复杂长任务更适合异步逐项状态；
- batch request 需要明确最大数量和稳定资源标识。

映射到本项目：状态切换、删除等同库短事务应采用真正的原子 batch API；embedding、文档解析、KG 抽取等长任务应创建父任务和逐项状态，不应由浏览器 `Promise.all` 拼接单项请求后假装成批量事务。

## 行业共同模式

1. **多选属于有批量动作的同类资源集合。** 导航、步骤、证据、诊断明细、只读引用列表不因“长得像列表”就增加 checkbox。
2. **稳定 ID 驱动选择。** 排序、过滤、轮询刷新后不能选错行。
3. **选中后切换批量工具栏。** 明确显示所选数量，只呈现所有选中项共同合法的动作。
4. **表头 checkbox 是三态。** 未选、当前范围全选、部分选中必须可辨认。
5. **选择范围显式。** 默认选择当前页；若支持全部筛选结果，必须二次显式提升范围并展示总数。
6. **过滤或查询改变时清空选择。** 除非产品明确承诺跨查询保留，不能让不可见旧选择继续生效。
7. **危险动作二次确认。** 对话框写明数量、资源类型及不可逆影响；混合状态时说明哪些项不适用。
8. **单项与批量动作语义一致。** 但批量调用必须是独立集合级契约，不能用客户端 N 次请求替代。
9. **结果必须可解释。** 同步原子失败不改变任何项；异步 partial success 显示成功/失败数量和逐项原因。
10. **无合法共同动作就不提供多选。** 复选框本身不是功能，不能为了形式统一制造“选了却无事可做”的状态。

## 对本项目的范围建议

### 应纳入通用批量选择的一级业务资源

- 文档
- FAQ
- KG 实体
- KG 关系
- 评测用例

这些资源都有持久 ID、独立生命周期、筛选/分页和明确单项动作，适合统一 selection column + batch action bar。

### 需要按实际动作判断的次级资源

- 检索别名：若支持删除/启停多个 alias，应该多选；如果只在单个用例编辑器内即时维护，优先做字段级编辑而非资源表批量。
- 会话：只有提供批量删除/归档时才多选；当前纯导航侧栏不宜默认常驻 checkbox，可进入“管理会话”模式。
- 文档切片：当前主要是文档内浏览、证据定位和逐块审核；文档级 KG 入口移除后，不应仅因它是列表而增加无意义多选。若未来出现批量禁用/重新切分，再作为独立资源管理面设计。

### 不应纳入通用批量选择

- KG evidence / 来源引用
- 检索候选与评分明细
- 评测诊断步骤、批次统计明细
- 设置项、状态卡、导航菜单
- 只读来源、消息引用和面包屑

这些表面不是同类可变业务资源，或没有合法共同批量动作。

## 推荐交互契约（待用户确认）

- 所有“可变业务资源列表”统一在最左侧提供 checkbox；只有具备至少一个合法共同批量动作时启用。
- 第一项被选中后，原列表 toolbar 切换为紧凑 batch action bar：`已选择 N 项`、共同动作、取消选择。
- 表头全选默认只选当前页；若后端支持按当前过滤器执行全量操作，再显示 Gmail 式“选择全部 M 个匹配结果”，本轮不隐式跨页。
- 排序和同一查询的自动刷新保留选择；过滤词、状态筛选或页容量变化清空选择并给轻提示。
- 当前选中项状态不一致时，只展示全部项目共同合法的动作；不做“点击后静默跳过不适用项”。
- 删除/停用等危险动作需要数量化确认；同步数据库动作采用一个原子 batch API。
- 解析、embedding、KG 抽取等长动作创建 batch parent job，返回逐项进度；不由客户端并发单项 mutation。
- 选择状态只存在于当前页面会话，不写入全局持久 store。

## 范围边界与确认结果

“所有列表”有两种可能解释：

1. **所有可变业务资源列表**（推荐）：文档、FAQ、KG 实体/关系、评测用例，以及确有批量动作的别名/会话管理面。
2. **所有视觉列表**：连导航、证据、切片、候选、诊断和设置列表都放 checkbox。

第二种会产生大量没有合法共同动作的选择状态，也违背 Carbon/Fluent/Ant 将 selection 作为可选资源操作能力的做法。

用户于 2026-07-15 确认采用第一种推荐边界：所有具备合法共同批量动作的可变业务资源列表必须支持多选；导航、只读引用、候选、诊断和设置表面不加入通用多选。第一阶段覆盖文档、FAQ、KG 实体、KG 关系和评测用例。

## 来源

- IBM Carbon, Data Table Usage: https://carbondesignsystem.com/components/data-table/usage/
- Microsoft Fluent UI, DataGrid Multiple Select: https://github.com/microsoft/fluentui/blob/master/packages/react-components/react-table/stories/src/DataGrid/MultipleSelect.stories.tsx
- Ant Design, Table `rowSelection`: https://ant.design/components/table#rowSelection
- Ant Design 官方 selection operation 示例：https://github.com/ant-design/ant-design/blob/master/components/table/demo/row-selection-and-operation.tsx
- Gmail Help, “Delete messages in Gmail”: https://support.google.com/mail/answer/7401?hl=en&co=GENIE.Platform%3DDesktop
- Google AIP-233, Batch Create: https://google.aip.dev/233
- Google AIP-234, Batch Update: https://google.aip.dev/234
- Google AIP-235, Batch Delete: https://google.aip.dev/235
