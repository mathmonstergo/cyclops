# Cyclops DB Contracts

> Project-specific contracts for the Python + PostgreSQL + pgvector knowledge-base backend.

## Scenario: Replacing Imported Chunks

### 1. Scope / Trigger

- Trigger: code modifies `ImportMixin.replace_import_chunks()` or any reparse path that replaces rows in `import_chunks`.
- Reason: platform assistant retrieval reads indexed document rows from `knowledge_chunks`; old document vectors can remain searchable if only `import_chunks` are replaced.

### 2. Signatures

- Python method: `ImportMixin.replace_import_chunks(file_id: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]`
- Database tables:
  - `import_chunks.file_id`
  - `knowledge_chunks.source_type`
  - `knowledge_chunks.source_id`

### 3. Contracts

- Before inserting replacement chunks for an import file, delete existing `knowledge_chunks` rows where:
  - `source_type = 'document'`
  - `source_id = file_id`
- Delete old `import_chunks` for the same `file_id` in the same connection context.
- Insert replacement chunks after both cleanup steps.
- The platform assistant must never be able to retrieve document chunks from a previous parse of the same file.

### 4. Validation & Error Matrix

- Missing `file_id` is not accepted by callers; callers must pass a concrete import file id.
- Database errors must propagate so the connection context rolls back partial replacement work.
- Empty `chunks` is valid and means the file has no replacement chunks; old document knowledge must still be removed.

### 5. Good/Base/Bad Cases

- Good: reparse `imp_1`, delete `knowledge_chunks` for `source_type='document' AND source_id='imp_1'`, delete old `import_chunks`, insert new chunks.
- Base: reparse produces no chunks; delete old knowledge and old chunks, return an empty list.
- Bad: delete only `import_chunks`; the assistant may still retrieve old `knowledge_chunks` because disabled filtering uses left joins.

### 6. Tests Required

- Unit test against `Database.replace_import_chunks()` with a fake connection:
  - Asserts a `DELETE FROM knowledge_chunks` call exists.
  - Asserts the delete filters `source_type = 'document'` and `source_id = %(file_id)s`.
  - Asserts knowledge cleanup happens before deleting `import_chunks`.

### 7. Wrong vs Correct

#### Wrong

```python
with self.connect() as conn:
    conn.execute("DELETE FROM import_chunks WHERE file_id = %(file_id)s", {"file_id": file_id})
```

#### Correct

```python
with self.connect() as conn:
    conn.execute(
        """
        DELETE FROM knowledge_chunks
        WHERE source_type = 'document'
          AND source_id = %(file_id)s
        """,
        {"file_id": file_id},
    )
    conn.execute("DELETE FROM import_chunks WHERE file_id = %(file_id)s", {"file_id": file_id})
```

## Scenario: Import Parse Job Progress Is A JSON Object

### 1. Scope / Trigger

- Trigger: code modifies `import_parse_jobs.progress`, persistent MinerU polling, parse-job serialization, or schema initialization.
- Reason: PostgreSQL JSONB also accepts strings, arrays, and JSON null. The persistent job contract is object-only, so accepting scalar values creates an invented format and breaks typed progress access.

### 2. Signatures

- Database field: `import_parse_jobs.progress JSONB NOT NULL DEFAULT '{}'::jsonb`
- Constraint: `import_parse_jobs_progress_object_check CHECK (jsonb_typeof(progress) = 'object')`
- Writers: `Database.update_import_parse_job_progress(...)`, `begin_import_parse_job_finalization(...)`, and `complete_import_parse_job(...)`
- Admin response field: `ImportParseJob.progress: Record<string, unknown>`

### 3. Contracts

- Every explicit `progress` write is a Python `dict`; serialization uses that object directly and never applies `value or {}`.
- psycopg returns the JSONB object as a dict. The admin layer copies it when serializing and derives `percent` without mutating the database row.
- There is no string JSON parser, state-string alias, array coercion, `None -> {}` conversion, or missing-field fallback.
- `import_files` has no provider locator or parse-progress columns. Fresh schema creation and repeatable migration install the job constraint exactly once and drop the old file runtime columns.
- If manually corrupted scalar JSONB already exists, adding the constraint fails visibly. Do not silently rewrite it to `{}`; inspect and correct the data explicitly before retrying migration.

### 4. Validation & Error Matrix

- Writer receives a string, list, JSON scalar, or `None` -> `TypeError("progress must be a JSON object")` before executing SQL.
- Admin reads a non-dict database value or a row missing the required field -> contract error; do not synthesize a progress object.
- Direct SQL attempts JSONB string/array/JSON null -> PostgreSQL `CheckViolation` naming `import_parse_jobs_progress_object_check`.
- Valid empty or populated object -> persisted and returned as an object.

### 5. Good/Base/Bad Cases

- Good: `{"state":"running","extracted_pages":3}` writes and returns the same object.
- Base: `{}` is the valid queued-job initial object; no file-level progress is synthesized.
- Bad: `'running'`, `'[]'`, or `'null'` is accepted merely because the column type is JSONB.
- Bad: the reader calls `json.loads()` to support a TEXT version that never existed.

### 6. Tests Required

- Unit tests reject string, list, and `None` before SQL execution and reject a scalar returned to the admin response builder.
- Schema text tests require the named `jsonb_typeof(...)=object` constraint.
- Real PostgreSQL tests migrate a legacy `import_files` table, assert old runtime columns are gone, create a production parse job, and assert direct scalar writes fail in separate transactions without changing its valid progress.

### 7. Wrong vs Correct

#### Wrong

```python
job["progress"] = json.loads(job["progress"] or "{}")
```

This silently turns `None`/empty lists into objects and stores strings as legal JSONB scalars.

#### Correct

```python
progress = fields["progress"]
if not isinstance(progress, dict):
    raise TypeError("progress must be a JSON object")
params["progress"] = json.dumps(progress, ensure_ascii=False)
```

