# Neo4j 方案 B：可重建图读模型设计

状态：用户已确认方案，书面设计复核通过，进入实现计划与测试驱动实施。

确认日期：2026-07-15（Asia/Shanghai）

## 1. 目标

PostgreSQL 保持唯一业务与审核写模型；Neo4j 作为 transactional outbox 驱动、可全量重建的 usable graph 查询模型。

目标数据流：

```text
FAQ / document / extraction / review mutation
→ PostgreSQL canonical KG + evidence + outbox（同一事务）
→ graph projector
→ Neo4j usable entity/relation graph
→ subgraph / path / graph-seed search
```

切换后：

* KG 写入、来源、任务、审核、revision 和 evidence 只写 PostgreSQL。
* 子图与图搜索只读 Neo4j。
* Neo4j 可以从 PostgreSQL 全量重建，不拥有不可恢复的业务事实。
* Neo4j 不可用、投影落后、重建或校验失败时明确 HTTP 503；不查询 PostgreSQL 图作为 fallback。

## 2. 明确不做

Neo4j v1 不保存或接管：

* FAQ、document、chunk 或 parent context。
* document/FAQ embedding、pgvector 或主 RAG BM25。
* KG evidence 节点、原文 excerpt 或来源文件内容。
* extraction job、审核历史、revision command、评测、设置或 query analytics。
* PostgreSQL 与 Neo4j 双写双真源。
* PostgreSQL/Neo4j runtime backend switch、长期双读或静默 fallback。
* 3D WebGL 页面、GDS 算法和 3 跳以上产品能力；这些在稳定图读模型上另立后续阶段。

## 3. PostgreSQL 前置正确性

禁止把当前错误状态直接投影到 Neo4j。接 outbox 前先完成：

### 3.1 `source_count` 精确重算

删除 `GREATEST` 保留旧计数的语义。实体每次来源替换、删除、禁用或重解析后，都按实时有效、去重后的来源集合计算精确 `source_count`；关系只计算 evidence count。

### 3.2 来源失效状态

来源失效后：

* 零条实时有效 evidence：owner 进入 `disabled`，对应检索/图投影消失。
* 仍有其他来源 evidence：owner 进入 `needs_review`，等待重新人工确认，投影消失。
* owner revision 在状态或审核快照变化时递增。

来源从 disabled 恢复为可用时，未被正文变更流程删除的历史 evidence 重新进入实时计数；对应 owner 只恢复为 `needs_review` 并递增一次 revision，绝不自动恢复 `usable`。如果正文已变化或重解析已删除旧 evidence，则必须重新抽取。重复提交相同 enabled/disabled 值不改变 owner revision。

实体 `source_count` 的唯一公式为实时有效 evidence 上 `COUNT(DISTINCT (source_type, source_id, COALESCE(source_chunk_id, '')))`；关系不增加 `source_count`，其 `evidenceCount` 为 `COUNT(DISTINCT live_evidence.id)`。

`review_revision` 表示人工看到的审核快照版本：候选字段、evidence、endpoint context 或手工退审/禁用发生变化时递增；对同一 revision 执行 confirm 只把状态改为 usable，不递增；projector 永远不修改 revision。

实体降级后，incident relations 同步按相同 evidence/endpoint 门禁重算。所有写入按“evidence 删除/插入 → 状态与计数重算 → outbox”在同一 transaction 完成，并继续遵守 entity → relation 的全局确定性锁序。

## 4. Neo4j 图模型

### 4.1 节点

唯一 label：

```text
(:KgEntity)
```

属性：

* `id`：PostgreSQL 稳定业务 ID，唯一约束。
* `name`
* `entityType`
* `aliases`
* `description`
* `confidence`
* `sourceCount`
* `reviewRevision`
* `updatedAt`
* `status='usable'`

实体类型放在 `entityType` 属性，不创建动态 label。

### 4.2 关系

唯一 relationship type：

```text
(:KgEntity)-[:KG_RELATION]->(:KgEntity)
```

