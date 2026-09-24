# Customer Service Agent Evaluation Workbench

> Project-specific frontend contracts for retrieval evaluation workflows in the local admin UI.

## Scenario: Batch Regression Diagnostics MVP

### 1. Scope / Trigger

- Trigger: code modifies `web/src/pages/EvaluationPage.tsx`, `web/src/pages/evaluation/batch-diagnostics.ts`, `web/src/pages/evaluation/batch-panel.tsx`, or evaluation batch-run UI behavior.
- Reason: the evaluation workbench is the safety net for retrieval/chunking changes. Batch regression must remain lightweight while still separating real retrieval failures from unlabeled or not-yet-run cases.

### 2. Signatures

- Frontend pure function:
  - `buildEvaluationRunPayload(strategy: EvaluationStrategy) -> RetrievalEvalRunPayload`
  - `storeEvaluationRunOverride(current: EvaluationRunOverrides, run: RetrievalEvalRun) -> EvaluationRunOverrides`
  - `selectEvaluationRun(evalCase: RetrievalEvalCase, overrides: EvaluationRunOverrides, strategy: EvaluationStrategy) -> RetrievalEvalRun | null`
  - `buildEvaluationBatchSummary(cases: RetrievalEvalCase[], strategy: EvaluationStrategy, runOverrides: EvaluationRunOverrides) -> EvaluationBatchSummary`
  - `diagnoseEvaluationCase(evalCase: RetrievalEvalCase, strategy: EvaluationStrategy, runOverrides: EvaluationRunOverrides) -> EvaluationCaseDiagnostic`
- Frontend component:
  - `EvaluationBatchPanel({ summary, runState, batchCaseCount, onRunBatch, onSelectCase })`
- API contract:
  - `POST /api/retrieval/eval-cases/{case_id}/run`
  - `GET /api/retrieval/eval-cases` returns `latest_runs: RetrievalEvalRun[]`
- UI strategies:
  - `baseline -> retrieval_hybrid_v1`
  - `kg_debug -> retrieval_hybrid_v1_kg_debug`

### 3. Contracts

- Batch MVP does not create a persistent batch/baseline record.
- Batch run reuses the single-case run API sequentially for the current filtered active cases.
- The run payload has exactly two shapes:
  - baseline: `{}`;
  - KG debug: `{"use_kg": true}`.
- The frontend must never send `null`, `{"use_kg": false}`, a string boolean, or any extra field.
- `RetrievalEvalCase` has exactly one persisted run field: `latest_runs`. The singular `latest_run` field does not exist.
- `latest_runs` contains at most one current-contract run for each fixed strategy, ordered baseline then KG debug. A missing strategy remains missing; the UI never displays the other strategy as a fallback.
- `EvaluationRunOverrides` is keyed by case id and UI strategy. Each successful run updates only that case/strategy slot, so running KG debug cannot overwrite the baseline result and vice versa.
- The selected result panel, header metrics, batch summary, and diagnostics must all call the same strategy-aware selector.
- Batch summary reads current page state for the selected strategy only:
  - active cases count;
  - labeled cases count;
  - run count;
  - average `recall_at_k`;
  - average `mrr`;
  - average `hit_rate_at_1`;
  - hit/missed/low-rank/granularity/empty/not-run/missing-expected counts.
- Missing expected source/chunk ids must not be counted as retrieval failure.
- Cases without a run for the selected strategy must not be counted as retrieval failure.

### 4. Validation & Error Matrix

- Payload `{}` -> run baseline.
- Payload `{"use_kg": true}` -> run KG debug.
- Any other payload shape -> request error from the backend; do not rewrite or retry it as baseline.
- No expected source/chunk ids -> reason `missing_expected`.
- Expected ids exist, no run for the selected strategy -> reason `not_run`.
- Selected-strategy run has no candidates -> reason `empty_candidates`.
- Expected id is absent from TopK -> reason `missed`, unless a chunk-level case has a source-level match that indicates `granularity_mismatch`.
- Expected id rank is `1` -> reason `hit`.
- Expected id rank is `> 1` -> reason `low_rank`.
- A failed single-case run during batch increments the transient failed count and must not stop the remaining cases.

### 5. Good/Base/Bad Cases

- Good: user runs baseline and KG debug for the same case; both persisted/current overrides remain available and the strategy control switches candidates and metrics without rerunning.
- Good: user runs active cases, sees progress, and failing cards link back to the selected-strategy case detail.
- Good: unlabeled cases appear as "待标注" and guide the user to label them, not as retrieval failures.
- Base: page reload restores up to one persisted run per strategy; transient batch progress is reset.
- Base: only baseline exists; selecting KG debug shows `not_run` rather than baseline data.
- Bad: adding a backend batch table for the MVP before the workflow needs historical baselines.
- Bad: averaging missing metrics as zero, which would make unknown results look like failures.
- Bad: storing overrides as `Record<caseId, RetrievalEvalRun>` or selecting a global newest run, because one strategy silently replaces the other.
- Bad: sending `{"use_kg": false}` as a second baseline payload.

