# Cyclops ASGI Admin Contracts

> Project-specific contracts for the Cyclops FastAPI/Uvicorn admin surface and CLI entrypoint.

## Scenario: FastAPI Admin Adapter

### 1. Scope / Trigger

- Trigger: code modifies `cyclops.asgi_app`, `cyclops.cli` admin dispatch, static admin asset serving, upload/download routes, assistant SSE transport, or systemd/admin service wiring.
- Reason: Cyclops keeps the existing `AdminApp` business layer while replacing the previous ad hoc HTTP server with an ASGI adapter. Route drift, error-shape drift, or lifecycle leaks would break the React admin UI and increase production-facing RAG latency under concurrent use.

### 2. Signatures

- App factory: `create_app(*, settings: Settings | None = None, admin_app: Any | None = None, init_schema: bool = False, worker_factory: Callable[[Any], ImportParseWorker] | None = None) -> FastAPI`
- Runner: `run_admin_asgi(settings: Settings, *, host: str, port: int) -> None`
- CLI command: `python -m cyclops admin --host <host> --port <port>`
- Module CLI command: `python -m cyclops.cli admin --host <host> --port <port>`
- ASGI state: `request.app.state.admin_app -> AdminApp`
- SSE formatter: `_sse_response(events: Iterable[dict[str, Any]]) -> StreamingResponse`
- JSON reader: `_read_json(request: Request) -> dict[str, Any]`
- KG confirm routes:
  - `POST /api/kg/entities/{entity_id}/confirm` body exactly `{"expected_revision":<positive integer>}`
  - `POST /api/kg/relations/{relation_id}/confirm` body exactly `{"expected_revision":<positive integer>}`
- Import parse routes:
  - `POST /api/import/files/{file_id}/parse-jobs` body exactly `{"chunker_type":"naive|manual|qa|table"}`
  - `GET /api/import/parse-jobs/{job_id}`
  - `GET /api/import/files/{file_id}`

### 3. Contracts

- The CLI parser program name is `cyclops`; the `admin` subcommand must dispatch to `run_admin_asgi(settings, host=args.host, port=args.port)`.
- `run_admin_asgi()` must preserve loopback protection by calling `ensure_loopback_or_explicit_opt_in(host, os.environ)` before starting Uvicorn.
- `create_app()` must be injectable for tests:
  - If `admin_app` is provided, use it as `app.state.admin_app`.
  - If `admin_app` is omitted, load settings or use the provided `settings`, construct `AdminApp(settings)`, and call `database().init_schema()` only when `init_schema=True`.
  - If `worker_factory` is provided, construct exactly one worker from the current admin app; otherwise construct `ImportParseWorker` from the current settings.
- Lifespan startup must create exactly one import parse worker task. Shutdown must call `worker.stop()`, await the task, and only then close initialized database pool resources through `admin_app.db.close()` when available.
- The root path `/` must serve the built React entry from `cyclops/static/dist/index.html`.
- Static assets must remain mounted under `/static/dist/*` because built Vite HTML references that URL prefix.
- API route paths and methods must stay stable for the current React admin UI, including:
  - settings: `/api/settings`
  - import upload/download/assets/chunks/candidates/jobs
  - FAQ list/save/embed/status APIs
  - assistant: `/api/assistant/chat-stream`, `/api/assistant/probe`, `/api/assistant/models`
  - analytics, retrieval eval, retrieval aliases, and KG review APIs
- Upload route `POST /api/import/files` must accept `multipart/form-data` field `file`, preserve `parse=false`, and enforce `admin_max_request_bytes` before reading the upload body.
- Import parse POST only queues persistent state. Parse-job and file GET routes only read database state; `/api/import/files/{file_id}/parse-status` and `/api/import/files/{file_id}/reparse` do not exist.
- JSON routes accept only explicit, non-empty JSON objects. An empty HTTP body is invalid and never aliases `{}`; callers that mean an empty object must send `{}`.
- KG confirm routes pass the exact JSON object to `AdminApp.confirm_kg_entity()` / `confirm_kg_relation()`. They do not provide a bodyless overload, default revision, id-only adapter, or request-shape alias.
- Assistant stream response must preserve SSE format:
  - response media type starts with `text/event-stream`
  - each event is emitted by `format_sse_event()`
  - business exceptions inside the event iterator become an SSE `error` event instead of a broken HTTP response
- Assistant request headers `X-Requester-Type` and `X-Requester-Id` may fill missing `requester_type` and `requester_id` payload fields.
- Download responses must keep RFC 5987 `filename*` encoding so Chinese filenames survive browser downloads.

### 4. Validation & Error Matrix