属性：

* `id`：PostgreSQL 稳定关系 ID。
* `relationType`
* `description`
* `confidence`
* `evidenceCount`
* `reviewRevision`
* `updatedAt`
* `status='usable'`

关系类型放在 `relationType` 属性，不生成动态 Cypher relationship type。API 永远使用 PostgreSQL ID，不暴露 Neo4j `elementId`。

Neo4j v1 固定使用 LTS Community image `neo4j:5.26.28` 与 Python driver `neo4j==6.2.0`。初始化创建：

```cypher
CREATE CONSTRAINT kg_entity_id IF NOT EXISTS
FOR (entity:KgEntity) REQUIRE entity.id IS UNIQUE;

CREATE CONSTRAINT kg_relation_id IF NOT EXISTS
FOR ()-[relation:KG_RELATION]-() REQUIRE relation.id IS UNIQUE;

CREATE CONSTRAINT kg_projection_meta_name IF NOT EXISTS
FOR (meta:KgProjectionMeta) REQUIRE meta.projectionName IS UNIQUE;
```

该 Community 容器由本项目独占并只使用默认 `neo4j` database。`KgEntity` 若连接任何非 `KG_RELATION` 关系，视为数据污染并使投影失败，projector 不得用 `DETACH DELETE` 连带清除未知数据。

### 4.3 投影资格

节点必须同时满足：

* PostgreSQL status 为 usable。
* 至少一条实时有效 evidence。

边还必须满足：

* 关系自身 usable 且至少一条实时有效 evidence。
* head/tail 两端节点都满足节点投影资格。

`sourceCount` 和 `evidenceCount` 都由 PostgreSQL 当前事实精确计算，projector 不做增量 `+1/-1`。

## 5. Graph-property 搜索边界

Neo4j 可以为已投影图属性创建同步 full-text index，只用于“从查询词找到图中的 seed entity/relation”：

* entity：`name`、`aliases`、`description`。
* relation：`relationType`、`description`。
* analyzer：Neo4j 内置 `cjk`。
* `fulltext.eventually_consistent=false`，不在 outbox 延迟之外叠加第二层最终一致。

唯一索引 DDL：

```cypher
CREATE FULLTEXT INDEX kg_entity_text IF NOT EXISTS
FOR (entity:KgEntity) ON EACH [entity.name, entity.aliases, entity.description]
OPTIONS {indexConfig: {
  `fulltext.analyzer`: 'cjk',
  `fulltext.eventually_consistent`: false
}};

CREATE FULLTEXT INDEX kg_relation_text IF NOT EXISTS
FOR ()-[relation:KG_RELATION]-()
ON EACH [relation.relationType, relation.description]
OPTIONS {indexConfig: {
  `fulltext.analyzer`: 'cjk',
  `fulltext.eventually_consistent`: false
}};
```

该索引不包含 FAQ/document/chunk，不能替代 PostgreSQL `pg_search + pdb.jieba` 主检索。它只是方案 B 图读模型的内部访问索引。

原始用户文本不能直接作为 Lucene query language 执行。Graph reader 必须转义 Lucene 保留字符，只允许代码生成的 term/OR 结构，并通过 procedure 参数传值。

KG debug 检索切换为：

```text
Neo4j graph-property search
→ stable entity/relation fact IDs
→ PostgreSQL live evidence expansion
→ original FAQ/document direct candidates
→ RRF
```

这是按所有权分层的组合读，不是同一事实双读：Neo4j 只返回图 fact ID/rank，PostgreSQL 只按这些稳定 ID读取 authoritative live evidence 和原始知识来源；两边不会各自返回一份可互相 fallback 的图事实。

Entity 和 relationship full-text index 分别返回各自 top-N；两路只按 rank 用 RRF 合成统一 graph fact rank，不直接比较两个 Lucene raw score。结果携带稳定 `fact_id + fact_type`，再进入 PostgreSQL evidence expansion。