The application and database enforce the same single object shape.

## Scenario: Document Parent Chunks As Context Only

### 1. Scope / Trigger

- Trigger: code modifies `KnowledgeMixin._search_knowledge_sql()`, `KnowledgeMixin._search_knowledge_text_sql()`, `KnowledgeMixin._get_parent_context_chunks_sql()`, or any retrieval fusion that reads `knowledge_chunks`.
- Reason: document parent chunks are broad context containers. If parent and child chunks compete as equal direct candidates, retrieval can duplicate hits and inflate assistant context.

### 2. Signatures

- Python methods:
  - `KnowledgeMixin.search_knowledge(query_embedding, *, top_k, min_score, status="usable")`
  - `KnowledgeMixin.search_knowledge_text(query_text, *, top_k, query_terms=None, status="usable")`
  - `KnowledgeMixin.get_parent_context_chunks(child_ids, *, status="usable")`
- Database fields:
  - `knowledge_chunks.source_type`
  - `knowledge_chunks.chunk_level`
  - `knowledge_chunks.parent_chunk_id`
  - `knowledge_chunks.embedding_status`

### 3. Contracts

- Document child chunks drive direct vector and keyword recall.
- Direct vector retrieval accepts only current document child rows with:
  - `(kc.source_type <> 'document' OR kc.chunk_level = 'child')`
- Legacy document `chunk` rows and document `parent` rows are both excluded; old layer values are not compatibility aliases for `child`.
- Direct keyword retrieval must use the same exclusion.
- Parent rows may still have embeddings and `embedding_status = 'ready'`; embedding generation and UI summaries must not depend on direct recall eligibility.
- `_get_parent_context_chunks_sql()` must not include the direct-recall parent exclusion; it exists specifically to read parent chunks for child hits.
- File-level and chunk-level disable filters must still apply to both direct retrieval and parent context retrieval.

### 4. Validation & Error Matrix

- Document parent chunk in `knowledge_chunks` -> not returned by direct vector/keyword search.
- FAQ rows with `chunk_level = 'parent'` -> not excluded by the document-only condition unless future FAQ semantics define otherwise.
- Child hit with valid `parent_chunk_id` -> parent can be returned by `get_parent_context_chunks()`.
- Disabled import file or disabled import chunk -> both child direct hits and parent context rows are filtered out.

### 5. Good/Base/Bad Cases

- Good: query hits a document child; assistant receives child plus parent context through explicit backfill.
- Base: query hits a FAQ row; FAQ retrieval behavior is unchanged.
- Bad: direct SQL returns both document parent and child for the same section, causing duplicate evidence and larger prompts.
- Bad: direct-recall exclusion is copied into parent context SQL, preventing parent backfill.

### 6. Tests Required

- Unit tests in `tests/test_db.py` must assert:
  - `_search_knowledge_sql()` contains `(kc.source_type <> 'document' OR kc.chunk_level = 'child')`;
  - `_search_knowledge_text_sql()` contains the same condition and neither query accepts `<> 'parent'` as an alias;
  - `_get_parent_context_chunks_sql()` still reads `parent.chunk_level = 'parent'` and retains disable filters.
- Assistant retrieval tests should cover child hits expanding with parent context when relevant.

### 7. Wrong vs Correct

#### Wrong

```sql
WHERE COALESCE(fq.status, kc.status) = %(status)s
  AND kc.embedding_status = 'ready'
```

This lets document parent and child chunks compete in the same direct candidate list.

#### Correct

```sql
WHERE COALESCE(fq.status, kc.status) = %(status)s
  AND kc.embedding_status = 'ready'
  AND (kc.source_type <> 'document' OR kc.chunk_level = 'child')
```

Direct recall stays child-first for documents, while parent context remains available through explicit backfill.

## Scenario: Retrieval Evaluation Candidate Labeling Payload

### 1. Scope / Trigger

- Trigger: code modifies `AdminApp.run_retrieval_eval_case()`, `retrieval_eval_item_payload()`, frontend evaluation candidate rendering, or expected hit labeling behavior.
- Reason: evaluation users must be able to label expected hits from readable candidates. Raw `source_id` / `chunk_id` alone is not enough because document drawer display numbers such as `#1` are UI-relative and do not equal knowledge chunk ids.

### 2. Signatures

- Python function: `retrieval_eval_item_payload(candidate: FusedCandidate) -> dict[str, Any]`
- Admin API:
  - `GET /api/retrieval/eval-cases`
  - `POST /api/retrieval/eval-cases`
  - `POST /api/retrieval/eval-cases/{case_id}/run`
- Frontend types:
  - `RetrievalEvalItem`
  - `RetrievalEvalCase.expected_source_ids`
  - `RetrievalEvalCase.expected_chunk_ids`

### 3. Contracts

- Every retrieved candidate stored in `retrieval_eval_runs.retrieved_items` must keep machine ids:
  - `id` = knowledge chunk id used for chunk-level expected hit matching.
  - `source_id` = FAQ id or import file id used for source-level expected hit matching.
  - `source_type` = exactly `faq` or `document`; synthetic KG facts are diagnostics, never final evaluation candidates.
- Candidate payloads must also expose the current readable/provenance fields:
  - `source_title`
  - `source_chunk_id`
  - `parent_chunk_id`
  - `chunk_level`
  - `section_path`
  - `page_start`
  - `page_end`
  - `block_type`
  - `content`
- Evaluation payloads do not repeat internal metadata or FAQ compatibility fields such as `question`, `answer`, `category`, or `tags`; readable text comes from canonical `source_title` and `content`.
- Frontend labeling should prefer "run first, label from candidate" over manual id entry.
- One-click labeling should use one evaluation granularity at a time:
  - Source labeling writes `expected_source_ids` and clears `expected_chunk_ids`.
  - Chunk labeling writes `expected_chunk_ids` and clears `expected_source_ids`.
- Manual id entry may remain as an advanced path, but it must not be the primary workflow.

