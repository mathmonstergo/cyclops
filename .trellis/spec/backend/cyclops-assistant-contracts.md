# Cyclops Assistant Contracts

> Project-specific contracts for the internal platform assistant page.

## Scenario: Assistant Chat Stream

### 1. Scope / Trigger

- Trigger: code modifies `AdminApp.iter_assistant_chat_events()`, `HybridRetrievalService`, `assistant_document_payload()`, `build_user_prompt()`, or frontend consumers under `web/src/pages/assistant/`.
- Reason: the internal platform question page relies on one canonical retrieval result for ranking, parent context, SSE diagnostics, provenance, analytics, and the final answer. A copied search path or identity fallback can make those consumers disagree.

### 2. Signatures

- Backend stream method: `AdminApp.iter_assistant_chat_events(payload: dict[str, Any]) -> Iterator[dict[str, Any]]`
- Canonical retrieval call: `HybridRetrievalService.retrieve(query: str, *, include_parent_context: bool, use_kg: bool) -> HybridRetrievalResult`
- Canonical retrieval model: `RetrievedKnowledgeChunk`
- Retrieval result boundaries:
  - `HybridRetrievalResult.candidates: list[FusedCandidate]`
  - `HybridRetrievalResult.parent_documents: list[RetrievedKnowledgeChunk]`
- SSE formatter: `format_sse_event(event: dict[str, Any]) -> str`
- Source payload builder: `assistant_document_payload(doc: RetrievedKnowledgeChunk) -> dict[str, Any]`
- Prompt builder: `build_user_prompt(question: str, docs: Sequence[RetrievedKnowledgeChunk], conversation_context: ConversationContext | None = None) -> str`
- Frontend stream client: `streamAssistantChat({ payload, signal, onEvent })`
- Frontend source type: `AssistantSource`

### 3. Contracts

- Stream events must use `type` values consumed by the frontend:
  - `meta`
  - `step`
  - `delta`
  - `done`
  - `error`
- `format_sse_event()` must emit `event: <type>` and JSON `data` with the same `type`.
- `step` events for retrieval must keep debug fields serializable, especially `analysis` and `documents`.
- `done` events must include:
  - `flow_id`
  - `question`
  - `answer_draft`
  - `documents`
- Sensitive questions with `analysis.safety_action == "refuse"` must stop after intent detection and must not call embedding, vector search, keyword search, rerank, or answer-generation LLM.
- After the sensitive-query short circuit, the assistant performs retrieval only through `AdminApp.hybrid_retrieval_service().retrieve(retrieval_query, include_parent_context=True, use_kg=False)`. `use_kg=False` is explicit; it is never supplied by a default.
- `HybridRetrievalResult.candidates` contains the ranked direct evidence. Only those candidates contribute to the `hybrid_retrieval` step, `top_score`, hit counts, analytics rows, and ranked source ids.
- `HybridRetrievalResult.parent_documents` is unranked prompt context. Parent rows may be added to `source_context`, `done.documents`, and the LLM prompt with `retrieval_channels=["parent_context"]`, but never to direct-candidate metrics or analytics.
- The assistant must not call `search_knowledge()`, `search_knowledge_text()`, fusion, or rerank directly. Retrieval sequencing and candidate limits belong to `HybridRetrievalService`.
- Realtime status questions may retrieve SOP/help content, but the prompt sent to the model must explicitly state that the assistant cannot confirm backend realtime status and must not fabricate realtime status.
- `conversation_context` is optional request context for the current assistant turn only:
  - Shape: `{ summary?: string, recent_messages: [{ role: "user" | "assistant", content: string }] }`
  - It must not be persisted to the database and must not be shared across conversations.
  - Backend normalization must discard invalid roles, empty content, and non-object items.
  - Backend formatting must use the GenericAgent-inspired short-term context structure:
    - `### [WORKING MEMORY]`
    - optional `<earlier_context>...</earlier_context>` for compact old history
    - optional `<history>` with `[USER] ...` and `[Agent] ...` lines for recent turns
  - The prompt must state that working memory only helps resolve references and follow-up context; factual answers still come from the knowledge-base context.
- `assistant_document_payload()` must expose provenance fields at the top level for frontend rendering:
  - `source_type`
  - `source_id`
  - `source_chunk_id`
  - `parent_chunk_id`
  - `chunk_level`
  - `source_title`
  - `section_path`
  - `page_start`
  - `page_end`
  - `block_type`
  - `source_offsets`
  - `content`
  - `metadata`
  - `score`
- Internal assistant, prompt, fusion, and serializer code accepts only `RetrievedKnowledgeChunk`; `RetrievedDocument` and its attribute aliases do not exist.
- `metadata` is supplemental domain data, not a second identity source. Backend and frontend consumers must not recover `source_id`, `source_chunk_id`, parent linkage, page range, section path, or content from metadata when a canonical top-level field is absent.
- If the established external `AssistantSource` wire still needs readable `question`, `answer`, `category`, or `source_date` keys, `assistant_document_payload()` derives them explicitly from the canonical chunk in one place. They are wire fields, not properties or alternate internal models.