唯一领域 DTO 为 `GraphFactHit(fact_id, fact_type, fact_rank, seed_score)`。Neo4j 切换提交直接替换该 DTO 的 PostgreSQL seed producer；consumer 与 evidence expansion 不识别 backend，也不接受 synthetic chunk ID。

切换时删除 PostgreSQL KG weighted `ILIKE`、KG synthetic `knowledge_chunks` 文本投影及其运行时搜索 helper；evidence expansion 改为接受稳定 fact ID/type，不再依赖 synthetic chunk ID。

## 6. Transactional outbox

### 6.1 表结构

```text
kg_graph_projection_outbox
- id UUID PRIMARY KEY
- entity_ids TEXT[] NOT NULL DEFAULT '{}'
- relation_ids TEXT[] NOT NULL DEFAULT '{}'
- created_at TIMESTAMPTZ NOT NULL
- projected_at TIMESTAMPTZ NULL
- attempt_count INTEGER NOT NULL DEFAULT 0
- last_error TEXT NULL
```

pending index 只覆盖 `projected_at IS NULL`。

运行状态：

```text
kg_graph_projection_checkpoint
- projection_name TEXT PRIMARY KEY  -- 固定 kg_graph_v1
- state TEXT                        -- uninitialized/rebuilding/ready/failed
- projection_epoch UUID NOT NULL
- applied_token UUID NULL
- projection_revision BIGINT NOT NULL DEFAULT 0
- last_projected_at TIMESTAMPTZ NULL
- failed_event_id UUID NULL
- projected_node_count BIGINT NOT NULL DEFAULT 0
- projected_edge_count BIGINT NOT NULL DEFAULT 0
- last_verified_checksum TEXT NULL
- last_verified_at TIMESTAMPTZ NULL
- last_error TEXT NULL
- updated_at TIMESTAMPTZ NOT NULL
```

checkpoint 是健康状态，不是唯一消费游标。

Neo4j 另有唯一运维节点：

```text
(:KgProjectionMeta {
  projectionName: 'kg_graph_v1',
  projectionEpoch: '2c1f0d4e-9390-4aef-855d-f7948b6e1686',
  appliedToken: 'de6a6ab4-0747-4f6a-9440-303a7f3b61e7',
  schemaVersion: 1
})
```

它不是业务图节点，不进入子图、搜索、计数或 3D API，只用于防止 URI 指向错误实例、Neo4j 被清空或恢复到旧副本后仍被 PostgreSQL `ready` 状态误判为健康。

Neo4j 属性不使用原生 UUID 类型：epoch/token 统一保存为 canonical lowercase UUID string，PostgreSQL UUID 比较前使用相同序列化。Reader、projector 和 rebuild 每次都断言恰好一个 `projectionName='kg_graph_v1'` 的 meta node。

### 6.2 写入点

下列 PostgreSQL domain mutation 必须在原事务内插入 outbox ID reference：

* extraction snapshot 原子替换。
* entity/relation confirm。
* entity/relation 退审或禁用。
* FAQ/document 来源删除、禁用、正文变化或重解析。
* entity 降级引起的 incident relation 变化。
* 后续物理删除。

outbox 不保存完整 Neo4j payload，避免形成第二份事实。Projector 始终按 ID 回读 PostgreSQL 最新 canonical 状态。

## 7. Projector

### 7.1 单实例与 pending 扫描

Projector 使用 PostgreSQL advisory lock 保证同一 projection 只有一个 active worker。每批扫描：

```sql
WHERE projected_at IS NULL
ORDER BY created_at, id
LIMIT :batch_size
```

不能只使用 `id > checkpoint`。并发事务可能先分配较小 ID、后提交；只按高水位会永久漏事件。

常驻 projector 只在 checkpoint=`ready` 时消费增量事件。`uninitialized` 必须先执行全量 `graph-rebuild`；`rebuilding` 由 rebuild 独占；`failed` 只能通过显式 `graph-project --resume` 或重新 rebuild 恢复，不能自动跳回 ready。

### 7.2 幂等 reconcile