### 4. Validation & Error Matrix

- Non-`FusedCandidate` input or a candidate containing a non-`RetrievedKnowledgeChunk` document -> `TypeError`; do not serialize dictionaries or legacy document models.
- Candidate missing `source_id` or `id` -> invalid current run; do not persist or label it through a compatibility shape.
- Optional location fields such as page range or section path may be empty only when the canonical source genuinely has no such locator.
- Existing source-level expectation + user labels chunk -> source expectations are cleared to avoid hidden priority confusion.
- Existing chunk-level expectation + user labels source -> chunk expectations are cleared for the same reason.

### 5. Good/Base/Bad Cases

- Good: user runs an eval case, sees document title, page range, section path, excerpt, and clicks "expected chunk"; the case stores the knowledge chunk id.
- Good: user wants broad document/FAQ acceptance, clicks "expected source"; the case stores the FAQ/import file id.
- Base: a current FAQ candidate has no page/section locator but still carries its canonical FAQ id, title/content, and current diagnostic fields.
- Bad: UI asks the user to type `kc_doc_child_...` without showing how to find it.
- Bad: UI displays document drawer `#3` as if it were the chunk id used by evaluation metrics.
- Bad: source and chunk expectations are both set by one-click UI while metrics silently use only chunk ids.

### 6. Tests Required

- Unit test for `retrieval_eval_item_payload()` asserting readable fields are emitted.
- Unit test asserting dictionaries, anonymous objects, removed document models, and synthetic KG chunks are rejected as final candidate payloads.
- Admin run test should continue to assert metrics are recorded and candidate ids are present.
- Frontend lint/build must cover changed `RetrievalEvalItem` type and candidate labeling UI.
- Manual UI verification should cover:
  - run eval case;
  - mark a candidate as expected source;
  - mark a candidate as expected chunk;
  - confirm labels update without hand-copying ids.

### 7. Wrong vs Correct

#### Wrong

```json
{
  "id": "kc_doc_child_1",
  "source_id": "imp_1",
  "source_type": "document"
}
```

This is technically enough for metrics but not enough for a human to know which document block is being labeled.

#### Correct

```json
{
  "id": "kc_doc_child_1",
  "source_id": "imp_1",
  "source_type": "document",
  "source_title": "售后手册.pdf",
  "source_chunk_id": "chunk_1",
  "chunk_level": "child",
  "section_path": ["售后", "报告导出"],
  "page_start": 3,
  "page_end": 4,
  "content": "报告导出失败时，先检查账号权限和网络状态。"
}
```

The UI can now let users label the expected source/chunk from a readable candidate row instead of asking them to discover internal ids.

## Scenario: Knowledge Graph Review Projection and Explicit Retrieval

### 1. Scope / Trigger

- Trigger: code modifies KG extraction jobs, model parsing, candidate/evidence storage, review status, projection into `knowledge_chunks`, or explicit KG evaluation retrieval.
- Reason: KG facts are model-generated. The workflow must have one asynchronous contract, preserve source consistency, require human confirmation, and remain excluded from default answers.

### 2. Signatures

- Parser: `parse_kg_extraction_response(payload, *, source) -> dict[str, list[dict[str, Any]]]`
- Job DB methods:
  - `Database.create_kg_extraction_job(row) -> dict[str, Any]`
  - `Database.start_kg_extraction_job(job_id) -> dict[str, Any]`
  - `Database.fail_kg_extraction_job(job_id, error) -> dict[str, Any]`
  - `Database.get_kg_extraction_job(job_id) -> dict[str, Any] | None`
  - `Database.complete_kg_extraction_job(job_id, extraction, *, source_guard) -> dict[str, Any]`
- Review DB methods:
  - `Database.confirm_kg_entity(entity_id, *, expected_revision: int) -> dict[str, Any]`
  - `Database.confirm_kg_relation(relation_id, *, expected_revision: int) -> dict[str, Any]`
  - `Database.set_kg_entity_status(entity_id, status) -> dict[str, Any]`
  - `Database.set_kg_relation_status(relation_id, status) -> dict[str, Any]`
- Admin job methods:
  - `AdminApp.queue_kg_extraction_job(payload) -> dict[str, Any]`
  - `AdminApp.run_kg_extraction_job(job_id) -> dict[str, Any]`
  - `AdminApp.get_kg_extraction_job(job_id) -> dict[str, Any]`
  - `AdminApp.confirm_kg_entity(entity_id, payload) -> dict[str, Any]`
  - `AdminApp.confirm_kg_relation(relation_id, payload) -> dict[str, Any]`
- ASGI API:
  - `POST /api/kg/extraction-jobs` body: `{"source_type":"faq|document_chunk","source_id":"..."}`
  - `GET /api/kg/extraction-jobs/{job_id}`
  - `POST /api/kg/entities/{entity_id}/confirm` body: `{"expected_revision":<positive integer>}`
  - `POST /api/kg/entities/{entity_id}/status` body: `{"status":"needs_review|disabled"}`
  - `POST /api/kg/relations/{relation_id}/confirm` body: `{"expected_revision":<positive integer>}`
  - `POST /api/kg/relations/{relation_id}/status` body: `{"status":"needs_review|disabled"}`
  - `POST /api/retrieval/eval-cases/{case_id}/run` body is exactly `{}` or `{"use_kg":true}`

### 3. Contracts

- This is a 0-to-1 feature with one contract. Do not add synchronous adapters, source-type inference, generic lifecycle updates, public candidate-save bypasses, field aliases, or legacy-handler KG routes.
- `source_type` and `source_id` are both required. `source_type` is exactly `faq` or `document_chunk`.
- `POST /api/kg/extraction-jobs` validates the source, stores a `queued` job, schedules `BackgroundTasks`, and returns the queued row before model execution.
- The client polls `GET /api/kg/extraction-jobs/{job_id}` until `completed` or `failed`. Retrying a failed job creates a new queued job; failed job rows are terminal.
- Lifecycle transitions are explicit:
  - create always writes `queued`, zero counts, and no error;
  - start is an atomic `queued -> processing` claim;
  - fail only writes `processing -> failed` with an error bounded to 1000 characters;
  - complete only writes `processing -> completed`.
