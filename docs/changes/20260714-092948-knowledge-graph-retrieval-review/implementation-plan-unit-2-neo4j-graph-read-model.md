# Neo4j Graph Read Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development task-by-task. PostgreSQL 永远是唯一写模型；不得新增 graph backend enum、双读、双写真源、兼容 adapter 或 fallback。本计划不自动提交 Git commit。

**Goal:** 建立由 PostgreSQL transactional outbox 驱动、可重建且 fail-closed 的 Neo4j usable graph 读模型，并在 parity 门通过后把子图与 KG seed 查询一次性硬切到 Neo4j。

**Architecture:** KG mutation 在 PostgreSQL canonical entity/relation/evidence 同一事务写 ID-reference outbox。单实例 projector 按最新 canonical 状态幂等 reconcile 到 Neo4j；checkpoint 与 Neo4j `KgProjectionMeta` 通过 epoch/applied-token/revision 双向围栏。图查询只读 Neo4j，随后仅按稳定 fact ID 回 PostgreSQL 展开 authoritative live evidence。

**Tech Stack:** PostgreSQL 16、psycopg 3、Neo4j Community LTS `5.26.28`、Python driver `neo4j==6.2.0`、Cypher、FastAPI、React/TypeScript、pytest、Docker Compose。

---

## File map

- Modify: `sql/001_init.sql` — outbox/checkpoint、synthetic 清理顺序和 FAQ/document-only source constraint。
- Create: `cyclops/db/graph_projection.py` — PostgreSQL outbox、checkpoint、snapshot、fence 与 checksum queries。
- Modify: `cyclops/db/__init__.py` — 组合 `GraphProjectionMixin`，只导出当前 graph DTO。
- Modify: `cyclops/graph.py` — 在 Unit 1 的 `GraphFactHit` 基础上增加 projection fence/error/state 与 canonical checksum。
- Create: `cyclops/neo4j_graph.py` — Neo4j schema、reconcile、meta、full-text seed、subgraph 和 checksum adapter。
- Create: `cyclops/graph_projector.py` — projector、resume、rebuild、drain/verify 与 advisory-lock orchestration。
- Modify: `cyclops/db/kg.py`, `cyclops/db/models.py`, `cyclops/kg.py`, `cyclops/retrieval.py` — mutation outbox、stable fact ID evidence expansion 与 synthetic runtime 删除。
- Modify: `cyclops/config.py`, `.env.example`, `pyproject.toml`, `uv.lock` — Neo4j/worker 唯一配置和精确 driver pin。
- Create: `compose.neo4j.yml` — loopback-only Neo4j 5.26.28 Community service。
- Create: `systemd/cyclops-graph-projector.service.template` — 独立 projector 进程。
- Modify: `scripts/install_user_service.sh` — 安装现有服务时同步渲染 projector unit，不内嵌凭据。
- Modify: `cyclops/cli.py`, `cyclops/admin_server.py`, `cyclops/asgi_app.py` — graph commands、503 error transport、projection status 与 Neo4j-only subgraph。
- Modify: `web/src/api/schemas.ts`, `web/src/api/hooks.ts`, `web/src/pages/KnowledgeGraphPage.tsx`, `web/src/pages/evaluation/result-panel.tsx`, `web/src/pages/SettingsPage.tsx`, `web/src/pages/settings/settings-model.ts` — projection health、stable fact wire 和配置。
- Create: `tests/test_graph_projection_db.py`, `tests/test_graph_projector.py`, `tests/test_neo4j_graph.py`, `tests/test_graph_projection_integration.py` — PostgreSQL/Neo4j 单元与跨库集成门。
- Modify: `tests/test_db.py`, `tests/test_kg.py`, `tests/test_kg_postgres_concurrency.py`, `tests/test_retrieval.py`, `tests/test_admin_server.py`, `tests/test_asgi_app.py`, `tests/test_config.py`, `tests/test_cli.py` — 删除 synthetic/PG graph runtime 断言并验证新唯一契约。
- Modify: `web/src/api/schemas-contract.test.ts`, `web/src/pages/evaluation/*.test.ts` — 删除 `fact_chunk_id`，使用 stable fact identity。