每批合并 entity/relation IDs，再从 PostgreSQL 读取当前最终状态。每个 relation ID 必须自动展开其 head/tail entity closure，并把合格端点加入本批 entity upsert；projector 不能依赖实体事件先提交、先排序或位于同一批。

1. 删除 Neo4j 中这些 relation ID 的现有 `KG_RELATION` 边。
2. upsert 当前可投影实体。
3. 显式删除当前不可投影实体关联的 `KG_RELATION`，再用普通 `DELETE` 删除 `KgEntity`；发现未知关系则整批失败。
4. 创建当前可投影关系；关系两端必须在本批 closure 后存在。
5. 生成新的 UUID applied token，在同一 Neo4j transaction 更新 `KgProjectionMeta.appliedToken` 后提交。
6. 最后在同一 PostgreSQL transaction 标记本批 outbox `projected_at`、保存相同 applied token、递增 `projection_revision` 并更新 checkpoint。

Neo4j transaction 必须返回并断言预期的关系删除、节点 upsert、节点删除和关系创建数量；每条预期关系必须匹配两个端点。任何数量不一致都回滚 Neo4j，且不得标记 PostgreSQL outbox。

Neo4j commit 成功、PostgreSQL 标记前崩溃时，同一事件会重复执行；上述流程必须产生相同结果，不得出现重复边或累计计数漂移。

### 7.3 失败

网络、认证、约束或数据错误都记录 `attempt_count + last_error`。合并批次失败后，worker 按本次选中的 outbox 行逐行重试并定位毒事件；只给实际失败行增加 `attempt_count`。成功标记必须断言 affected rows 等于所选 outbox rows，`failed_event_id` 指向第一个达到上限的精确事件。

连续失败达到固定重试上限后 checkpoint 进入 `failed`，projector 停止推进，图 API 返回 503。恢复必须由明确的 projector retry 或 rebuild command 触发，不丢弃 pending event，也不切换 PostgreSQL 查询。

首版默认值固定为：batch size 200、poll interval 1 秒、单事件 retry limit 5；配置只能设为正整数/正数。

## 8. Rebuild 与 checksum

全量重建流程：

1. rebuild、projector 与 retry 使用同一 PostgreSQL session advisory lock，由专用连接全程持有。生成新 projection epoch，并用独立短 transaction 提交 checkpoint=`rebuilding`、新 epoch、`applied_token=NULL`；图 API 立即 503。
2. 打开 PostgreSQL `REPEATABLE READ` transaction，读取完整可投影节点/边，并记录该快照内可见的 pending event IDs。
3. 显式删除全部 `KG_RELATION`，再删除全部 `KgEntity`；未知关系使删除失败。不执行无 label 的全库清理。
4. 在 Neo4j 分批写入节点，再分批写入关系；完成快照写入后生成 token T0，在最终 Neo4j transaction 写入 `KgProjectionMeta(epoch, T0)`。
5. 在步骤 2 的 PostgreSQL transaction 中精确标记该快照可见的 event IDs，并写入相同 epoch/T0、递增 revision 后提交；不得用 ID 高水位批量标记。
6. 退出快照后，由 rebuild 在 `rebuilding` 状态和同一 advisory lock 下复用幂等 reconcile，排空后来提交的 pending events；每批继续执行 Neo4j token → PostgreSQL token 的配对提交。
7. 比较 PostgreSQL/Neo4j node IDs、edge IDs、计数和规范化属性 checksum。checksum 使用字段名排序的 canonical JSON；aliases 先去重排序，时间统一 UTC RFC3339，空值保持 JSON `null`。
8. 在新的 PostgreSQL `REPEATABLE READ` verify snapshot 中先确认该快照没有 pending，再计算 PostgreSQL checksum；同一 advisory lock 保证 Neo4j 不被其他 projector 修改，然后计算 Neo4j checksum。
9. 以条件 PostgreSQL transaction 确认 verify snapshot 之后仍没有 pending、epoch/token 一致，并把 checkpoint 从 `rebuilding` 改为 `ready`、保存 checksum/verified time。若出现新 pending，则回到 drain → verify 循环；只有真实校验/连接错误才进入 failed，绝不暴露部分图。