- Non-loopback admin host without explicit opt-in -> fail before Uvicorn starts.
- Invalid UTF-8 or malformed JSON body -> `400 {"error": "request body must be valid JSON"}`.
- Empty JSON request body -> `400 {"error": "request body must be a JSON object"}`.
- JSON body that is not an object -> `400 {"error": "request body must be a JSON object"}`.
- JSON body larger than `admin_max_json_bytes` -> mapped by `classify_error_response()` as payload-too-large.
- Upload request larger than `admin_max_request_bytes` -> mapped by `classify_error_response()` as payload-too-large.
- Missing file or invalid multipart field -> FastAPI validation response; keep the route contract as `file`.
- `AdminValidationError` -> business validation HTTP error from `classify_error_response()`.
- `AdminConflictError` -> HTTP 409 with the sole error shape `{"error": "..."}`; stale KG review confirmation writes nothing and requires client refresh.
- `AdminNotFoundError` -> not-found HTTP error from `classify_error_response()`.
- `AiSuggestionError` or `ImportCandidateError` -> business error shape from `classify_error_response()`.
- Unhandled exception outside SSE -> generic classified error response without leaking internals.
- Exception raised while streaming SSE -> emit `event: error` with the classified message and stop the stream.

### 5. Good/Base/Bad Cases

- Good: `python -m cyclops admin --host 127.0.0.1 --port 8765` prints `Cyclops admin: http://127.0.0.1:8765` and starts Uvicorn.
- Good: `/` returns the Vite HTML containing Cyclops branding and `/static/dist/assets/...` references.
- Good: `/api/import/files?parse=false` uploads `file`, passes `auto_parse=False`, and does not read oversized uploads.
- Good: a parse POST returns queued immediately; the lifespan worker continues after the browser disconnects, and GET later observes polling/completed/failed.
- Good: `/api/assistant/chat-stream` returns `event: meta`, `event: step`, `event: delta`, `event: done`, or `event: error` in the existing event/data format.
- Good: leaving a `TestClient(create_app(admin_app=fake))` context calls `fake.db.close()` once when such a close method exists.
- Base: `create_app(admin_app=fake)` can be used without real settings, database, env vars, or schema initialization.
- Base: retrieval evaluation baseline sends a literal `{}` body; omitting the body is a validation error.
- Bad: duplicating RAG or import business logic in `asgi_app.py`; the adapter should call `AdminApp`.
- Bad: returning raw FastAPI exceptions for known business errors; the frontend expects the established `{"error": ...}` shape.
- Bad: a KG confirm route discards the body or supplies an implicit revision, allowing a stale drawer to confirm a replaced candidate.
- Bad: mounting static assets under a new URL prefix while old built HTML still references `/static/dist`.
- Bad: making `init_schema=True` the default for tests; this would unexpectedly touch the real database.
- Bad: using request `BackgroundTasks`, GET polling, or a synchronous reparse route to execute import parsing.

### 6. Tests Required

- CLI test that `build_parser().prog == "cyclops"` and `admin` dispatches to `run_admin_asgi()` with host/port unchanged.
- Route-surface test that all expected old admin API method/path pairs exist on the FastAPI app.
- Route-surface test that current parse job routes exist and old parse-status/reparse routes are absent.
- Static root test asserting `/` returns Cyclops-branded HTML and `/static/dist/assets/` references.
- Upload test asserting multipart `file` is accepted and `parse=false` becomes `auto_parse=False`.
- Download/assets test asserting content is served and Chinese `filename*` encoding is preserved.
- Assistant SSE test asserting `text/event-stream` and existing event/data formatting.
- Invalid JSON test asserting HTTP 400 and the old error body shape.
- Empty-body test asserting rejection before `AdminApp` is called, while a literal `{}` reaches the baseline evaluation method.
- KG confirm tests asserting exact positive `expected_revision` reaches `AdminApp`, `{}`/empty body/extra fields fail, and `AdminConflictError` maps to HTTP 409 with `{"error": string}`.
- Lifespan test asserting the worker starts once, stops and finishes before the database pool closes once.
- Service wiring test asserting `systemd/cyclops.service.template` and `scripts/install_user_service.sh` use Cyclops service names and placeholders.

### 7. Wrong vs Correct

#### Wrong

```python
@app.post("/api/faqs")
async def save_faq(request: Request) -> dict[str, Any]:
    payload = await request.json()
    database.insert_faq(payload)
    return {"ok": True}
```

This moves business rules into the ASGI adapter and bypasses existing validation, status, and embedding lifecycle logic.

#### Correct

```python
@app.post("/api/faqs")
async def save_faq(request: Request) -> Any:
    payload = await _read_json(request)
    return _admin(request).save_faq(payload)
```

The ASGI layer adapts HTTP concerns only; `AdminApp` remains the source of business behavior.