- `complete_kg_extraction_job()` is the only public candidate-completion entry. In one transaction it locks the job, verifies the job/source mapping, locks and fingerprints the live FAQ or document source, verifies every evidence locator matches the same guard, replaces the source snapshot, writes candidates/evidence, and writes completed counts.
- Model JSON uses exactly these top-level arrays: `entities` and `relations`. Non-arrays and non-object items are errors, not empty results.
- Relation fields are exactly `head`, `head_type`, `relation_type`, `tail`, `tail_type`, `description`, `confidence`, and `evidence`. No alternate field names are accepted.
- Entity and relation candidates always enter `needs_review`; repeated extraction moves affected candidates and projections back to `needs_review`.
- `confirm_kg_entity()` and `confirm_kg_relation()` are the only paths into `usable`. Generic status methods accept only `needs_review` or `disabled` and synchronize projections; entity demotion also invalidates attached relations.
- Entity and relation rows start with `review_revision = 1`. Re-extraction conflict upserts, source invalidation, manual demotion/disable, and relation cascade invalidation increment the affected row revision. Confirmation verifies the caller's revision under row lock but does not invent or infer a revision from timestamps or content.
- Review list payloads always return `review_revision`. A confirm request contains exactly one field, `expected_revision`, and the UI sends the selected row's current positive integer unchanged. There is no bodyless/id-only overload, optional/default revision, alias field, or compatibility route.
- Confirmation requires at least one currently valid evidence source. Relation confirmation also requires both endpoint entities to be `usable`.
- Relation confirmation reads its endpoint locator, locks endpoint entities by ascending entity ID, locks the relation, then rereads and validates the locator, evidence, and endpoint statuses. A combined join lock does not define a safe row order.
- Entity demotion locks the entity first and all attached relations by ascending relation ID before updating entity, relation, or projection state.
- Every write path follows one global two-phase owner-row order:
  1. Read semantic targets without row locks and keep source-affected IDs separate from lock-only IDs.
  2. Build the union of source-affected entities, candidate entities, and endpoints of directly affected relations. Process the entire union in ascending entity ID; candidate IDs perform their real upsert and other IDs take a single-row `FOR UPDATE`.
  3. After all entity operations finish, reread incident relations using a fresh READ COMMITTED statement snapshot. No later step may acquire an entity lock.
  4. Build the union of direct-source, incident, and candidate relations. Process the entire union in ascending relation ID; candidate IDs perform their real upsert and other IDs take a single-row `FOR UPDATE`.
  5. Only after owner locks are complete may status, revision, projection, and evidence writes run; evidence writes use deterministic evidence ID order.
- A direct relation's endpoints may be lock-only concurrency targets. Unless an endpoint is also source-affected or a candidate, its status, `review_revision`, `updated_at`, and `kg_entity` projection must remain unchanged. `source_entity_ids` and `entity_lock_ids` are different semantic sets and must not be reused interchangeably.
- Do not catch `DeadlockDetected` to retry/back off. Fix the deterministic lock graph. A user retry of a terminal failed extraction creates a new queued job and is unrelated to database deadlock retry.
- Default `search_knowledge()` and `search_knowledge_text()` exclude `kg_entity` and `kg_relation`. KG participates only when an evaluation/debug caller explicitly passes `use_kg=true`.

### 4. Validation & Error Matrix

- Missing or unsupported `source_type`, missing `source_id`, or any request field other than `source_type`/`source_id` at queue time -> `AdminValidationError`; no job is created.
- FAQ missing/not `usable`, or document file/chunk missing, disabled, or empty at queue time -> `AdminNotFoundError` or `AdminValidationError`; no job is created.
- Source changes after the model request -> fingerprint mismatch; transaction rolls back and the processing job becomes `failed`.
- Relation endpoint locator changes between the initial read and the locked context reread -> `ValueError`; no usable relation or projection is written.
- Job is not `processing`, guard does not belong to the job, or candidate evidence does not match the guard -> `ValueError`; no candidates or completed state are committed.
- Model payload is not an object; `entities`/`relations` is not an array; an array member is not an object; a canonical field is missing -> `KnowledgeGraphExtractionError` and job becomes `failed`.
- Unknown entity/relation type, missing evidence, empty excerpt, or invalid confidence -> `KnowledgeGraphExtractionError` and job becomes `failed`.
- Confirm body missing `expected_revision`, containing extra fields, or using a boolean, non-integer, or value below 1 -> `AdminValidationError` / HTTP 400 before database confirmation.
- Locked row revision differs from `expected_revision` -> `KgReviewConflictError` -> `AdminConflictError` -> HTTP 409; the UI must refresh the candidate and require a new manual confirmation.
- `status=usable` sent to a status endpoint -> `AdminValidationError`; callers must use the matching confirm endpoint.
- Empty object payload -> baseline and no KG lookup.
- `{"use_kg":false}`, `null`, string booleans, or extra fields -> `AdminValidationError`; no retrieval starts.

### 5. Good/Base/Bad Cases

