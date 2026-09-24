# Chinese Lexical Retrieval Options

## Current State

The current lexical score is a fixed sum of `ILIKE` substring matches over title, `search_text`, content, and up to 20 hand-derived query terms. It has no term frequency, inverse document frequency, document-length normalization, or term-frequency saturation, so it is not BM25.

The schema contains a GIN expression index over `to_tsvector('simple', search_text)`, but no query uses `@@`, `tsquery`, or `ts_rank`. PostgreSQL `simple` tokenization also treats ordinary continuous Chinese text poorly.

## Option A: Indexed Chinese Substring Retrieval

Use PostgreSQL `pg_trgm` indexes for the fields used by substring matching, keep the current query-term/alias logic, and continue fusing lexical rank with vectors through RRF.

Pros:

* Smallest change to the current PostgreSQL architecture.
* Handles continuous Chinese substrings without an application token table.
* Can be validated with `EXPLAIN` and the existing retrieval evaluation workbench.

Cons:

* Still not BM25.
* Static field weights remain heuristic and require evaluation.
* Short one-character/two-character queries need explicit handling.

## Option B: Chinese FTS With PostgreSQL Extension

Install a Chinese tokenizer such as `zhparser` or `pg_jieba`, build a generated/expression `tsvector`, query it with `@@`, and rank with `ts_rank`/`ts_rank_cd` before RRF.

Pros:

* Inverted index and token storage remain inside PostgreSQL.
* Better word-level Chinese matching than the built-in `simple` configuration.

Cons:

* `ts_rank` is not strict BM25.
* Adds a database extension and deployment/upgrade contract.
* Dictionary quality and custom domain terms require operations tooling.

## Option C: True BM25 Engine

Use a PostgreSQL search extension that explicitly implements BM25, or a dedicated search engine, with a Chinese analyzer and an indexed document identity synchronized to `knowledge_chunks`.

Pros:

* Provides TF, IDF, document-length normalization, and BM25 saturation semantics.
* Better fit if lexical relevance is a core product requirement at larger scale.

Cons:

* Largest deployment and data synchronization change.
* Requires migration, backfill, health checks, rollback, and cross-entry consistency work.
* Must prove a measurable gain over vector + indexed substring + rerank before becoming default.

## Token Storage Answer

BM25 needs token/posting statistics, but the application does not always need a visible `tokens` column:

* A search engine or PostgreSQL extension stores the inverted index internally.
* A `tsvector` GIN index stores lexemes internally, although PostgreSQL built-in ranking is not strict BM25.
* A custom application-side BM25 implementation would need durable tokens/postings, term frequency, document frequency, and document length, plus exact invalidation on every content change.

## Recommendation

First repair retrieval correctness and measure the baseline. If the current deployment must remain plain PostgreSQL, use Option A and name it accurately as indexed lexical retrieval. If strict BM25 is a product requirement, evaluate Option C as a separate migration rather than hiding it inside the KG repair.