### Task 1: 建立 PostgreSQL outbox/checkpoint 与事务原子性

- [ ] **Step 1: 写 schema 和 pending-scan 失败测试**

`tests/test_graph_projection_db.py` 先断言目标 schema：

```sql
CREATE TABLE IF NOT EXISTS kg_graph_projection_outbox (
    id UUID PRIMARY KEY,
    entity_ids TEXT[] NOT NULL DEFAULT '{}',
    relation_ids TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    projected_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    CHECK (cardinality(entity_ids) + cardinality(relation_ids) > 0)
);

CREATE INDEX IF NOT EXISTS kg_graph_projection_outbox_pending_idx
ON kg_graph_projection_outbox (created_at, id)
WHERE projected_at IS NULL;
```

checkpoint 固定一行 `projection_name='kg_graph_v1'`，state 只允许 `uninitialized/rebuilding/ready/failed`；初始 epoch 为 canonical zero UUID，applied token 为 null。测试断言 pending query 只按 `projected_at IS NULL ORDER BY created_at,id LIMIT`，不存在 `id > checkpoint`。

- [ ] **Step 2: 运行 RED**

Run:

```bash
python -m pytest tests/test_graph_projection_db.py -q
```

Expected: 表、mixin 和方法尚不存在而失败。

- [ ] **Step 3: 实现 `GraphProjectionMixin` 当前 API**

```python
class GraphProjectionMixin:
    """维护 KG 图读模型 outbox、checkpoint、快照和查询围栏。"""

    def enqueue_kg_graph_outbox_in_conn(
        self,
        conn: Any,
        *,
        entity_ids: Sequence[str],
        relation_ids: Sequence[str],
    ) -> UUID:
        """在 canonical mutation 同一事务写去重排序后的 ID reference event。"""

    def list_pending_kg_graph_events_in_conn(
        self,
        conn: Any,
        *,
        batch_size: int,
    ) -> list[dict[str, Any]]:
        """按提交可见的 created_at/id 顺序读取 pending，不使用高水位。"""

    def load_projectable_kg_graph_in_conn(
        self,
        conn: Any,
        *,
        entity_ids: Sequence[str] | None,
        relation_ids: Sequence[str] | None,
    ) -> dict[str, list[dict[str, Any]]]:
        """从当前 canonical 状态读取精确 projectable nodes/edges 与端点 closure。"""
```

同文件实现 mark projected、increment attempt、checkpoint conditional update、read/verify fence、repeatable-read full snapshot 和 canonical checksum queries；所有变更方法检查 affected row count。

- [ ] **Step 4: 把所有 KG mutation 接到同一事务 outbox**

在 Unit 0 已完成的 evidence→reconcile 末尾汇总实际受影响 entity/relation IDs，只写一个 outbox event。必须覆盖 extraction snapshot、confirm、needs_review/disabled、FAQ/document 删除/禁用/正文变化/重解析、entity cascade relation。没有变化的 ID 集合不写空事件。

测试用故意抛错的连接验证 canonical mutation rollback 时 outbox 也不存在；outbox insert 失败时 canonical mutation 同样 rollback。

- [ ] **Step 5: 运行 GREEN 与真实 PostgreSQL 门**

Run:

```bash
python -m pytest tests/test_graph_projection_db.py tests/test_db.py -k "outbox or invalidat or confirm or snapshot" -q
TEST_DATABASE_URL="$TEST_DATABASE_URL" python -m pytest tests/test_kg_postgres_concurrency.py -q
```

Expected: PASS，既有 entity→relation 锁序不改变。

### Task 2: 固定 graph 领域类型、Neo4j 配置与 schema

- [ ] **Step 1: 写 graph types/config 失败测试**

目标 DTO：

