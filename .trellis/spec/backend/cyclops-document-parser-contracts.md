# Cyclops Document Parser Contracts

> Project-specific contracts for MinerU payload normalization and document evidence retention.

## Scenario: MinerU Page Chrome And HTML Cleanup

### 1. Scope / Trigger

- Trigger: code modifies `extract_blocks_from_mineru_payload()`, `_blocks_from_content_list()`, `_extract_item_text()`, table extraction helpers, or import chunk source evidence derived from `ParsedBlock`.
- Reason: MinerU output may include page headers, footers, page numbers, sidebars, and HTML markup. These must not pollute searchable text, but real content evidence must remain available for document management and source inspection.

### 2. Signatures

- Python function: `extract_blocks_from_mineru_payload(payload: dict[str, Any], *, source_file: str, use_kb_packager: bool = False) -> list[ParsedBlock]`
- Python dataclass: `ParsedBlock(text, block_type, page_number, section_title, evidence, position_tag)`
- Import chunk fields derived later:
  - `source_text`
  - `page_start`
  - `page_end`
  - `source_offsets.pdf_positions`
  - `source_blocks[].evidence`

### 3. Contracts

- Raw MinerU `content_list` must skip page chrome block types: `header`, `footer`, `page_number`, `page_header`, `page_footer`, `page_aside_text`, `discarded`.
- Raw MinerU `content_list` must skip unsupported unknown block types instead of letting them become searchable text.
- Real content blocks must preserve `page_number`, `position_tag`, `bbox`-derived `pdf_positions`, `section_title`, `layout_type`, `layoutno`, and `doc_type_kwd` when available.
- HTML entities must be unescaped in searchable text.
- HTML tags must be stripped from searchable text; `<br>` and block/table endings should become line breaks.
- Literal non-HTML angle-bracket text such as `<退款规则>` must remain text, not be stripped as a tag.
- HTML table bodies should become readable row text such as `状态 | 处理`, while the original raw HTML remains in `evidence["table_html"]`.

### 4. Validation & Error Matrix

- Empty or fully filtered payload -> raise `MineruParseError("MinerU returned no parseable text")`.
- Page chrome block with text -> skip silently; this is expected parser noise, not a user-facing error.
- Real content block with invalid bbox -> keep the block and omit `position_tag`.
- Table block with raw HTML but no parseable rows -> fall back to sanitized text, keeping raw table HTML in evidence.

### 5. Good/Base/Bad Cases

- Good: `header`, `text`, `page_number`, `footer` on page 77 returns only the text block, with page 77 and bbox evidence preserved.
- Base: plain table text `状态 | 处理` stays plain text and keeps asset evidence.
- Bad: page footer text enters `source_text` or `embedding_text`, causing duplicate chunks across pages.
- Bad: raw `<td>` tags enter `source_text`.
- Bad: filtering `page_number` removes the real content block's `page_start/page_end`.

### 6. Tests Required

- Unit tests in `tests/test_document_parser.py` must assert:
  - page chrome and unknown raw block types are excluded from returned blocks;
  - content block page number and `source_offsets.pdf_positions` survive filtering;
  - HTML text is sanitized;
  - HTML table text is converted to row text;
  - raw table HTML is still present in block evidence.

### 7. Wrong vs Correct

#### Wrong

```python
if block_type == "page_number":
    return ""
return str(item.get("text") or "").strip()
```

This only skips one noise type and lets HTML/table markup and unknown page chrome enter searchable text.

#### Correct

```python
if _is_ignored_mineru_block_type(block_type):
    return ""
text = _sanitize_mineru_inline_text(raw_text)
```

Filtering is centralized and text cleanup preserves evidence separately from searchable text.

## Scenario: MinerU RAGFlow-Faithful Document Parsing Layer

### 1. Scope / Trigger

