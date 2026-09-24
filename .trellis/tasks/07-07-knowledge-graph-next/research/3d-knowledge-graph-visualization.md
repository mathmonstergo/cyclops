# 3D 知识图谱可视化权威方案调研

调研日期：2026-07-15（Asia/Shanghai）

## 目标

用户希望知识图谱后期提供 3D 点线关系探索。目标不是制作装饰性动画，而是形成可用的图工作台：搜索实体、渐进展开关系、辨识类型与方向、聚焦局部网络、查看实体/关系详情和回溯 evidence。

## 当前项目约束

- 前端是 React 19 + Vite，KG 页面已按路由懒加载，但没有 Three.js 或图布局依赖。
- 当前 `/api/kg/subgraph` 固定只返回 usable 事实，输入是单个中心实体、hops 和 limit；前端当前请求 1 hop、limit 40。
- KG 页面以实体/关系审核表和右侧证据抽屉为主；3D 视图不应替代审核列表和 evidence 真相来源。
- 数据仍保存在 PostgreSQL + pgvector；不应为了展示层引入 Neo4j 或独立图数据库。
- 管理页是本地内部工具，但仍需控制 bundle、主线程占用、WebGL 资源和可访问性。

## 权威技术底座

### Three.js

Three.js 是成熟的通用 JavaScript 3D 库，提供 WebGL/WebGPU renderer、scene、camera、geometry、material、ray casting 和各类控制器。它是 3D 渲染的权威底层，但不自带图数据模型、force layout、节点/边 picking 语义或图探索交互。

直接使用 Three.js 意味着项目自行负责：

- 节点/边 Object3D 生命周期与增量 diff；
- raycaster picking、hover/click、拖拽、镜头聚焦；
- force simulation 与渲染循环协调；
- 标签遮挡、方向箭头、选中高亮；
- 场景清理、context loss、resize 和性能治理。

它适合高度定制或极大规模专项优化，不是当前阶段最快、最稳的关系图产品层。

### react-force-graph-3d / 3d-force-graph（推荐渲染层）

`react-force-graph-3d` 是 `react-force-graph` 套件的独立 3D React 组件。官方说明：

- 3D 渲染使用 Three.js/WebGL；
- 布局使用 `d3-force-3d`，也可切换 ngraph；
- 原生支持 zoom/pan/rotate、node dragging、node/link hover/click；
- 接受稳定 `nodes + links` 数据，可增量更新；
- 支持节点/边颜色、大小、标签、可见性、自定义 Object3D、曲线、自环、方向箭头和粒子；
- 提供 click-to-focus、expand/collapse、multi-selection、fix dragged node、zoom-to-fit、大图等官方示例；
- 暴露 camera、scene、renderer、controls 和 Three.js post-processing，必要时可逐步定制；
- 提供 warmup/cooldown、pause/resume、pointer interaction 开关等性能控制。

当前官方 package 为 MIT；React peer dependency 接受任意 React，类型声明包含在包内。它把图领域的通用渲染与交互封装好，同时保留 Three.js 逃生口，最适合本项目。

风险：Three.js 依赖会显著增加 KG 页面 chunk，因此 3D 组件还需在 KG 页面内部二次 lazy import；该生态主要由社区维护，必须通过 adapter 隔离数据契约，避免业务代码直接散落库专有 props。

### d3-force-3d

`d3-force-3d` 是 d3-force 的 1/2/3 维扩展，提供 link、charge、center、collision 等 force。官方文档明确：

- 默认初始分布和 random source 可使用固定种子，便于稳定布局；
- 节点可通过 `fx/fy/fz` 固定；
- simulation 可停止、手工 tick 或重新加热；
- 大型静态图布局建议放 Web Worker，避免阻塞 UI。

`react-force-graph-3d` 已封装它。本项目首版应通过 bounded scene、cooldown 后冻结和渐进扩展控制规模；只有真实基准显示主线程布局仍不可接受时，再引入 worker/服务端坐标，不提前自建第二套 layout。

## 权威产品交互参考

### Neo4j Bloom（推荐交互范式）

Neo4j Bloom 虽然主要是 2D 图产品，但其图探索信息架构比“3D 特效”更值得借鉴：

- **场景只包含搜索或探索得到的局部图。** 不默认加载整库。
- **search-first。** 用户从实体、关系或 graph pattern 搜索进入场景，而不是面对随机毛线球。
- **渐进探索。** Expand selection 查看选中节点的直接邻居；Reveal relationships 补出已选节点间尚未显示的边。
- **场景控制。** Fit to selection、Clear scene、dismiss unrelated/single nodes、选择相关节点。
- **清晰图例。** 图例列出实体类别和关系类型，显示可见数量，支持筛选、按类型选择和样式规则。
- **渐进详情。** hover 展示少量属性；单击选择；双击进入 Inspector；card list 以结构化列表展示场景内节点与关系详情。
- **多选有上下文动作。** Cmd/Ctrl 或框选多个元素，动作只在所选对象类型适用时出现。
- **样式有语义。** 按 category/type 配色，可用数据驱动规则调整节点大小/颜色，而不是随机彩色。

这些原则应直接映射到本项目的 3D 视图。Neo4j Bloom 是产品交互参考，不意味着引入 Neo4j 数据库或复制其完整查询语言。

### GraphXR（商业对照）

Kineviz GraphXR 提供浏览器图可视化、关系/关系库数据连接、布局、path finding、centrality、community detection、时间/地理分析等成熟能力。它证明高级图工作台后续可以演进到路径、中心性和社区分析。

但它是独立商业平台，会引入授权、数据连接、部署和 UI 割裂，不符合当前本地 PostgreSQL 单体边界。建议仅作为能力路线图参考，不作为当前实现依赖。