```python
@dataclass(frozen=True)
class GraphFactHit:
    """用 PostgreSQL 业务 ID 表示图 seed 命中，不暴露存储内部 ID。"""
    fact_id: str
    fact_type: Literal["entity", "relation"]
    fact_rank: int
    seed_score: float


@dataclass(frozen=True)
class GraphProjectionFence:
    """保存一次 fenced read 的 PostgreSQL epoch/token/revision。"""
    projection_epoch: UUID
    applied_token: UUID
    projection_revision: int
```

配置只允许 `NEO4J_URI/USERNAME/PASSWORD`、connection/query timeout 和三个 projector 参数；不存在 `NEO4J_ENABLED`、database name 或 backend selector。参数必须为正值；默认 database 固定 `neo4j`。

- [ ] **Step 2: 写 Neo4j schema 失败测试**

用 fake driver 捕获并断言只创建：

```cypher
CREATE CONSTRAINT kg_entity_id IF NOT EXISTS
FOR (entity:KgEntity) REQUIRE entity.id IS UNIQUE

CREATE CONSTRAINT kg_relation_id IF NOT EXISTS
FOR ()-[relation:KG_RELATION]-() REQUIRE relation.id IS UNIQUE

CREATE CONSTRAINT kg_projection_meta_name IF NOT EXISTS
FOR (meta:KgProjectionMeta) REQUIRE meta.projectionName IS UNIQUE
```

以及设计中的两个 `cjk`、同步 full-text indexes。schema verify 要求 analyzer 存在、constraint/index ONLINE、driver server version 5.26.28、恰好一个 `KgProjectionMeta`。

- [ ] **Step 3: 运行 RED**

Run:

```bash
python -m pytest tests/test_config.py tests/test_neo4j_graph.py -k "config or schema or analyzer or meta" -q
```

Expected: graph 模块和依赖尚不存在而失败。

- [ ] **Step 4: 实现依赖、配置和 adapter 生命周期**

`pyproject.toml` 精确加入 `neo4j==6.2.0` 并更新 lock。`Neo4jGraphStore` 只接受一个 settings object，调用 `GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password), connection_timeout=settings.neo4j_connection_timeout_seconds)`；每条 query 使用 `neo4j.Query(text, timeout=settings.neo4j_query_timeout_seconds)`。`close()` 幂等关闭 driver，但不吞连接错误。

- [ ] **Step 5: 增加本地 Compose**

`compose.neo4j.yml` 固定：

```yaml
services:
  neo4j:
    image: neo4j:5.26.28
    restart: unless-stopped
    ports:
      - "127.0.0.1:7474:7474"
      - "127.0.0.1:7687:7687"
    environment:
      NEO4J_AUTH: "${NEO4J_USERNAME}/${NEO4J_PASSWORD}"
    volumes:
      - cyclops_neo4j_data:/data
      - cyclops_neo4j_logs:/logs

volumes:
  cyclops_neo4j_data:
  cyclops_neo4j_logs:
```

Compose 不写默认密码，不开放非 loopback，不配置第二 database。

- [ ] **Step 6: 运行 GREEN**

Run:

```bash
uv lock --check
python -c 'import neo4j; assert neo4j.__version__ == "6.2.0"'
python -m pytest tests/test_config.py tests/test_neo4j_graph.py -k "config or schema or analyzer or meta" -q
docker compose -f compose.neo4j.yml config
```

Expected: PASS。

### Task 3: 实现幂等 reconcile projector 与失败恢复

- [ ] **Step 1: 写 batch/closure/idempotency 失败测试**

测试必须覆盖：

```text
outbox merge/dedupe preserves every selected event ID
relation event automatically loads head/tail entity closure
delete selected relationship IDs first
upsert projectable entities
remove only known KG_RELATION edges before DELETE unavailable KgEntity
create projectable relations after endpoints
same batch replay produces identical nodes/edges/counts
unknown relationship attached to KgEntity fails without DETACH DELETE
```

Neo4j transaction 返回的 deleted/upserted/created counts 必须与预期逐项相等。