- Trigger: code modifies MinerU API integration, payload normalization, post-processing, `build_import_chunks_from_blocks()`, or any `qa`, `table`, `manual/title`, or `naive` routing for parsed MinerU blocks.
- Reason: this project stays deployment-light and MinerU API-first, but document parsing quality must remain accurate and efficient. "Lightweight" means deployment/dependency shape, not simplified parsing or chunker rules.

### 2. Signatures

- Python function under discussion: `build_import_chunks_from_blocks(file_id, blocks: list[ParsedBlock], *, chunk_token_num=None, delimiter="\n。；！？", ...) -> list[dict[str, Any]]`
- MinerU/RAGFlow reference files that must be checked before implementation:
  - `rag/app/qa.py`
  - `rag/app/table.py`
  - `rag/app/manual.py`
  - `rag/app/naive.py`
  - `rag/nlp/__init__.py`
- Output should remain existing `import_chunks` rows unless a separate schema decision is explicitly confirmed.

### 3. Contracts

- Do not implement a simplified "lightweight routing" or parser shortcut as a substitute for MinerU/RAGFlow behavior.
- "Lightweight" means local deployment/dependency shape is light, e.g. MinerU can be consumed via API; it does not reduce parsing, post-processing, or chunking correctness requirements.
- Before implementation, write a design that maps RAGFlow behavior to this project:
  - MinerU API/local-provider boundary;
  - payload discovery and normalization;
  - QA pair extraction and malformed-row handling;
  - table row/field handling and metadata retention;
  - manual/title hierarchy handling;
  - naive fallback and delimiter/token budget behavior;
  - parent-child indexing implications;
  - source evidence retention.
- Every route must preserve existing evidence fields such as `page_number`, `section_title`, `position_tag`, `pdf_positions`, `table_html`, and asset paths.
- Raw MinerU dictionaries end at `extract_blocks_from_mineru_payload()` / `MineruClient.parse_file()`. The chunker boundary accepts only canonical `ParsedBlock`; it has no test-only dictionary adapter or `_ensure_block` compatibility helper.
- Chunking changes must never bypass import review or directly write searchable knowledge.

### 4. Validation & Error Matrix

- Unclear file/chunker selection strategy -> stop and confirm design; do not guess in code.
- RAGFlow behavior differs from current project model -> document "copy / adapt / not applicable" before implementation.
- RAGFlow requires heavy services or storage engines -> adapt the behavior, not the dependency.
- MinerU API output lacks fields RAGFlow expects -> define evidence-preserving fallback and tests.
- A dictionary or anonymous object reaches `build_import_chunks_from_blocks()` -> `TypeError("blocks must contain ParsedBlock")`; fix the parser/fake at the upstream boundary instead of coercing it.

### 5. Good/Base/Bad Cases

- Good: implementation test cases are derived from RAGFlow `qa/table/manual/naive` behavior.
- Good: MinerU remains API-first while parsing and chunking rules remain faithful to MinerU/RAGFlow where applicable.
- Base: ordinary paragraphs still use RAGFlow-style `naive`.
- Bad: implementing a new provider registry or local RAGFlow task executor for this lightweight project.
- Bad: replacing MinerU/RAGFlow parser or chunker behavior with a few ad hoc heuristics.
- Bad: dropping `table_html` or page evidence while transforming chunks.
- Bad: a test returns `{"text": ...}` from `MineruClient.parse_file()` and production chunking silently treats it as a parsed block.

### 6. Tests Required

- Unit tests in `tests/test_document_parser.py` must assert:
  - QA cases match the chosen RAGFlow-derived behavior, including malformed rows;
  - table cases match RAGFlow-derived row/field behavior;
  - manual/title cases match RAGFlow-derived hierarchy behavior;
  - existing naive behavior and evidence preservation still pass.
- Tests must include both "desired RAGFlow behavior" and "project evidence retention" assertions.
- Tests must assert dictionary aliases are rejected and admin/provider fakes return real `ParsedBlock` instances.

### 7. Wrong vs Correct

#### Wrong

```python
if table_like:
    return simple_row_chunks(blocks)
```