## 可访问性和 3D 的边界

MDN 明确说明 `<canvas>` 本身只是 bitmap，绘制对象不会像语义 HTML 一样暴露给辅助技术。因此 3D Canvas 不能成为唯一操作入口：

- 现有实体/关系表保留为 canonical 可访问视图；
- 搜索、图例、已选元素和 evidence 详情使用语义 HTML；
- Canvas 提供简短 fallback 描述和键盘可到达的配套结果列表；
- WebGL 不可用或 context lost 时显示明确错误和返回表格入口，不做静默空白。

这是同一产品的双视图，不是旧兼容路径。

## 三种实现方案

### A. react-force-graph-3d + 当前 PostgreSQL 图 API（推荐）

渲染层使用 `react-force-graph-3d`，底层沿用 Three.js + d3-force-3d；产品交互参考 Neo4j Bloom；后端继续从 PostgreSQL 提供 bounded usable subgraph。

- 优点：React 集成快；图交互能力完整；MIT；可按需深入 Three.js；不改变数据库。
- 缺点：KG 页面 bundle 变大；Canvas 可访问性需用 HTML 配套；超大场景仍需限制。

### B. Three.js + d3-force-3d 全自研

直接维护 Object3D、raycasting、controls 和 layout。

- 优点：定制自由，可针对特定规模做 instancing、worker 或服务端坐标。
- 缺点：需要自行重做成熟库已有的 picking、拖拽、镜头、增量和清理；测试面大，首版投入最高。
- 适用：真实基准证明 A 无法满足规模/交互要求后。

### C. 接入 GraphXR / Neo4j Bloom 一类独立产品

- 优点：高级分析和探索能力成熟。
- 缺点：授权、部署、数据同步、账户与界面割裂；Neo4j Bloom 还绑定 Neo4j 生态。
- 结论：不符合当前项目边界。

## 推荐产品设计（后期实施时确认）

### 页面结构

- KG 页面保留“实体 / 关系”审核表，并增加独立“3D 图谱”视图；3D 不承载审核写操作。
- 进入 3D 后先显示搜索入口或一个受限 graph snippet，不自动拉取全部图。
- 左上工具栏：实体搜索、实体类型/关系类型筛选、场景节点/边数量。
- 左侧可折叠图例：类型颜色、当前可见数量、显隐控制。
- 主区：3D canvas。
- 右侧复用现有详情抽屉：实体、关系、confidence、evidence 和来源跳转。

### 核心交互

- 单击节点：选择、聚焦并打开详情；单击边：查看关系详情与 evidence。
- 双击节点或显式“展开邻居”：请求该节点 1-hop 增量并合并到 scene。
- Cmd/Ctrl 多选；只显示所有选中项共同合法的场景动作。
- 提供聚焦选择、适应画布、重置镜头、清空场景、隐藏非相关节点。
- 节点按 entity type 稳定配色；边显示方向箭头并按 relation type 配色。
- 标签只在 hover、选中或近距离时显示；默认不启用持续粒子和强 bloom 特效，避免噪音与 GPU 浪费。
- 拖拽后允许 pin/unpin；force cooldown 后冻结，数据增量时再局部 reheat。

### 数据/API 原则

- 只展示 `usable` 事实，审核候选仍在表格工作台处理。
- 后端始终限制 hops、node/edge 数和单次扩展规模；不能依赖浏览器过滤全库结果。
- scene 以稳定 entity/relation ID 去重；增量扩展必须可重复调用且结果幂等。
- API 返回 `truncated/has_more` 等明确边界；超过限制时提示继续按节点展开，不能静默截断。
- 未来若增加最短路径、社区或中心性分析，作为独立查询能力，不把推导结果写回事实 KG。

### 性能策略

- KG 路由本身已 lazy load；3D renderer 再按“3D 图谱”视图二次动态加载。
- 初始 scene 采用保守上限并通过 100/300/1000 nodes 多档真实浏览器基准确定正式阈值，不凭经验声称无限规模。
- force 停止后暂停不必要动画；切换离开页面时释放 scene、renderer、事件和 WebGL 资源。
- labels、方向粒子、后处理效果逐项做性能开关；默认配置优先清晰和稳定。
- 若布局成为瓶颈，先评估 worker；若 draw call 成为瓶颈，再评估 instancing 或方案 B，不提前重写。

## 推荐结论

权威组合不是照搬某一个完整产品，而是：

> **Three.js 作为可信 3D 底座，`react-force-graph-3d` 作为专业图渲染/交互层，Neo4j Bloom 作为图探索产品范式。**

它能保留当前 PostgreSQL 与 React 架构，同时提供成熟的点线关系、镜头和增量探索能力。3D 应作为后期独立阶段；当前文档级 KG、evidence 和稳定 ID 先做好，后续才能得到可信而非漂亮但错误的图。

## 来源

- Three.js 官方站点与文档：https://threejs.org/
- Three.js 官方仓库：https://github.com/mrdoob/three.js
- react-force-graph 官方仓库与 API：https://github.com/vasturiano/react-force-graph
- react-force-graph 3D 示例：https://vasturiano.github.io/react-force-graph/
- 3d-force-graph 官方仓库：https://github.com/vasturiano/3d-force-graph
- d3-force-3d 官方仓库与 API：https://github.com/vasturiano/d3-force-3d
- Neo4j Bloom Visual Tour：https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/
- Neo4j Bloom Scene Interactions：https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/bloom-scene-interactions/
- Neo4j Bloom Search Bar：https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/search-bar/
- Neo4j Bloom Legend Panel：https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/legend-panel/
- Kineviz GraphXR：https://www.kineviz.com/graphxr
- MDN Canvas Accessibility：https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/canvas#accessibility