- Good: POST with explicit FAQ type/id returns queued; background execution claims the job; GET later returns completed with counts; candidates remain `needs_review`.
- Good: a document chunk changes while the model runs; completion rejects the old fingerprint and GET returns failed with a bounded error.
- Good: a user confirms an evidence-backed entity; the entity becomes usable and a pending `kg_entity` knowledge projection is created.
- Good: a drawer opened at revision 3 submits `{"expected_revision":3}`; if re-extraction has advanced the row to 4, confirmation returns 409 and writes no usable projection.
- Good: relation-only evidence invalidation locks both endpoints before the relation, but only the relation revision/status/projection changes.
- Base: extraction returns canonical empty arrays; the source's previous candidates are retired and the job completes with zero counts.
- Base: evaluation payload `{}` remains the normal hybrid baseline.
- Bad: POST calls the model before returning, or a wrapper queues and immediately runs in the request thread.
- Bad: a public `save_kg_extraction_candidates()` or generic `update_kg_extraction_job(status="completed")` bypasses fingerprinting and atomic completion.
- Bad: confirmation accepts `{}`, an empty HTTP body, an id-only call, or a default revision and therefore confirms a snapshot the reviewer never saw.
- Bad: source replacement locks a relation and later upserts a candidate entity, or uses deadlock retry to hide the resulting relation-to-entity lock inversion.
- Bad: parser silently accepts `head_name` or drops malformed collection items and reports completed with zero counts.

### 6. Tests Required

- Parser tests assert canonical fields, strict object arrays, brace-safe fenced JSON, fixed enums, evidence requirements, and `needs_review` defaults.
- DB tests assert forced queued creation, atomic claim/fail transitions, absence of a generic status-update entry, locked processing completion, exact job/guard/evidence mapping, source fingerprint rejection, and candidates/counts committed through one connection.
- Review DB tests assert valid live evidence, usable relation endpoints, re-extraction invalidation, entity demotion cascade, and source deletion/reparse invalidation.
- Review revision tests assert initial value 1, every invalidation increment, exact expected-revision comparison under lock, no confirm default, and conflict mapping through HTTP 409.
- Recording concurrency tests assert the global entity-union then fresh incident-relation-union order, post-lock endpoint locator validation, stable candidate/evidence writes, and lock-only endpoints excluded from semantic updates.
- Real PostgreSQL tests require an explicit `TEST_DATABASE_URL`, a random isolated schema, at least two psycopg connections/threads, `lock_timeout`, bounded thread joins, and condition-based observation of a real lock wait. They must run snapshot replacement against production `confirm_kg_relation(..., expected_revision=...)`, assert no deadlock/timeout and correct final revisions, and verify relation-only source invalidation leaves lock-only endpoint rows/projections byte-for-byte unchanged. Recording fakes do not replace this gate.
- Admin tests assert queue does not call Chat, execution calls Chat only after start, failed is terminal, errors are bounded, and FAQ/document guards contain exact source ids/chunk ids/fingerprints.
- ASGI tests directly call the POST endpoint with `BackgroundTasks`, assert queued is returned before `run_kg_extraction_job`, then execute tasks and query the GET endpoint.
- Frontend tests assert queued/processing polling, completed-only success, backend failed error propagation, and unknown-status rejection.
- Retrieval tests assert KG remains absent by default and is present only in the explicit KG debug strategy.

### 7. Wrong vs Correct

#### Wrong

```python
def create_kg_extraction_job(payload):
    job = queue_kg_extraction_job(payload)
    return run_kg_extraction_job(job["id"])
```

This creates a synchronous second contract and blocks the request on model execution.

#### Correct

```python
@app.post("/api/kg/extraction-jobs")
async def queue_kg_extraction_job(request, background_tasks):
    job = admin.queue_kg_extraction_job(await _read_json(request))
    background_tasks.add_task(admin.run_kg_extraction_job, job["id"])
    return job
```

The POST response is queued, execution is background-scheduled, and the client observes the terminal state through the single GET polling contract.

## Scenario: KG Live Evidence Counts And Review Cache

### 1. Scope / Trigger

- Trigger: code modifies KG entity/relation review lists, subgraph edges, evidence source gates, source mutations, or React Query invalidation for KG review data.
- Reason: `kg_evidence` is an audit history. Its physical row count can differ from currently usable sources, so using `evidence.length` or a missing-field default can show stale facts as live and leave the review UI cached after a source change.

### 2. Signatures

- Database reads:
  - `Database.list_kg_entities(...) -> {"items": list[dict[str, Any]], "total": int}`
  - `Database.list_kg_relations(...) -> {"items": list[dict[str, Any]], "total": int}`
  - `Database.get_kg_subgraph(...) -> dict[str, Any] | None`
- Required wire fields:
  - `KgEvidence.is_valid: boolean`
  - `KgEntity.source_count: number`
  - `KgRelation.evidence_count: number`
  - `KgSubgraphEdge.evidence_count: number`
- Frontend invalidation:

```typescript
export async function invalidateKgReviewQueries(
  queryClient: QueryClient,
): Promise<void>
```

### 3. Contracts

- Entity and relation review lists return every historical evidence row. Each evidence object also has a mandatory `is_valid` boolean computed with the same live-source predicate used by review confirmation and projection.
- A FAQ evidence row is live only while the exact FAQ is `usable`. A document evidence row is live only while its exact file and exact chunk exist and both are enabled.
- `KgEntity.source_count` is the authoritative database column maintained from distinct live `(source_type, source_id, COALESCE(source_chunk_id, ''))` locators. The list passes it through; it never derives it from the historical evidence array.
- Relation-list and subgraph `evidence_count` count distinct live evidence IDs through the shared predicate. They do not count physical `kg_evidence` rows.
- All four wire fields are required. A missing `evidence_count` in a connected subgraph row is a contract error and must raise through `row["evidence_count"]`; do not synthesize `0` with `.get()`.
- The review table labels the entity value as `有效来源` and the relation value as `有效证据`. The drawer labels `证据历史`, may show its historical array length, and marks `is_valid=false` as low-contrast `来源已失效`.
- `invalidateKgReviewQueries()` is the only frontend KG review invalidator. It awaits the three query-key prefixes `['kg-entities']`, `['kg-relations']`, and `['kg-subgraph']`; TanStack prefix matching covers all filter/page/center parameters.
- KG confirm/status/extraction completion and every successful source mutation that changes live evidence reuse this helper: document delete, file enable/disable, chunk enable/disable, chunk text replacement, FAQ content/status save, and successful document snapshot replacement.
- Failed document parsing keeps the old snapshot and does not invalidate KG. Successful completion invalidates at the response/observer boundary rather than relying on a Drawer effect that may have unmounted.
- Entity, relation, and subgraph queries use `refetchOnMount: 'always'` because a persistent backend worker can change KG state while the user is on another route. This is freshness behavior, not a PostgreSQL/legacy fallback.