### 6. Tests Required

- Node test for `buildEvaluationBatchSummary()` must assert:
  - summary and diagnostics use only the requested strategy;
  - a run from the other strategy produces `not_run`, not a fallback result;
  - unlabeled cases are counted as missing expected;
  - missed cases are counted separately;
  - low-rank hits still count as hits and low-rank diagnostics;
  - average metrics ignore unknown values.
- Node tests for evaluation helpers must assert:
  - baseline payload is exactly `{}` and KG debug payload is exactly `{"use_kg": true}`;
  - overrides preserve independent baseline and KG debug slots for one case;
  - `selectEvaluationRun()` prefers the same-strategy override, then the same-strategy persisted run, then returns `null`;
  - no `latest_run` shape is accepted by current frontend types.
- Frontend `npm test`, `npm run lint`, and `npm run build` must pass.
- Browser verification should load `/#/evaluation` and confirm the batch panel renders.

### 7. Wrong vs Correct

#### Wrong

```typescript
const run = runOverrides[evalCase.id] || evalCase.latest_run
const summary = buildEvaluationBatchSummary(cases)
```

This uses the removed singular shape and lets the newest strategy overwrite or contaminate the other strategy's results.

#### Correct

```typescript
const run = selectEvaluationRun(evalCase, runOverrides, strategy)
const summary = buildEvaluationBatchSummary(cases, strategy, runOverrides)
const failures = summary.diagnostics.filter(
  (item) => item.reason === 'missed' || item.reason === 'empty_candidates',
)
```

The selected strategy is explicit everywhere, and baseline/KG debug results remain independently comparable.

## Scenario: Readable Candidate Provenance

### 1. Scope / Trigger

- Trigger: code modifies `web/src/pages/evaluation/result-panel.tsx`, `web/src/pages/evaluation/helpers.ts`, `web/src/pages/EvaluationPage.tsx`, or backend retrieval evaluation candidate payloads.
- Reason: evaluation candidates are used by non-developer users to label expected hits. The UI must show readable FAQ/document provenance first and keep raw ids as secondary troubleshooting data.

### 2. Signatures

- Backend payload function:
  - `retrieval_eval_item_payload(candidate) -> RetrievalEvalItem`
- Frontend helper functions:
  - `candidateSourceLabel(item: RetrievalEvalItem) -> string`
  - `candidateLocationLabel(item: RetrievalEvalItem) -> string`
  - `candidateExcerpt(item: RetrievalEvalItem) -> string`
  - `displayStrategyLabel(value?: string | null) -> string`
  - `retrievalChannelLabel(value: string) -> string`
- Frontend source drawers:
  - `FaqDrawer({ faqId, onClose, onCreated })`
  - `DocumentDrawer({ fileId, onClose })`
  - `useUi().setOpenImportFileId(fileId, sourceChunkId?)`

### 3. Contracts

- `RetrievalEvalItem` must preserve:
  - `id`: knowledge chunk id, used for chunk-level expected hit matching.
  - `source_id`: FAQ id for FAQ candidates; import file id for document candidates.
  - `source_chunk_id`: import chunk id for document candidates when available; used to position `DocumentDrawer`.
  - `source_type`: `faq` or `document`.
  - `source_title`, `section_path`, `page_start`, `page_end`, `block_type`, and `content`: the only readable provenance fields.
  - `channels`, `fused_score`, `vector_score`, `keyword_score`, `kg_score`, and `kg_matches`: current ranking diagnostics.
  - It does not carry `metadata`, `question`, `answer`, `category`, or `tags`; the UI must not recreate removed DTO aliases or fallback reads.
- Evaluation UI must open source drawers rather than create a separate preview:
  - FAQ candidate -> `setOpenFaqId(item.source_id)`.
  - Document candidate -> `setOpenImportFileId(item.source_id, item.source_chunk_id ?? null)`.
- Raw ids remain visible only as secondary rows with explicit copy icon buttons.
- Copy buttons must have `title`/`aria-label` and `cursor-pointer`.

### 4. Validation & Error Matrix

- Null `source_title` -> display the required canonical `source_id` as the secondary identifier; do not read another field shape.
- Empty `content` -> display an explicit empty excerpt state; do not read `answer` or metadata.
- Missing document `source_chunk_id` -> open the document drawer without forced chunk positioning.
- Missing `navigator.clipboard.writeText` or copy failure -> show a toast error; do not silently fail.
- Unknown strategy/channel/source type -> display the raw value for troubleshooting.

### 5. Good/Base/Bad Cases

- Good: FAQ candidate shows canonical `source_title`, `content`, `查看 FAQ`, and copy icons for source/chunk ids.
- Good: Document candidate shows file name, page/section/chunk position, excerpt, `查看切片`, and opens the existing document drawer.
- Base: If optional provenance is absent, the required source id remains visible and copyable.
- Bad: Showing only `source_id/chunk_id` as the primary candidate text.
- Bad: Creating a one-off evaluation preview drawer instead of reusing FAQ/document drawers.