### 4. Validation & Error Matrix

- Missing `question` -> raise `AdminValidationError`.
- Unsupported `flow_id` -> raise `AdminValidationError`.
- A retrieval result containing a non-`RetrievedKnowledgeChunk` document -> `TypeError`; do not coerce dictionaries or anonymous objects.
- Caller omits `use_kg` -> Python `TypeError`; fix the caller instead of adding a default.
- Invalid `conversation_context` -> ignore invalid parts and continue the request; do not fail the chat stream for malformed optional history.
- Sensitive question -> return a refusal `delta` and `done` with `documents: []`.
- No retrieval hits -> continue answer generation with no documents and no fabricated knowledge-base evidence.
- Parent context exists without a ranked direct candidate -> it must not produce a `top_score`, hit count, or analytics hit.
- Answer-generation model/provider failure -> emit `answer_generation` step with `status: "failed"` and then `type: "error"` with a user-readable model-service message; do not let the exception bubble to the generic SSE `internal error` handler.
- Stream HTTP/SSE transport errors -> frontend must surface an error on the assistant message and stop streaming state.

### 5. Good/Base/Bad Cases

- Good: sensitive query emits `meta`, `input_question`, `intent_detection`, refusal `delta`, `answer_generation completed`, `done` with empty documents.
- Good: answer-generation model failure emits `answer_generation running`, `answer_generation failed`, then `error` and no `done`.
- Good: follow-up questions include `### [WORKING MEMORY]` before the current question while retrieval still uses the current question.
- Good: a document child is the ranked direct source; its parent appears only in source context and the prompt with `parent_context`, while analytics and `top_score` still use the child.
- Good: Admin reuses its cached `HybridRetrievalService` with the same database, embedding, and rerank dependencies, and settings updates invalidate that cache.
- Base: realtime status query with no hits still sends prompt guidance saying realtime backend state cannot be confirmed.
- Bad: sensitive query goes through embedding or search; the UI can imply the platform retrieved secret knowledge.
- Bad: provider errors such as unsupported model names surface as generic `internal error` in the answer bubble.
- Bad: provenance exists only inside `metadata`, or a consumer reads metadata to repair a missing canonical identity field.
- Bad: parent context is appended to ranked retrieval results, increasing hit counts or changing evaluation/analytics ids.
- Bad: the assistant copies vector, lexical, fusion, or rerank logic instead of calling `HybridRetrievalService`.
- Bad: old conversation history is appended as a full transcript without truncation or working-memory boundaries.

### 6. Tests Required

- Unit test for SSE formatting covering `meta`, `step`, `delta`, `done`, and `error`.
- Regression test for sensitive short-circuit asserting embedding/search/LLM are not called.
- Regression test for realtime status prompt constraints.
- Regression test for answer-generation model failure asserting failed step + `error` event.
- Unit test for `normalize_conversation_context()` and `build_user_prompt(..., conversation_context=...)`.
- Unit test for `assistant_document_payload()` top-level provenance fields.
- Unit test asserting `assistant_document_payload()` rejects non-canonical documents and never depends on `RetrievedDocument` aliases.
- Admin assistant test asserting one retrieval-service call with `include_parent_context=True` and explicit `use_kg=False`.
- Boundary test asserting direct candidates drive the retrieval step, `top_score`, and analytics while parent documents appear only in source context, `done`, and the prompt.
- Settings test asserting cached retrieval is cleared and rebuilt with the current shared DB/embedding/rerank dependencies.
- Frontend lint/build or focused type check for changed assistant source fields.

### 7. Wrong vs Correct

#### Wrong

```python
vector_docs = self.database().search_knowledge(query_embedding, top_k=limit, min_score=min_score)
keyword_docs = self.database().search_knowledge_text(query, top_k=limit, query_terms=terms)
candidates = fuse_retrieval_candidates(vector_docs=vector_docs, keyword_docs=keyword_docs, top_k=top_k)
documents = [*candidates, *parent_documents]
```

This duplicates the canonical service and mixes ranked evidence with unranked parent context.

#### Correct

```python
analysis = analyze_query(question, chat)
if analysis.safety_action == "refuse":
    yield {"type": "done", "answer_draft": SENSITIVE_REFUSAL_MESSAGE, "documents": []}
    return

result = self.hybrid_retrieval_service().retrieve(
    analysis.query_rewrite or question,
    include_parent_context=True,
    use_kg=False,
)
ranked_documents = [candidate.document for candidate in result.candidates]
prompt_documents = [*ranked_documents, *result.parent_documents]
```

The unsafe query stops first; the normal path delegates once and keeps direct ranking separate from parent prompt context.