This invents a shortcut without proving it matches RAGFlow behavior.

#### Correct

```python
reference = "rag/app/table.py + rag/nlp/tokenize_table"
# Implement only after mapping RAGFlow behavior to import_chunks and tests.
```

The implementation remains project-owned, but the behavior is explicitly derived from RAGFlow.

## Scenario: Document Chunker Type Configuration

### 1. Scope / Trigger

- Trigger: code modifies document import settings, `Settings.from_env()`, `AdminApp._build_document_import_chunks()`, or `build_import_chunks_from_blocks(..., chunker_type=...)`.
- Reason: the project now supports multiple RAGFlow-derived chunker behaviors without introducing RAGFlow runtime services. The selected route must be explicit and testable, not an untracked heuristic.

### 2. Signatures

- Environment key: `DOCUMENT_CHUNKER_TYPE`
- Settings field: `Settings.document_chunker_type: str`
- Admin payload/snapshot field: `document_chunker_type`
- Python function:
  - `build_import_chunks_from_blocks(file_id, blocks, *, chunker_type="naive", ...) -> list[dict[str, Any]]`
- Supported values:
  - `naive`
  - `manual`
  - `qa`
  - `table`

### 3. Contracts

- Default chunker type is `naive`.
- `DOCUMENT_CHUNKER_TYPE` must be normalized to lowercase.
- Unknown chunker types must raise `SettingsError` during settings load or `MineruParseError` at parser entry.
- Admin settings must preserve `document_chunker_type` when the settings payload omits it.
- `AdminApp._build_document_import_chunks()` must pass `document_chunker_type` into `build_import_chunks_from_blocks()`.
- Non-naive chunkers must record their route in `import_chunks.source_offsets["chunker"]["type"]`.
- Chunker outputs must still be import review rows; no route may directly write official FAQ or searchable knowledge.

### 4. Validation & Error Matrix

- Missing env/admin field -> use current setting or default `naive`.
- `DOCUMENT_CHUNKER_TYPE=lightweight` -> raise `SettingsError("DOCUMENT_CHUNKER_TYPE must be one of...")`.
- `build_import_chunks_from_blocks(..., chunker_type="lightweight")` -> raise `MineruParseError("Unsupported chunker_type...")`.
- `table` chunker with no data rows -> raise `MineruParseError("table chunker found no table rows")`.
- `qa` chunker with no Q/A pairs -> raise `MineruParseError("qa chunker found no question-answer pairs")`.
- `manual` chunker with no section text -> raise `MineruParseError("manual chunker found no section chunks")`.

### 5. Good/Base/Bad Cases

- Good: `DOCUMENT_CHUNKER_TYPE=table` produces one import chunk per table row and keeps sheet/header/row evidence.
- Good: `DOCUMENT_CHUNKER_TYPE=qa` appends malformed txt/csv rows to the current answer after a valid question exists.
- Good: `DOCUMENT_CHUNKER_TYPE=manual` groups consecutive parsed blocks by section path and keeps page evidence.
- Base: omitted `DOCUMENT_CHUNKER_TYPE` keeps existing naive behavior.
- Bad: adding an `auto` or `lightweight` route that guesses behavior without RAGFlow mapping and tests.
- Bad: storing chunker type only in transient code variables, leaving import chunks unauditable.

### 6. Tests Required

- `tests/test_config.py`:
  - default settings expose `document_chunker_type == "naive"`;
  - configured settings parse `DOCUMENT_CHUNKER_TYPE`;
  - unknown types raise `SettingsError`.
- `tests/test_admin_server.py`:
  - settings snapshot and tenant persistence include `document_chunker_type`;
  - omitted settings payload preserves existing chunker type;
  - `_build_document_import_chunks()` passes `chunker_type` into the parser.
- `tests/test_document_parser.py`:
  - `table` creates one chunk per row with row/header evidence;
  - `qa` appends malformed rows to the current answer;
  - `manual` groups by section path and records chunker metadata.

### 7. Wrong vs Correct