### 6. Tests Required

- Node test for `helpers.ts` must assert:
  - FAQ helpers use only `source_title` and `content`.
  - document helpers include page, section, source chunk id, and excerpt.
  - internal strategy/source labels are translated where known.
- Python test for `retrieval_eval_item_payload()` must assert canonical readable fields are included and duplicate compatibility fields are absent.
- Frontend `npm test`, `npm run lint`, and `npm run build` must pass.
- Backend `python -m pytest`, `python -m ruff check .`, and `python -m cyclops check-config` must pass when the environment is available.

### 7. Wrong vs Correct

#### Wrong

```tsx
<div>source {item.source_id} · chunk {item.id}</div>
```

This makes users label expected hits by opaque ids and hides the actual provenance.

#### Correct

```tsx
<div>{candidateSourceLabel(item)}</div>
<div>{candidateLocationLabel(item)}</div>
<button title="打开对应 FAQ 抽屉">查看 FAQ</button>
```

Readable provenance is primary; ids are secondary copy actions.

## Scenario: Shared Admin Drawer And Icon Button Conventions

### 1. Scope / Trigger

- Trigger: code modifies admin drawers, drawer widths, icon-only buttons, popover/select controls, or confirmation dialogs.
- Reason: the local admin UI should feel like one tool. New pages must reuse existing controls instead of inventing separate button/drawer/dialog structures.

### 2. Signatures

- Drawer width constants:
  - `DRAWER_WIDTH_COMPACT = 520`
  - `DRAWER_WIDTH_MEDIUM = 560`
- Shared components:
  - `Button`, `Drawer`, `Dialog`, `Popover`, `Tooltip`, `Badge`, `toast`

### 3. Contracts

- Default drawer width is compact (`520px`) unless the content needs a documented medium width.
- FAQ/evaluation/simple editing drawers should use compact width.
- Document chunk browsing can use medium width when compact width would make the toolbar and chunk browser too cramped.
- Icon-only buttons must include `title` or tooltip-equivalent text and `cursor-pointer`.
- Hard-to-understand primary actions may use short text + icon.
- Dangerous actions must keep danger styling and explicit confirmation through the existing `Dialog` component.
- Native `select` should not be used for dark-theme popups when an existing Popover/Button menu can provide stable contrast.
- Common function button mapping:
  - Start/run actions -> `Play` icon + short verb text, e.g. `开始解析`, `运行单条`.
  - Close actions -> `X` icon for explicit close buttons; text cancel remains `取消` when it means aborting a form/dialog.
  - Save actions -> `Save` icon + `保存` / `保存修改` text.
  - Embedding actions -> `Waypoints` icon + `Embedding` text across FAQ, document, and chunk-level embedding; use loading spinner while pending.
  - Regenerate chunk embedding must not use refresh icons; it is still an Embedding action.
  - Download actions -> `Download` icon.
  - Disable/enable visibility at chunk level -> `EyeOff` / `Eye`; document-level power state -> `PowerOff` / `Power`.
  - Delete actions -> `Trash2` icon with danger styling and existing `Dialog` confirmation.

### 4. Validation & Error Matrix

- Icon button lacks title/tooltip -> fix before commit.
- Clickable icon lacks pointer cursor -> fix before commit.
- New drawer hardcodes a width already covered by shared constants -> use the constant.
- New destructive action uses `confirm()` -> replace with existing dialog.
- New dropdown has unreadable dark-theme popup -> replace with existing Popover/Button pattern.

### 5. Good/Base/Bad Cases

- Good: secondary toolbar actions are icon-only with title and stable dimensions.
- Good: platform-wide function buttons reuse the semantic icon mapping above, so the same action has the same visual language across drawers.
- Good: Embedding buttons are `Waypoints + Embedding`, including chunk-level regeneration.
- Good: Chunker selection uses Popover/Button so dark theme is readable.
- Base: Main workflow buttons such as "开始解析" may keep short text.
- Bad: A new page creates local one-off drawer widths, button shapes, or custom popover markup.
- Bad: Different drawers use unrelated button styles for the same action type.

### 6. Tests Required

- Frontend lint and build must pass.
- Browser/manual verification should inspect any modified drawer at desktop width and confirm buttons do not overlap.
- If a pure display helper is added, add a Node test.

### 7. Wrong vs Correct

#### Wrong

```tsx
<select className="...">
  <option value="naive">Naive</option>
</select>
```

Native select popups can render outside the app theme and become unreadable in dark mode.

#### Correct

```tsx
<Popover>
  <PopoverTrigger asChild>
    <Button title="选择解析后的切块策略">Chunker</Button>
  </PopoverTrigger>
  <PopoverContent>{/* themed options */}</PopoverContent>
</Popover>
```

Use existing themed primitives for consistent dark-mode behavior.