### 4. Validation & Error Matrix

- Historical evidence exists but its source is disabled/deleted/non-usable -> keep the row, return `is_valid=false`, and exclude it from live counts.
- Required SQL count alias is missing from a connected subgraph row -> `KeyError`; do not return a valid-looking edge with count zero.
- Source mutation fails or optimistic state rolls back -> do not call the KG success invalidator.
- Successful parse response is `needs_review` or `completed` -> invalidate all three KG query families.
- Parse response is `processing` or `failed` -> do not claim a replacement snapshot changed KG.
- Browser remounts the KG page after an out-of-band worker change -> refetch even if the old cache is inside its nominal stale window.

### 5. Good/Base/Bad Cases

- Good: an entity has two historical evidence rows but only one live distinct locator; the list returns both rows with different `is_valid` values and `source_count=1`.
- Good: deleting a document completes, then all parameterized entity/relation/subgraph caches become stale and active observers refetch.
- Base: an isolated usable entity returns no edges; no edge count is required because there is no relation row.
- Bad: UI renders `entity.evidence.length` under `有效来源`.
- Bad: subgraph serializer uses `row.get("evidence_count", 0)` and hides a broken SQL projection.
- Bad: only the open Drawer invalidates KG after parsing; closing it makes correctness depend on component lifetime.

### 6. Tests Required

- SQL contract tests assert historical `LEFT JOIN kg_evidence`, per-item `is_valid`, shared live predicate reuse, live relation/subgraph counts, and absence of the old physical `count(*)` subquery.
- Serializer tests pass a connected subgraph row without `evidence_count` and require `KeyError`.
- TypeScript contract tests require all four fields and prohibit optional `?` forms.
- UI contract tests assert table values use `source_count` / `evidence_count`, while only the explicitly historical drawer title uses `evidence.length`.
- Cache tests assert one shared helper, exactly three key families, all KG/source success callers, no failed-parse invalidation, and `refetchOnMount: 'always'` on all three KG reads.
- A real PostgreSQL smoke must execute entity/relation list SQL and compile the subgraph SQL, confirming boolean `is_valid` and integer count wire types.

### 7. Wrong vs Correct

#### Wrong

```typescript
<span>{item.evidence.length}</span>
```

```python
"evidence_count": row.get("evidence_count", 0)
```

Both turn audit history or a broken row shape into a live business fact.

#### Correct

```typescript
<span>{item.evidence_count}</span>
```

```python
"evidence_count": row["evidence_count"]
```

The backend owns live-state computation, and a missing required field fails visibly.

## Scenario: Canonical Document Knowledge Identity

### 1. Scope / Trigger

- Trigger: code creates document `knowledge_chunks`, derives parent/child rows, changes chunk indexes, serializes document provenance, or joins a document candidate back to `import_files` / `import_chunks`.
- Reason: import file ids, import chunk ids, and derived knowledge row ids identify different objects. Inferring one from another or from metadata breaks exact evidence lookup, drawer navigation, parent expansion, and evaluation labels.

### 2. Signatures

- Builder: `build_document_knowledge_chunk_row(chunk, import_file, *, knowledge_chunk_id: str) -> dict[str, Any]`
- Row derivation: `document_knowledge_rows_for_embedding(chunk, import_file) -> list[dict[str, Any]]`
- Child index: `child_knowledge_chunk_index(parent_index: int, child_index: int) -> int`
- Canonical retrieved model: `RetrievedKnowledgeChunk`
- Identity fields:
  - `knowledge_chunks.id`
  - `knowledge_chunks.source_type`
  - `knowledge_chunks.source_id`
  - `knowledge_chunks.source_chunk_id`
  - `knowledge_chunks.parent_chunk_id`
  - `knowledge_chunks.chunk_level`
  - `knowledge_chunks.chunk_index`

### 3. Contracts

- Every derived document row has `source_type = 'document'`, `source_id = import_files.id`, and `source_chunk_id = import_chunks.id`.
- The parent knowledge row has:
  - `id = 'kc_document_' + import_chunks.id`;
  - `chunk_level = 'parent'`;
  - `parent_chunk_id = NULL`;
  - the original non-negative import chunk index.
- Child knowledge rows have:
  - deterministic ids `kc_document_<import_chunk_id>_child_<1-based-index>`;
  - the same canonical `source_id` and `source_chunk_id` as their parent;
  - `chunk_level = 'child'`;
  - `parent_chunk_id` equal to the parent knowledge row id;
  - unique negative `chunk_index` values produced by `child_knowledge_chunk_index()` so they cannot collide with parent indexes under the `(source_type, source_id, chunk_index)` upsert key.
- Each source import chunk produces one parent and at least one direct child. Structured blocks take precedence, then `children_delimiter`, then the complete source text becomes the single child.
- Top-level identity and locator fields are authoritative. `metadata.chunk_id`, filename metadata, id prefixes, and rendered content are not alternate identity sources and must never repair a missing top-level field.
- Vector and lexical direct retrieval return document children only. Parents remain available only through exact `parent_chunk_id` lookup for prompt context.

### 4. Validation & Error Matrix

- `chunk.file_id != import_file.id` -> `ValueError`; no knowledge row is built.
- `chunk_level` is not exactly `parent` or `child` -> `ValueError`; do not infer a level from ids or metadata.
- No structured block and no effective delimiter split -> create one direct child from the complete source text.
- Duplicate derived child index for one file -> test/implementation defect; fix `child_knowledge_chunk_index()` rather than changing the upsert identity.
- Missing canonical `source_chunk_id` during document evidence or UI lookup -> invalid row; do not read `metadata.chunk_id` as a fallback.