#### Wrong

```python
chunker_type = guess_from_text(blocks)
chunks = build_import_chunks_from_blocks(file_id, blocks)
```

This hides the route, cannot be audited in import chunks, and can drift away from RAGFlow behavior.

#### Correct

```python
chunks = build_import_chunks_from_blocks(
    file_id,
    blocks,
    chunker_type=settings.document_chunker_type,
)
```

The route is explicit, validated, and persisted in chunk metadata for non-naive chunkers.

## Scenario: Document File-Level Chunker Selection

### 1. Scope / Trigger

- Trigger: code modifies `import_files.chunker_type`, document parse job payloads, document management UI chunker selection, or `AdminApp.process_import_parse_job()`.
- Reason: global `DOCUMENT_CHUNKER_TYPE` is only a default. Mixed imports need each file to preserve the selected RAGFlow-derived post-parser route so parsing is auditable and repeatable.

### 2. Signatures

- Database field: `import_files.chunker_type TEXT NOT NULL DEFAULT 'naive'`
- Python method:
  - `AdminApp.create_import_file(filename, content, *, auto_parse=True, chunker_type=None)`
  - `AdminApp.start_import_parse_job(file_id, payload)`
  - `AdminApp.process_import_parse_job(job)`
  - `AdminApp._build_document_import_chunks(file_id, blocks, *, chunker_type: str)`
- HTTP payload:
  - `POST /api/import/files/<id>/parse-jobs`
  - body exactly `{"chunker_type":"naive|manual|qa|table"}`
- Frontend type:
  - `ImportFile.chunker_type: DocumentChunkerType`
  - `DocumentChunkerType = 'naive' | 'manual' | 'qa' | 'table'`

### 3. Contracts

- New import file rows must persist a `chunker_type`; omitted values use `settings.document_chunker_type`, then `naive`.
- Parse job payload may override the file's `chunker_type`; the backend must validate and persist it before MinerU job progress is saved.
- Worker finalization must pass the claimed job's canonical `chunker_type` into `build_import_chunks_from_blocks()`.
- The global `DOCUMENT_CHUNKER_TYPE` is only a new-file/default-setting input. An existing file record is the final source of truth and must contain one exact canonical value.
- The queued job stores the canonical generation chunker. `_build_document_import_chunks()` requires that claimed job value as a keyword argument; the builder never reads global settings or supplies `naive` when its caller omits/passes null.
- Schema migration may assign the declared `naive` column default when the column is first added. After migration, every reader treats the non-null column as required; this is a data migration, not a permanent dual-read path.
- Document management UI must display the current file chunker and submit the selected value when starting parse.
- The UI rejects missing, differently-cased, or unknown response values instead of displaying them as `naive`.
- Markdown chat imports do not use document chunkers; their message chunking remains `parse_mode` / `chunk_days` based.

### 4. Validation & Error Matrix

- Missing `chunker_type` in parse-job payload -> `AdminValidationError`; every parse generation records an explicit canonical route.
- Explicit parse-job `chunker_type` of null, blank, padded, or differently cased text -> `AdminValidationError`; omission is also invalid.
- Builder omits `chunker_type` -> Python `TypeError`; builder receives explicit null or a non-canonical value -> `AdminValidationError`.
- `chunker_type` in `{naive, manual, qa, table}` -> persist on `import_files` and use for MinerU chunk building.
- Unknown values such as `auto`, `lightweight`, or `ragflow` -> raise `AdminValidationError("chunker_type must be one of...")`.
- Existing DB without the column -> `sql/001_init.sql` must add `chunker_type TEXT NOT NULL DEFAULT 'naive'`.
- File status polling completion -> must not silently switch back to global settings.

### 5. Good/Base/Bad Cases

