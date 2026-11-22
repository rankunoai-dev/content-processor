# Retrieval: how the knowledge base finds passages, and why each choice

Code: `src/modules/content_processor/knowledge/` (`vector_store.py`, `retrieval.py`,
`reranker.py`, `query_dates.py`, `federated.py`). Settings: `CP_VECTOR_CACHE_MB`,
`CP_HNSW_EF_SEARCH`, `CP_RERANK*`, `CP_MMR_LAMBDA` (see `.env.example`).

## Pipeline for one question

1. **Embed the question** with the model the website was indexed with (refused when it
   differs: vectors of two models are not comparable).
2. **Dense retrieval** (vector similarity, cosine) and **sparse retrieval** (BM25 over
   FTS5 in SQLite, `ts_rank_cd` over a GIN `tsvector` index in Postgres), 50 candidates
   each. Question words such as "what", "is", "the" are left out of the keyword query.
3. **Metadata pre-filtering**: an offering scope or a publication-date range chosen by a
   person or a tool is applied *before* scoring (SQLite masks the matrix and puts the
   filter in the FTS query; Postgres filters in SQL with pgvector's iterative scan), so a
   narrow filter still returns its best passages.
4. **Reciprocal rank fusion** (k = 60) merges the two rankings. A question that names a
   date or asks for "the latest" adds a third ranking (closest publication date, or
   newest first), counted twice: a hint that reorders, never a filter ("the 2026 annual
   meeting" was announced in 2025).
5. **One copy of the same text**: a paragraph repeated on many pages (a teaser on every
   podcast page, a disclaimer, a re-published release) is kept once, at its best rank.
6. **Reranking** with a cross-encoder (`jinaai/jina-reranker-v2-base-multilingual`, 100+
   languages, CPU via fastembed): the best 12 candidates are read together with the
   question. Passages below `CP_RERANK_MIN_SCORE` (-0.5) are dropped; when none are left,
   the answer is "not found on the website". Near-equal scores are ordered by date when
   the question is about a date.
7. **Diversity** with maximal marginal relevance (`CP_MMR_LAMBDA` 0.75): each next passage
   balances relevance against similarity to those already chosen.
8. **Answer**: with a local instruct model, the passages are given best first and second
   best last (models lose the middle of long prompts), and every statement is checked
   against its quote. Without one, the answer is the best sentences, ranked by the
   reranker; headings and labels are never chosen, a bullet keeps its lead-in ("We do
   not: • sell..."), and an amount or date question prefers sentences that carry one.

Federated search (a project, or several) runs steps 1-5 per website with that website's
model, fuses the websites by rank, then reranks all candidates together on one scale.

## The concepts, and where each is used

| Concept | Here | Why |
|---|---|---|
| KNN (exact) | SQLite store: numpy over every vector; themes, similar pages, semantic report, topic drift | Exact and fast at this size (1,308 passages: 0.5 ms) |
| ANN / HNSW | Postgres store: `hnsw (vector_cosine_ops)` | The right index for many vectors that change; `ef_search` raised to 100 (pgvector's 40 returned at most 40 rows), iterative scan on pgvector 0.8+, exact rerun of a short filtered search on older versions |
| IVF, product quantization | Not used | Pay off at millions of vectors; IVF needs retraining as pages change |
| Vector index vs metadata indexes | HNSW for vectors; B-tree on site, offering, page, text hash, publication date; FTS5 / GIN for words | Hybrid search needs both |
| Pre- vs post-filtering | Pre-filtering in both stores | Post-filtering after HNSW lost a small website's rows (all websites share one index) |
| Vector DB vs relational | Vectors in the database with their verbatim text and evidence ids | Every answer must quote stored text; one store keeps them in step |
| Persistence | SQLite file per website (needs a persistent volume on Railway); Postgres | Postgres path has not yet run against a real server |
| Namespaces | One per tenant, project and website | Websites indexed with different models can be searched together |
| Memory | Vectors kept in memory across searches up to `CP_VECTOR_CACHE_MB` (256); stored without the zero padding of smaller models | Reading every vector from disk on each search took 300 ms for 0.5 ms of search; a 384-number model padded to 1,024 used 2.7x the memory |
| Top-k, choosing k | 5 passages for answers, 8 for Claude Desktop's search; with the reranker, k follows the evidence (only passages above the threshold) | |
| Similarity threshold | On reranker scores, not on vector cosine | Cosine of questions the site cannot answer (0.61-0.67) fell inside the range of answerable ones (0.24-0.90) |
| Dense, sparse, BM25, hybrid, RRF | Steps 2 and 4 | |
| MMR, diversity | Steps 5 and 7 | |
| Retriever vs reranker | Steps 2-4 retrieve; step 6 reranks | |
| Lost in the middle | Step 8 | |

## Measured on John Hancock (40 questions, CPU models)

See `docs/eval/john-hancock-2026-11-22/REPORT.md` for the cases. Results before and
after this change are in the lane log entry of 22 November 2026.