- [ ] **Step 2: 写三段提交与崩溃测试**

`project_pending_batch()` 顺序固定：

```text
Neo4j reconcile + new applied token commit
→ PostgreSQL mark exact selected events + same token + revision commit
```

分别模拟 Neo4j commit 前、Neo4j commit 后/PG mark 前、PG mark 后进程退出；重跑都必须收敛且不重复边。

- [ ] **Step 3: 运行 RED**

Run:

```bash
python -m pytest tests/test_graph_projector.py -k "batch or closure or replay or crash or poison" -q
```

Expected: projector 尚不存在而失败。

- [ ] **Step 4: 实现单实例 projector**

```python
class GraphProjector:
    """以 PostgreSQL advisory lock 串行投影、retry 和 rebuild。"""

    def run(self) -> None:
        """只在 checkpoint ready 时按固定 poll interval 消费 pending。"""

    def project_pending_batch(self) -> int:
        """幂等 reconcile 一批事件，并完成 Neo4j token→PG token 配对提交。"""

    def resume_failed_projection(self) -> None:
        """显式重试 failed_event，不清零 attempt history 或跳过 pending。"""
```

advisory lock 用专用 PostgreSQL session 全程持有；batch size 200、poll 1 秒、retry 5 是默认配置。批失败后逐事件定位毒事件，只给实际失败行增加 attempt；达到上限把 checkpoint 置 failed 并停止。

- [ ] **Step 5: 运行 GREEN**

Run:

```bash
python -m pytest tests/test_graph_projector.py -q
```

Expected: PASS；代码中不存在 `DETACH DELETE` 或 pending ID 高水位。

### Task 4: 实现 repeatable-read rebuild、checksum 与 ready 门

- [ ] **Step 1: 写 rebuild state-machine 失败测试**

覆盖：新 epoch/rebuilding 原子发布、repeatable-read snapshot、精确 visible event IDs、节点后关系写入、T0 token 配对、退出 snapshot 后 drain、无 pending verify snapshot、canonical checksum、verify 后新 pending 触发再次循环、条件更新 ready。

canonical JSON 规则固定为字段名排序、aliases 去重排序、UTC RFC3339、JSON null 保留；checksum 使用 SHA-256 lowercase hex。

- [ ] **Step 2: 运行 RED**

Run:

```bash
python -m pytest tests/test_graph_projector.py -k "rebuild or checksum or verify or epoch" -q
```

Expected: rebuild/checksum 尚未实现而失败。

- [ ] **Step 3: 实现 rebuild/check**

```python
def rebuild(self) -> GraphProjectionStatus:
    """在同一 advisory lock 内重建、排空、校验并条件发布 ready。"""

def check(self) -> GraphProjectionStatus:
    """只读比较 PG/Neo4j IDs、属性、计数、checksum 和 meta identity。"""
```

重建只执行 `MATCH ()-[r:KG_RELATION]->() DELETE r` 与 `MATCH (n:KgEntity) DELETE n`；未知关系使节点删除失败。不得清空无 label 数据或增加蓝绿 generation。

- [ ] **Step 4: 运行 GREEN**

Run:

```bash
python -m pytest tests/test_graph_projector.py -q
```

Expected: PASS。

### Task 5: 实现 epoch/token/revision fenced graph reader

- [ ] **Step 1: 写 fail-closed 失败测试**

每次 read 必须执行：

```text
PG before: state ready + pending 0 + epoch/token/revision
Neo4j same read transaction: matching meta + graph query
PG after: same ready/pending/epoch/token/revision
```

分别测试并固定错误 code：

```text
kg_graph_uninitialized
kg_graph_rebuilding
kg_graph_projection_lagging
kg_graph_projection_failed
kg_graph_unavailable
kg_graph_projection_mismatch
```

任一检查失败都丢弃结果并抛 `KgGraphReadError`；没有返回空图或 PostgreSQL 查询的分支。

- [ ] **Step 2: 写 subgraph parity query 测试**

