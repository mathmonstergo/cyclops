# db 包拆分计划（Stage 1 架构重构）

> [!IMPORTANT]
> 本文是历史重构记录。当前检索只使用 `RetrievedKnowledgeChunk` 与 `HybridRetrievalService`，详见
> `.trellis/spec/backend/cyclops-retrieval-contracts.md`；不得恢复 `RetrievedDocument` 或 FAQ-only search。

## 背景

`customer_service_agent/db.py` 2157 行、101 个 def/class，是项目第一大文件。随 reranker + query analytics 加进来还在继续涨。同时 `admin_server.py` 也已经 2106 行。

调研前沿企业级 KB 系统的代码组织：
- **RAGFlow**：`api/db/db_models.py` 集中 ORM + `api/db/services/*_service.py` 一域一文件
- **Onyx (Danswer)**：`backend/onyx/db/{models.py, document.py, chunk.py, ...}` 一域一文件 + ORM 集中
- **Quivr**：`base + registry + implementations` 模式（适合多实现，本项目不适用）

本项目特点（与上面三者关键差异）：
- 不用 ORM，psycopg `dict_row` 直接写 SQL，无需 schema/model 分层
- 每个组件只有一个实现（chat、embedding、rerank client 各一），不需要 base+factory
- Database 是单类多方法的"上帝类"

## 范围

把 `customer_service_agent/db.py` 按业务域物理拆到 `customer_service_agent/db/` 包，**对外接口完全兼容**（mixin 模式），244 测试零修改通过。

不在本计划：
- admin_server.py 拆分（Stage 2 单独立项）
- 引入 ORM
- 顶层包重命名
- 任何业务行为变更

## 设计：Mixin

权衡过组合（`self.faq.upsert_faq`）vs mixin（`self.upsert_faq` 不变），选 **mixin**：

| 维度 | Mixin | 组合 |
|---|---|---|
| 调用方改动 | 零 | 大量（含测试） |
| 静态方法继承 | 自动（`Database._insert_*_sql()` 直接可用） | 需重导出 |
| 风险 | 低 | 高 |

5 个业务 mixin + BaseDatabase + pure-function builders + dataclasses，单文件 < 700 行。

## 目标结构

```
customer_service_agent/db/
├── __init__.py            # 重导出全部 public 接口；定义 Database = (FaqMixin, ..., BaseDatabase)
├── base.py                # BaseDatabase: __init__, connect, init_schema, _row_dict
├── models.py              # RetrievedDocument, RetrievedKnowledgeChunk + format_vector, score_to_distance
├── builders.py            # build_*_row / build_*_embedding_text / compute_*_hash / _clean_* / _format_* / next_embedding_status / empty_import_file_embedding_summary
├── faq.py                 # FaqMixin
├── knowledge.py           # KnowledgeMixin（含 search / search_knowledge / search_knowledge_text / get_parent_context_chunks）
├── imports.py             # ImportMixin（import_files/chunks/candidates/generation_jobs）
├── retrieval_meta.py      # RetrievalMetaMixin（eval_cases + aliases）
└── analytics.py           # AnalyticsMixin（query_analytics_events + cluster_summaries）
```

Database 主类：

```python
class Database(FaqMixin, KnowledgeMixin, ImportMixin, RetrievalMetaMixin, AnalyticsMixin, BaseDatabase):
    pass
```

## 实施顺序（逐域 + 跑 pytest）

1. 跑 pytest 录基线（244 passed）
2. 建 db/ 包骨架（空 mixin）
3. 迁 models + builders（纯函数最低风险）→ pytest
4. 迁 FaqMixin → pytest
5. 迁 KnowledgeMixin → pytest
6. 迁 ImportMixin → pytest
7. 迁 RetrievalMetaMixin → pytest
8. 迁 AnalyticsMixin → pytest
9. 删旧 db.py
10. 全量验证 + 填 confirmation.md

## 关键约束

- Database 类名不变，`customer_service_agent.db` 顶层可导入
- 所有公开函数/类零换名、零换位置（通过 `__init__.py` 重导出）
- 不改 SQL、不改业务行为
- 不动测试代码
- ruff clean + 244 测试零失败

## 验证命令

```bash
source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m pytest -q
source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m ruff check customer_service_agent tests
source /home/adam/miniconda3/etc/profile.d/conda.sh && conda run -n customer-service-agent python -m customer_service_agent.cli check-config
grep -rn "from customer_service_agent.db import" customer_service_agent tests | wc -l
wc -l customer_service_agent/db/*.py
```

## 需要用户确认

- mixin 方式 + 保持 import 兼容（推荐，已选）
- 渐进迁移、不动业务逻辑、不改测试
