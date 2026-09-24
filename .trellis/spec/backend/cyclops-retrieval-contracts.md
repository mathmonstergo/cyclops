# Cyclops Unified Retrieval Contracts

> Executable contracts for the Python hybrid retrieval service and every production caller.

## Scenario: One Canonical Hybrid Retrieval Service

### 1. Scope / Trigger

- Trigger: code changes retrieval, RAG prompts, CLI search/ask, WeChat, MCP, Admin assistant, or retrieval evaluation.
- Reason: every entry point must retrieve the same FAQ/document knowledge rows; a FAQ-only path or copied fusion flow creates contradictory answers and stale-data exposure.

### 2. Signatures

- Canonical model: `RetrievedKnowledgeChunk`.
- Service:
  - `HybridRetrievalService.retrieve(query: str, *, include_parent_context: bool, use_kg: bool) -> HybridRetrievalResult`
- Result fields:
  - `query`, `query_terms`, `query_embedding_dimensions`
  - `vector_documents`, `keyword_documents`
  - `candidates`, `parent_documents`
  - `kg_fact_hits`, `kg_expanded_candidates`
  - `candidate_limit`, `rerank_used`
- Database reads:
  - `list_retrieval_aliases()`
  - `search_knowledge(query_embedding, *, top_k, min_score)`
  - `search_knowledge_text(query, *, top_k, query_terms)`
  - `get_parent_context_chunks(child_ids)`
  - explicit KG debug only: `search_kg_knowledge_text()` and `expand_kg_fact_hits()`

### 3. Contracts

- `use_kg` has no default. Every production caller passes `False`; only retrieval evaluation may pass `True`.
- The service performs one fixed sequence:
  1. read active aliases;
  2. build query terms;
  3. embed the query;
  4. run canonical vector and lexical reads;
  5. optionally query and expand KG facts;
  6. fuse direct candidates with RRF;
  7. optionally rerank the fused pool;
  8. after final ranking, optionally backfill parent context.
- Alias, embedding, vector, and lexical failures propagate. There is no FAQ-only fallback or second search implementation.
- `candidates` contains only ranked direct knowledge rows. `parent_documents` is separate and may enter an answer prompt, but never RRF, `top_k`, evaluation metrics, search wire results, `top_score`, hit counts, or analytics chunk ids.
- `RetrievedDocument`, `KnowledgeMixin.search()`, FAQ-only SQL, and `RetrievedKnowledgeChunk.question/answer/category/source_date` aliases do not exist.
- Fusion, rerank, Admin DTOs, and RAG prompt formatting accept canonical models only; dict/anonymous-object compatibility shapes are errors.
- Current lexical retrieval is weighted PostgreSQL `ILIKE` over canonical `knowledge_chunks`. It is not BM25 and must not be named BM25.
- BM25 is a separate migration requiring a Chinese analyzer plus durable inverted-index term frequency, document frequency, and document-length statistics. Those postings may be owned internally by a search extension/engine; they do not require an application-visible token column.
- Entry points:
  - CLI search: `include_parent_context=False, use_kg=False`.
  - CLI ask and WeChat: `True, False`.
  - RagTool/MCP search: `False, False`.
  - RagTool/MCP answer: `True, False`.
  - Admin assistant: `True, False` after sensitive-query short-circuit.
  - Evaluation baseline: `False, False`; KG debug: `False, True`.

### 4. Validation & Error Matrix

- Caller omits `use_kg` -> Python `TypeError`; fix the caller instead of adding a default.
- Non-`RetrievedKnowledgeChunk` direct candidate or parent-context row -> `TypeError` before it reaches fusion, rerank, prompts, or wire serialization.
- Non-`KgExpandedCandidate` or non-`KgFactHit` KG expansion -> `TypeError`.
- KG expansion references a fact absent from the fact query -> `ValueError`; do not silently drop diagnostics.
- No direct hits -> empty `candidates`; answer callers may still generate the guarded no-context response.
- Rerank unavailable or provider returns no valid ranking -> keep the pre-rerank order according to the existing optional rerank contract.
- Alias/embedding/database failure -> propagate to the entry point's normal error transport; do not switch to old FAQ search.

### 5. Good/Base/Bad Cases