`hops` 只接受 1/2 并选择两条固定 Cypher。测试 center missing、unusable、isolated、connected、entity/relation filter、全局 stable edge limit 和无向遍历；两阶段 reachable/edge query 在同一 Neo4j transaction/meta token 下。

- [ ] **Step 3: 写 graph full-text seed 测试**

用户文本先逐字符转义 Lucene 保留字符 `+ - && || ! ( ) { } [ ] ^ " ~ * ? : \\ /`，再由代码生成 term/OR query并参数化传入。entity/relation index 各取 top-N，只按 rank RRF 合并，输出：

```python
GraphFactHit(fact_id="kg_ent_1", fact_type="entity", fact_rank=1, seed_score=4.2)
```

不直接比较两种 Lucene raw score。

- [ ] **Step 4: 运行 RED**

Run:

```bash
python -m pytest tests/test_neo4j_graph.py -k "fence or subgraph or fulltext or lucene" -q
```

Expected: reader 尚未实现而失败。

- [ ] **Step 5: 实现 reader 与 PostgreSQL evidence consumer**

`Neo4jGraphStore.read_subgraph()` 和 `search_graph_facts()` 都只在 fenced read coordinator 内调用。`Database.expand_graph_fact_hits(hits)` 直接以 `fact_type/fact_id` join `kg_evidence` 和 live FAQ/document direct child；不接受 synthetic chunk ID、metadata locator 或 ID prefix。

- [ ] **Step 6: 运行 GREEN**

Run:

```bash
python -m pytest tests/test_neo4j_graph.py tests/test_db.py tests/test_retrieval.py -k "graph or kg" -q
```

Expected: PASS。

### Task 6: CLI、API、settings 与管理页 health

- [ ] **Step 0: 完成 UI 布局 checkpoint**

在修改 KG/Settings React 页面前，给用户一份“现有 KG 工具页顶部增加紧凑 projection health strip，ready 才开放图探索；审核表始终可用”的 UI prompt。收到布局图或用户明确允许沿用现有布局后再执行 React 步骤；后端 CLI/API 不受该 checkpoint 阻塞。

- [ ] **Step 1: 写 CLI/API 失败测试**

parser 必须提供：

```text
graph-project
graph-project --resume
graph-rebuild
graph-check
```

新增 `GET /api/kg/projection/status`；`GET /api/kg/subgraph` wire 保持 usable-only，但唯一实现注入 fenced Neo4j reader。`KgGraphReadError` 映射 HTTP 503，body 为 `{"error": <message>, "code": <fixed-code>}`。

- [ ] **Step 2: 实现 config/settings 生命周期**

Neo4j URI/username/password 可为空以允许非图服务启动；缺失时 projection status 为 uninitialized，graph API 与 `use_kg=true` 返回 503。`python -m cyclops check-config` 在完整部署门要求三项齐全并验证 driver/schema/meta；`use_kg=false` 检索、审核和导入继续可用，这不是 fallback。

settings 更新时密码空字符串沿用现有 secret-preserving 语义；URI/auth/timeout 变化关闭旧 driver、清除 graph reader 与 retrieval cache。ASGI lifespan 同时关闭 PG pool 与 Neo4j driver。

- [ ] **Step 3: 实现 projection health UI**

KG 页显示 state、pending、last projected、node/edge count、last checksum time 和有界错误。非 ready 时只禁用图探索/`use_kg=true`，实体/关系审核表仍正常工作；mutation 成功后失效 entity/relation/subgraph/projection-status queries。

- [ ] **Step 4: 运行 GREEN**

Run:

```bash
python -m pytest tests/test_cli.py tests/test_admin_server.py tests/test_asgi_app.py tests/test_config.py -k "graph or neo4j" -q
cd web && npm test && npm run typecheck && npm run lint
```

Expected: PASS。

### Task 7: 同次硬切并删除 PostgreSQL graph runtime/synthetic chunks

- [ ] **Step 1: 先写 absence tests**

测试要求以下符号在 production module 不存在：