- Good: PDF manual row has `chunker_type='manual'`; MinerU completion builds manual chunks even when global setting is `naive`.
- Good: FAQ-like source row has `chunker_type='qa'`; parse job payload persists `qa` before background polling.
- Base: adding the non-null column to a pre-column database assigns the one-time schema default `naive`; subsequent code reads that stored value directly.
- Bad: UI only sends parser name and backend always reads `settings.document_chunker_type`.
- Bad: backend or UI converts missing, `NAIVE`, or an unknown value to `naive` and hides a broken row/response contract.
- Bad: chunker choice is stored only in `import_chunks.source_offsets` after parsing, leaving the file list/audit trail unable to show which route will be used on reparse.

### 6. Tests Required

- `tests/test_db.py` must assert `sql/001_init.sql` adds `import_files.chunker_type`.
- `tests/test_admin_server.py` must assert:
  - file creation writes the default chunker;
  - parse job payload persists `chunker_type`;
  - unknown chunker payloads are rejected;
  - MinerU finish uses the file-level chunker instead of global settings;
  - missing/null/blank payload chunker values fail instead of reading a file/global default.
  - the private chunk builder rejects an omitted or explicit-null chunker instead of selecting global settings.
- Frontend tests must assert the four exact values are accepted and missing, differently-cased, and unknown values throw; TypeScript, lint, and build must pass.

### 7. Wrong vs Correct

#### Wrong

```python
chunk_rows = self._build_document_import_chunks(record["id"], blocks)
```

This lets the builder choose a setting that may differ from the persisted file route.

#### Correct

```python
chunk_rows = self._build_document_import_chunks(
    record["id"],
    blocks,
    chunker_type=finalizing_job["chunker_type"],
)
```

The route is persisted on the import file and then explicitly passed into the RAGFlow-derived post-processing layer.

## Scenario: Persistent Import Parse Worker Lifecycle

### 1. Scope / Trigger

- Trigger: code modifies `import_parse_jobs`, `ImportParseWorker`, MinerU job execution, import parse HTTP routes, output-affecting parser settings, or ASGI lifespan behavior.
- Reason: parsing must continue without browser polling, survive process restarts, prevent duplicate provider submissions during long I/O, and publish replacement chunks only as one atomic snapshot.

### 2. Signatures

- DB methods:
  - `create_import_parse_job(file_id, *, chunker_type, input_fingerprint) -> dict[str, Any]`
  - `claim_import_parse_job(*, lease_seconds) -> dict[str, Any] | None`
  - `renew_import_parse_job_lease(job_id, *, lease_token, lease_seconds) -> bool`
  - `update_import_parse_job_progress(...) -> dict[str, Any]`
  - `begin_import_parse_job_finalization(...) -> dict[str, Any]`
  - `complete_import_parse_job(...) -> dict[str, Any]`
  - `fail_import_parse_job(...) -> dict[str, Any]`
- Worker: `ImportParseWorker(admin_app, *, poll_interval_seconds, lease_seconds)`
- HTTP:
  - `POST /api/import/files/{file_id}/parse-jobs`
  - `GET /api/import/parse-jobs/{job_id}`
  - `GET /api/import/files/{file_id}`
  - `GET /api/import/files`
- Environment:
  - `IMPORT_PARSE_WORKER_POLL_INTERVAL_SECONDS`
  - `IMPORT_PARSE_WORKER_LEASE_SECONDS`

### 3. Contracts