### 5. Good/Base/Bad Cases

- Good: `imp_1/chunk_1` produces parent `kc_document_chunk_1` and child `kc_document_chunk_1_child_1`; both keep `source_id=imp_1` and `source_chunk_id=chunk_1`.
- Good: two structured blocks produce two direct children with distinct negative indexes and one shared parent id.
- Base: an unsplit source still produces exactly one parent plus one child.
- Bad: a child stores its synthetic knowledge id in `source_chunk_id`, so evidence can no longer open the original import chunk.
- Bad: a consumer extracts `chunk_1` from `metadata` or an id prefix when the top-level locator is missing.
- Bad: a parent competes with its children in direct retrieval.

### 6. Tests Required

- Builder tests assert file/chunk/knowledge ids remain distinct, `chunk_level` is mandatory, and mismatched file ids raise.
- Row-derivation tests cover no blocks, one block, multiple blocks, one delimiter segment, and multiple delimiter segments; every case must produce at least one child.
- Index tests assert every child index is negative, unique, and cannot collide with a real parent index.
- SQL tests assert vector and lexical searches exclude document parents and exact parent lookup keeps file/chunk disable filters.
- Serializer/evaluation tests assert document drawer and expected-hit locators use top-level `source_id` and `source_chunk_id`, never metadata or id parsing.

### 7. Wrong vs Correct

#### Wrong

```python
child = {
    "id": "kc_document_chunk_1_child_1",
    "source_id": "chunk_1",
    "source_chunk_id": "kc_document_chunk_1_child_1",
}
```

This conflates file, source chunk, and knowledge row identity.

#### Correct

```python
child = {
    "id": "kc_document_chunk_1_child_1",
    "source_type": "document",
    "source_id": "imp_1",
    "source_chunk_id": "chunk_1",
    "parent_chunk_id": "kc_document_chunk_1",
    "chunk_level": "child",
}
```

Every id names one object, so storage, evidence, UI navigation, and metrics can use exact equality.

## Scenario: KG Fact Evidence Expansion

### 1. Scope / Trigger

- Trigger: code changes KG text lookup, `kg_evidence`, `expand_kg_fact_hits()`, KG RRF fusion, or evaluation metrics/diagnostics.
- Reason: `kg_entity` and `kg_relation` projections are synthetic facts. They may explain an expansion but cannot become final answer/evaluation candidates or substitute for live FAQ/document evidence.

### 2. Signatures

- Fact lookup: `search_kg_knowledge_text(query_text, *, top_k, query_terms=None) -> list[KgFactHit]`
- Evidence expansion: `expand_kg_fact_hits(fact_hits: list[KgFactHit]) -> list[KgExpandedCandidate]`
- Synthetic fact model: `KgFactHit`
- Expanded original model: `KgExpandedCandidate(document: RetrievedKnowledgeChunk, kg_matches: tuple[KgFactHit, ...])`
- Fusion: `fuse_retrieval_candidates(*, vector_docs, keyword_docs, kg_candidates, top_k, rrf_k=60) -> list[FusedCandidate]`

### 3. Contracts

- Fact lookup returns synthetic ids only as `KgFactHit`; synthetic facts never enter `retrieved_items`, expected-hit metrics, answer sources, or direct candidate ids.
- Expansion starts from an exact usable `knowledge_chunks.id` whose `source_type` is `kg_entity` or `kg_relation`, and verifies the referenced entity/relation is still `usable`. A relation additionally requires both endpoint entities to be `usable`.
- Entity and relation facts join their own `kg_evidence` rows by exact foreign key. There is no metadata locator, source-type inference, id-prefix parsing, or parent-row fallback.
- FAQ evidence is eligible only when:
  - `kg_evidence.source_type = 'faq'` and `source_chunk_id IS NULL`;
  - the exact `faq_documents.id = source_id` is `usable` with a ready embedding;
  - the exact original knowledge row is the usable, ready FAQ chunk for that source.
- Document evidence is eligible only when:
  - `kg_evidence.source_type = 'document'` identifies the exact import file and import chunk;
  - both live rows exist and are not disabled;
  - the exact original knowledge row has matching top-level file/chunk ids, `chunk_level = 'child'`, `status = 'usable'`, and a ready embedding.
- Expansion groups by original `knowledge_chunks.id`, deduplicates repeated fact/original pairs, and preserves every distinct contributing `KgFactHit` in rank order.
- One original row receives one KG RRF vote based on its best fact rank. Other matches remain diagnostics and do not multiply the score.
- Evaluation computes metrics from the final original FAQ/document ids. `kg_fact_hits` and fact-to-original mappings remain separate analysis diagnostics; a fact with no live evidence is retained there with an empty expanded-id list.

### 4. Validation & Error Matrix

- Empty fact input -> `[]` without opening an expansion query.
- Fact projection, reviewed fact, endpoint, evidence source, or original knowledge row is missing/non-usable -> omit that expansion; do not infer another row.
- Document evidence resolves only to a parent row -> no candidate.
- Multiple facts resolve to one original row -> one expanded candidate with ordered, deduplicated matches.
- A returned expanded match references a fact absent from the original fact lookup -> `ValueError` when building diagnostics; do not silently drop the mismatch.
- Non-canonical KG/original model reaches fusion -> `TypeError`.

### 5. Good/Base/Bad Cases

- Good: a usable relation fact points through exact evidence to a live document child; the child receives one KG vote and the relation appears in its diagnostics.
- Good: two facts expand to the same FAQ row; the FAQ appears once with both fact matches and the best-rank vote.
- Base: a fact has no currently valid evidence; it appears only in KG analysis with zero expanded candidates.
- Bad: the synthetic `kc_kg_relation_*` row is stored as the retrieved item or counted as a Recall@K hit.
- Bad: expansion finds a document parent by source id when the evidence chunk id has no live child.
- Bad: code guesses an original row from `metadata.chunk_id` or a knowledge-id prefix.