```text
Database.get_kg_subgraph
Database._kg_subgraph_sql
Database.search_kg_knowledge_text
Database._search_kg_knowledge_text_sql
KgFactHit.fact_chunk_id
build_kg_entity_knowledge_chunk_row
build_kg_relation_knowledge_chunk_row
```

confirm/status/invalidation 测试改为 canonical row + outbox，不再断言 synthetic `knowledge_chunks`。

- [ ] **Step 2: 按安全顺序迁移 schema**

顺序固定：

```sql
-- 1. 先利用仍存在的 synthetic rows 精确清理 eval expected ids。
-- 2. 再删除 synthetic rows。
DELETE FROM knowledge_chunks
WHERE source_type IN ('kg_entity', 'kg_relation');

-- 3. 最后阻止再次写入 synthetic source type。
ALTER TABLE knowledge_chunks
ADD CONSTRAINT knowledge_chunks_source_type_check
CHECK (source_type IN ('faq', 'document'));
```

repeatable migration 先按 constraint name 检查，不创建第二个同义 constraint。

- [ ] **Step 3: 删除全部 runtime/helpers/tests**

删除 Neo4j audit 中列出的 PG subgraph SQL、KG weighted ILIKE、synthetic builders/私有 helpers、projection status helpers、fact_chunk DTO 和前端 key；保留 entity/relation/evidence 审核、revision/locks、outbox、stable fact evidence expansion、FAQ/document BM25/vector/RRF。

- [ ] **Step 4: 静态门**

Run:

```bash
rg -n "get_kg_subgraph|_kg_subgraph_sql|search_kg_knowledge_text|_search_kg_knowledge_text_sql|fact_chunk_id|kc_kg_|build_kg_(entity|relation)_knowledge_chunk_row|DETACH DELETE" cyclops web/src
rg -n "GRAPH_BACKEND|graph_backend|use_postgres_graph|postgres.*neo4j|neo4j.*postgres" cyclops web/src
```

Expected: production code 零命中；migration 中精确 synthetic cleanup 可由限定扫描单独允许。

### Task 8: 跨库集成、崩溃恢复、parity 与交付门

- [ ] **Step 1: 启动精确 Neo4j 容器**

Run:

```bash
docker compose -f compose.neo4j.yml up -d
docker compose -f compose.neo4j.yml exec neo4j cypher-shell -u "$NEO4J_USERNAME" -p "$NEO4J_PASSWORD" "CALL dbms.components() YIELD versions RETURN versions[0]"
```

Expected: 版本为 5.26.28。

- [ ] **Step 2: 运行跨库 integration**

Run:

```bash
TEST_NEO4J_URI=bolt://127.0.0.1:7687 \
TEST_NEO4J_USERNAME="$NEO4J_USERNAME" \
TEST_NEO4J_PASSWORD="$NEO4J_PASSWORD" \
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
python -m pytest tests/test_graph_projection_integration.py -q
```

必须验证 duplicate consume、四类 crash、poison event/resume、dirty graph rebuild、source disable lag 503、node/edge/property/checksum parity 100%、center/filters/limit parity 和 full-text stable fact IDs。

- [ ] **Step 3: 运维命令验收**

Run:

```bash
python -m cyclops init-db
python -m cyclops graph-rebuild
python -m cyclops graph-check
python -m cyclops check-config
curl -fsS http://127.0.0.1:8765/api/kg/projection/status
```

Expected: checkpoint/meta epoch+token 配对、pending 0、checksum 一致、state ready。

- [ ] **Step 4: 更新 specs/change log 并执行全量门**

Run:

```bash
uv lock --check
python -m pytest
python -m ruff check .
python -m cyclops check-config
cd web && npm test && npm run typecheck && npm run lint && npm run build
git diff --check
```

更新 `.trellis/spec/backend/cyclops-db-contracts.md`、retrieval/ASGI/settings contracts、frontend KG contracts 和本 change 目录的实际验证结果。全部门通过后才启动 projector/Admin 服务供用户验收。