不增加蓝绿 generation。当前本地内部工具允许重建窗口明确不可用，这比同时维护两代图更干净。

## 9. 查询一致性与 fail-closed

每次 Neo4j 图查询：

1. 在 PostgreSQL 检查 checkpoint=`ready` 且没有 committed pending outbox，并记录 epoch、applied token 与 `projection_revision`。
2. 在同一个 Neo4j read transaction 内读取 `KgProjectionMeta` 并执行图查询；meta epoch/token 必须与 PostgreSQL 查询前值一致。
3. 再次检查 PostgreSQL checkpoint、pending、epoch/token 和 `projection_revision` 与查询前一致。
4. 任何一次检查或 token identity 失败都丢弃查询结果并返回 HTTP 503。

错误码保持可诊断：

* `kg_graph_uninitialized`
* `kg_graph_rebuilding`
* `kg_graph_projection_lagging`
* `kg_graph_projection_failed`
* `kg_graph_unavailable`
* `kg_graph_projection_mismatch`

不存在“失败后查 PostgreSQL 子图”的分支。

## 10. API 与命令

### 10.1 保持的产品 API

`GET /api/kg/subgraph` 的 usable-only wire contract 和缺失/isolated/connected 语义保持稳定，但唯一实现改为 Neo4j。保持产品 API 不等于保留旧数据库实现。

唯一查询语义：

* `hops` 只接受 1 或 2；代码选择两条固定 Cypher query，不拼接用户输入。
* 从 center 沿 `KG_RELATION` 按无向关系遍历，relation type filter 在遍历阶段生效。
* center 不存在时返回既有 404；存在但无输出边时返回 isolated。
* entity type filter 只过滤输出边：head 或 tail 任一端类型命中即可，不改变中间 reachable 集合。
* 对 depth `< hops` 的 reachable nodes 收集 incident `KG_RELATION`，先按稳定 relation ID 去重，再按 relation `updatedAt DESC, id ASC` 全局稳定排序，最后应用 1–200 的 limit。
* 两阶段 reachable/edge 查询必须在同一个 Neo4j read transaction 与同一个 projection token 下完成。

这一定义逐项复刻当前 PostgreSQL contract；parity 通过后 PostgreSQL SQL 被删除，Neo4j 语义成为唯一规范。

### 10.2 新增运维表面

* `GET /api/kg/projection/status`：返回 state、pending count、last projected time、counts/checksum 和有界错误；不返回凭证。
* `python -m cyclops graph-project`：运行常驻 projector。
* `python -m cyclops graph-project --resume`：执行专用 preflight，只要求连接、约束/index、projection epoch 可识别；允许 pending 和 token mismatch。它为达到上限的精确 failed event 增加一次显式重试额度，不清零历史 attempt。只有重试成功、pending=0 且 epoch/token 配对后，才以条件更新恢复 ready；否则保持 failed。
* `python -m cyclops graph-rebuild`：显式全量重建。
* `python -m cyclops graph-check`：只读 parity/checksum 健康检查。

Admin KG 页面显示投影健康状态；状态不是 ready 时禁用图探索入口并展示明确原因，审核表仍从 PostgreSQL 正常工作。

## 11. 配置与密钥

新增配置只进入 `config.py` 与本地 settings：

* Neo4j URI
* username/password
* connection/query timeout
* projector batch size/poll interval/retry limit

URI、密码和证书不得提交 Git。服务启动验证字段范围；图功能已切换后缺少配置时 checkpoint/health 明确为 `uninitialized`，所有 graph API 与 `use_kg=true` 评测返回 503，不能解释为“关闭 Neo4j”或改查 PostgreSQL。正式 `use_kg=false` FAQ/document 检索、管理审核和其他非图能力继续启动并可用。