- Good: a document child is recalled by vector or lexical search, ranked once, and its parent is added only to the answer prompt.
- Good: MCP search returns canonical document provenance while preserving its established top-level wire keys.
- Good: KG debug expands a synthetic relation fact to the live FAQ/document evidence row and metrics use the original row id.
- Base: no hits returns an empty candidate list and no parent query.
- Bad: CLI or MCP calls `Database.search()` against `faq_documents`.
- Bad: Admin copies vector/keyword/RRF/rerank code instead of delegating to `HybridRetrievalService`.
- Bad: a missing alias table read is swallowed and retrieval silently continues without aliases.
- Bad: parent rows are appended to ranked results or analytics hits.

### 6. Tests Required

- Core tests assert document recall, alias expansion, explicit KG mode, KG-off default callers, rerank pool sizing, parent separation, and rejection of anonymous parent rows.
- DB SQL tests assert live FAQ `status` plus `embedding_status='ready'`, disabled document filters, direct parent exclusion, and exact parent lookup.
- RAG/RagTool tests assert canonical prompt provenance and parent-only answer context.
- CLI, MCP factory, Admin assistant, and evaluation tests assert delegation to the same service with explicit flags.
- Removal test asserts `Database.search`, `RetrievedDocument`, and compatibility properties are absent.
- Full Python pytest, Ruff, configuration check, frontend tests/lint/build, and a production-reference `rg` scan must pass.

### 7. Wrong vs Correct

#### Wrong

```python
embedding = embeddings.embed(question)
docs = database.search(embedding, top_k=top_k, min_score=min_score)
```

This is the removed FAQ-only path and cannot retrieve canonical document children.

#### Correct

```python
result = retrieval.retrieve(
    question,
    include_parent_context=True,
    use_kg=False,
)
prompt_docs = [candidate.document for candidate in result.candidates]
prompt_docs.extend(result.parent_documents)
```

The caller uses one service and keeps ranked evidence separate from parent context.

## Scenario: Evaluation Contract Version 2

### 1. Scope / Trigger

- Trigger: code changes evaluation run storage, list payloads, strategy controls, metrics, or KG diagnostics.

### 2. Signatures

- Run payload is exactly `{}` for baseline or `{"use_kg": true}` for KG debug.
- Stored `strategy` is exactly `retrieval_hybrid_v1` or `retrieval_hybrid_v1_kg_debug`.
- Stored `analysis.contract_version` is JSON number `2`.
- Case list field: `latest_runs: RetrievalEvalRun[]`.

### 3. Contracts

- `None`, `{"use_kg": false}`, string booleans, and extra fields are rejected.
- Each case returns at most the newest v2 run for each strategy, ordered baseline then KG debug. The singular `latest_run` field does not exist.
- Frontend overrides are keyed by case plus UI strategy; selection never falls back to the other strategy.
- Old runs with `contract_version != 2` are deleted idempotently because synthetic-candidate metrics cannot be converted safely.
- Synthetic expected ids are removed only through exact joins to KG tables/projections; id-prefix heuristics are forbidden.

### 4. Validation & Error Matrix

- Missing/unknown strategy at DB write -> `ValueError` before opening a connection.
- Missing/wrong contract version -> `ValueError`.
- No run for the selected strategy -> `null`/not-run UI state, not another strategy's result.
- Unknown fact-to-candidate association -> `ValueError`.

### 5. Good/Base/Bad Cases

- Good: one case retains independent baseline and KG debug results and the toggle switches between them.
- Base: only baseline exists; KG debug shows not run.
- Bad: running KG overwrites the baseline local override.
- Bad: old synthetic KG chunk ids remain labelable as expected hits.

### 6. Tests Required

- SQL tests assert v2 deletion, exact expected-id cleanup, fixed strategy ordering, and `latest_runs=[]` for no runs.
- Admin tests assert the two exact payloads, v2 analysis, and original-evidence metrics.
- Frontend helper tests assert independent override storage and exact strategy selection.
- Batch diagnostics and case-list metrics must use the currently selected strategy.

### 7. Wrong vs Correct

#### Wrong

```typescript
const run = overrideByCase[evalCase.id] || evalCase.latest_run
```

#### Correct

```typescript
const run = selectEvaluationRun(evalCase, overrides, strategy)
```

The active strategy is explicit and cannot consume the other strategy's result.