### 6. Tests Required

- SQL tests assert exact FAQ/document evidence joins, usable reviewed facts/endpoints, live source status, ready original rows, document-child-only selection, and absence of metadata/prefix/parent fallback.
- Grouping tests assert original-id deduplication, multiple fact matches, stable rank order, and empty input behavior.
- Fusion tests assert one best-rank KG vote per original candidate and merge with vector/lexical channels by original id.
- Evaluation tests assert `retrieved_items` contains only `faq`/`document`, metrics use original ids, orphan facts remain diagnostics, and unknown fact associations raise.

### 7. Wrong vs Correct

#### Wrong

```python
fused = fuse_retrieval_candidates(
    vector_docs=vector_docs,
    keyword_docs=keyword_docs,
    kg_candidates=kg_fact_chunks,
    top_k=top_k,
)
```

This ranks synthetic facts as if they were original customer-support evidence.

#### Correct

```python
fact_hits = database.search_kg_knowledge_text(query, top_k=limit, query_terms=terms)
expanded = database.expand_kg_fact_hits(fact_hits)
fused = fuse_retrieval_candidates(
    vector_docs=vector_docs,
    keyword_docs=keyword_docs,
    kg_candidates=expanded,
    top_k=top_k,
)
```

KG contributes only through exact, live original evidence while fact ids remain diagnostic.

## Scenario: Usable-Only Isolated KG Subgraph

### 1. Scope / Trigger

- Trigger: code changes `get_kg_subgraph()`, `_kg_subgraph_sql()`, `/api/kg/subgraph`, subgraph query fields, or isolated/error rendering.
- Reason: review candidates must never leak into the visible usable graph, and an existing usable center with no eligible edges is a valid isolated result rather than a missing entity or server failure.

### 2. Signatures

- DB method: `get_kg_subgraph(*, center_entity_id: str, hops: int = 1, entity_types: list[str] | None = None, relation_types: list[str] | None = None, limit: int = 80) -> dict[str, Any] | None`
- Admin method: `AdminApp.kg_subgraph(params: dict[str, list[str]]) -> dict[str, Any]`
- API: `GET /api/kg/subgraph?center_entity_id=<id>&hops=<1|2>&entity_type=<csv>&relation_type=<csv>&limit=<n>`
- Response: `{"state":"isolated|connected","center":KgNode,"nodes":KgNode[],"edges":KgEdge[]}`

### 3. Contracts

- Status is fixed internally to `usable`. The API has no `status` query field and no caller-selectable review-state mode.
- The center CTE requires the exact entity id with `status = 'usable'`.
- Recursive traversal and returned edges require usable relations and usable head/tail entities at every hop.
- Entity/relation type filters narrow eligible edges but never remove an otherwise valid usable center row.
- The SQL left-joins limited edges to the center so a usable center with no eligible edge still returns one row.
- `get_kg_subgraph()` returns:
  - `None` when the center does not exist or is not usable;
  - `state='isolated'`, the center in `nodes`, and `edges=[]` when the center exists but no eligible edge remains;
  - `state='connected'` with deduplicated usable nodes and eligible edges otherwise.
- Admin maps `None` to `AdminNotFoundError`/HTTP 404. Database/query failures propagate through the normal error handler as HTTP 500; they are never converted to isolated.
- `hops` is constrained to 1-2 and `limit` to 1-200 by the admin boundary.

### 4. Validation & Error Matrix

- Missing/blank `center_entity_id` -> `AdminValidationError`/HTTP 400.
- Any unsupported query field, including `status` -> `AdminValidationError`/HTTP 400.
- Missing or non-usable center -> `AdminNotFoundError`/HTTP 404.
- Usable center with zero edges after filters -> HTTP 200 with `state='isolated'`.
- SQL/connection failure -> HTTP 500; do not return 404 or isolated.
- A usable relation with a non-usable endpoint -> excluded even if corrupt historical data exists.

### 5. Good/Base/Bad Cases

- Good: a usable center connected by usable relations to usable endpoints returns `connected`.
- Base: a usable center has no usable relation and returns an explicit isolated payload.
- Base: filters remove every edge but preserve the usable center as isolated.
- Bad: `status=needs_review` exposes unconfirmed nodes through the public subgraph API.
- Bad: no-edge and missing-center both return an empty object, so the UI cannot distinguish them.
- Bad: a database exception is swallowed and rendered as an isolated center.

### 6. Tests Required

- SQL tests assert the usable center predicate, usable relation/endpoints, recursive hop bound, type filters, edge limit, and center-to-edge `LEFT JOIN`.
- DB tests assert `None` for missing/non-usable center, exact isolated payload for a usable center, and connected payload containing only usable nodes/edges.
- Admin/ASGI tests assert required center validation, rejection of `status` and other unknown fields, 404 for `None`, and 500 propagation for database failures.
- Frontend tests assert isolated, connected, not-found, and query-failure states remain distinguishable and the hook never sends `status`.

### 7. Wrong vs Correct

#### Wrong

```sql
SELECT *
FROM kg_relations
WHERE status = %(status)s;
```

This exposes a caller-controlled review state and cannot preserve an isolated center row.

#### Correct

```sql
WITH center AS (
    SELECT * FROM kg_entities
    WHERE id = %(center_entity_id)s AND status = 'usable'
), usable_edges AS (
    SELECT relation.*
    FROM kg_relations relation
    JOIN kg_entities head ON head.id = relation.head_entity_id AND head.status = 'usable'
    JOIN kg_entities tail ON tail.id = relation.tail_entity_id AND tail.status = 'usable'
    WHERE relation.status = 'usable'
)
SELECT center.*, usable_edges.*
FROM center
LEFT JOIN usable_edges ON true;
```

The center remains visible when isolated, while every returned fact is fixed to the usable contract.