`python -m cyclops check-config` 在完整部署验收中把缺少 Neo4j 配置视为失败，并明确列出缺失字段；普通服务进程不因图后端故障拖垮非图能力。

Community v1 固定使用默认数据库 `neo4j`，不暴露无实际用途的多数据库配置。初始化必须调用 `db.index.fulltext.listAvailableAnalyzers()` 验证 `cjk` 可用，并验证两个 uniqueness constraint 与两个 full-text index ONLINE；任何一项失败都不能换 analyzer 或降级。

Neo4j 与 Python driver 已锁定 LTS `neo4j:5.26.28` / `neo4j==6.2.0`，部署文档记录 Community/Enterprise 许可边界。版本升级必须先跑 rebuild/parity 集成门。

## 12. 切换与删除

切换前：

1. 完成 PostgreSQL source invalidation/source count P0。
2. 安装 Neo4j、约束、graph-property indexes、driver 与 projector。
3. 用非敏感合成图执行 parity、崩溃恢复和性能 POC。
4. 全量 rebuild、排空 outbox并通过 checksum。

同一次切换提交：

* `AdminApp.kg_subgraph()` 改为唯一 Neo4j graph reader。
* KG debug fact seed search 改为 Neo4j graph-property search。
* 删除 `Database.get_kg_subgraph()` 与 `_kg_subgraph_sql()`。
* 删除 PostgreSQL KG weighted `ILIKE` 与 synthetic KG `knowledge_chunks` 运行时投影。
* 删除对应旧 SQL tests/helpers，不增加 backend option。

保留 PostgreSQL：

* entity/relation/evidence 表和审核 API。
* extraction、review revision、来源治理和确定性锁序。
* fact ID → live evidence → original FAQ/document candidate expansion。
* FAQ/document vector、BM25、RRF、rerank、parent context 和评测结果。

## 13. 测试与验收

### PostgreSQL 单元/集成

* 每个 domain mutation 与 outbox 同事务提交；rollback 时两者都不出现。
* `source_count` 精确重算，零 evidence disabled，共享来源 needs_review。
* pending 扫描不因并发提交顺序漏事件。

### PostgreSQL + Neo4j 集成

* 重复消费不产生重复节点/边。
* entity/relation usable、needs_review、disabled 与 live evidence 门禁完全一致。
* Neo4j commit 前、commit 后/PG mark 前、PG mark 后以及 worker 重启四类崩溃点均可恢复。
* 禁用/删除来源后，在投影未追平期间图 API 只返回 503，不泄漏旧事实。
* dirty Neo4j 全量重建后 IDs、属性、计数和 checksum 100% 一致。

### 查询 parity

* 切换前离线对比当前 PostgreSQL usable subgraph 与 Neo4j，节点/边集合 100% 一致。
* center missing、center unusable、isolated、connected、limit 和 1/2-hop 语义一致。
* graph-property search 返回稳定 fact IDs，PostgreSQL evidence expansion 只生成实时原始知识候选。

### 性能

* 既定 1-hop/2-hop 和 bounded edge workload 记录 P50/P95/P99。
* Projector backlog、单批吞吐、rebuild 时间和 Neo4j 内存占用有可重复基线。
* POC 未达到现有响应目标时停止切换；不把 PostgreSQL runtime path保留为生产 fallback。

### 静态清理

* 正式代码不存在 PostgreSQL subgraph runtime SQL。
* 不存在 `postgres|neo4j` backend switch。
* Neo4j 不包含 FAQ/document/chunk/evidence 数据。
* API/DTO 不暴露 Neo4j internal element ID。

## 14. 后续演进边界

方案 B 稳定后，后续可以独立增加：

* 3–10 hop path/pattern query。
* shortest path、社区和中心性。
* bounded 3D scene 的 search-first、expand 和 path API。
* GDS 离线分析。

这些能力只能建立在 outbox、rebuild、checksum 和 fail-closed 已稳定的基础上，不能反向扩大 Neo4j 为 FAQ/document 统一知识读层。