- `import_parse_jobs` is the only parse-runtime truth. `import_files` keeps business status and summary fields only; it has no provider batch, provider filename, or parse-progress columns.
- Lifecycle is exactly `queued -> submitting -> polling -> finalizing -> completed|failed`. Claim changes only a queued job to submitting; reclaim preserves submitting, polling, or finalizing.
- POST creates and returns one queued job. GET routes are strictly database reads and never call MinerU, download results, or advance lifecycle state.
- The ASGI lifespan owns one worker task. Shutdown calls `stop()`, awaits that task, and only then closes the database pool.
- Provider calls run outside database transactions. While a claimed call is executing, a heartbeat renews the same unexpired lease token; a transient claim failure is logged and retried after the poll interval instead of terminating the worker task.
- All lease-fenced writers require the current token and an unexpired lease. Losing the token prevents the stale worker from updating progress, failing, or completing the job.
- Multi-row terminal writes use file-before-chunks-before-job lock order. They first read and validate the job without a row lock, lock the immutable source hierarchy, then lock and revalidate the job before writing.
- File deletion locks the file/chunks and then every parse job for that file in ID order. Holding the file lock prevents a new parse job from appearing before the foreign-key cascade reaches job rows.
- Completion fast-checks status/token/fingerprint, locks the file and old chunks, locks and rechecks the job, invalidates KG evidence and old knowledge, replaces all chunks, updates the file summary, and marks the job completed in one transaction.
- The input fingerprint includes file bytes, parser, selected chunker, and every MinerU output-affecting setting: KB packager, token budget, delimiters, overlap, and table/image context sizes.
- Parse worker timing fields survive settings round trips. Runtime changes take effect for a new worker process; a settings save cannot silently reset them to defaults.

### 4. Validation & Error Matrix

- Active job already exists for the file -> conflict; do not reuse or replace that job.
- Missing/non-canonical `chunker_type` -> validation error before job creation.
- Missing file, unsupported parser, or changed input fingerprint -> terminal failed job; old chunks remain unchanged.
- Current lease token differs, is expired, or job is terminal -> fenced writer fails without source changes.
- MinerU reports failed/error/cancelled -> terminal failed job with bounded error text.
- Claim database call raises transiently -> worker logs, waits one poll interval, and retries.
- Heartbeat renewal returns false -> stop renewing; the stale process remains fenced from any later write.

### 5. Good/Base/Bad Cases

- Good: the browser closes after POST; the worker submits, polls, finalizes, and the next GET observes completed state.
- Good: a process dies in finalizing; after lease expiry a new process reclaims the same row and atomically completes it.
- Good: a provider call exceeds the original lease window, heartbeat renewal keeps a second worker from reclaiming it.
- Good: deleting a file with an active/finalizing job fences the worker and completes under the same source-before-job order as terminal writes.
- Base: no due job exists; the worker waits on a stoppable event instead of busy-looping.
- Bad: `GET /parse-status` calls MinerU and controls task progress.
- Bad: a 60-second lease surrounds a 600-second provider call without renewal.
- Bad: completion locks job then file while deletion reaches job through a file foreign-key cascade.
- Bad: current global chunk settings are omitted from the generation fingerprint.

### 6. Tests Required

- Worker unit tests assert no-HTTP progress, stoppable idle wait, per-job exception isolation, transient claim recovery, and lease heartbeat renewal during a blocked provider call.
- DB unit tests assert `FOR UPDATE SKIP LOCKED`, expired finalizing reclaim, lease-token fencing, heartbeat SQL, file/chunks-before-job terminal/delete lock order, and atomic cleanup/insert/update order.
- Admin tests assert each provider phase, Markdown direct completion, provider-free GET, bounded failure, complete fingerprint coverage, and settings timing round-trip.
- ASGI tests assert the current route surface, absence of `/parse-status` and `/reparse`, worker start/stop, and worker-before-pool shutdown order.
- Real PostgreSQL tests assert heartbeat prevents reclaim, expired submitting/finalizing leases are reclaimed, one job reaches completed, old chunks/knowledge/evidence disappear, new chunks appear together, and KG owners/projections are disabled when their last evidence is removed.

### 7. Wrong vs Correct

#### Wrong

```python
status = mineru.get_task_status(batch_id, file_name)  # may block longer than lease
return database.complete_import_parse_job(job_id, chunks=chunks)
```

This permits another worker to reclaim and repeat provider I/O before the first process reaches its fenced write.

#### Correct

```python
while provider_call_is_running:
    database.renew_import_parse_job_lease(
        job_id,
        lease_token=lease_token,
        lease_seconds=lease_seconds,
    )
```

The heartbeat prevents duplicate live execution, while token checks still fence a process that truly loses ownership.
