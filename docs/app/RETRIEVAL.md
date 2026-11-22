# Retrieval: how the knowledge base finds passages, and why each choice

Code: `src/modules/content_processor/knowledge/`: `retrieve.py` (the one retriever,
RFC 0005 step 1), `vector_store.py`, `reranker.py`, `query_dates.py`; `retrieval.py`
(answers) and `federated.py` re-export the search names for older callers. Settings: `CP_VECTOR_CACHE_MB`,
`CP_HNSW_EF_SEARCH`, `CP_RERANK*`, `CP_MMR_LAMBDA` (see `.env.example`).

## Pipeline for one question

The same function, `retrieve(scope, query, filters, k)`, runs for one website (`kb search`,
`kb ask`) and for a project or several projects (the app's search, Claude Desktop's
`search_knowledge`, action cards). Steps 1-3 run in each website with its own model;
steps 4-7 run once, over all websites together.

1. **Embed the question** with the model the website was indexed with (refused when it
   differs: vectors of two models are not comparable).
2. **Dense retrieval** (vector similarity, cosine) and **sparse retrieval** (BM25 over
   FTS5 in SQLite, `ts_rank_cd` over a GIN `tsvector` index in Postgres), 50 candidates
   each. Question words such as "what", "is", "the" are left out of the keyword query.
3. **Metadata pre-filtering**: an offering scope, a publication-date range or passage roles
   (claim, feature, pricing, proof, FAQ, call to act) chosen by a person or a tool are
   applied *before* scoring (SQLite masks the matrix and puts the
   filter in the FTS query; Postgres filters in SQL with pgvector's iterative scan), so a
   narrow filter still returns its best passages.
4. **Reciprocal rank fusion** (k = 60) merges every website's two rankings into one. A question that names a
   date or asks for "the latest" adds a third ranking (closest publication date, or
   newest first), counted twice: a hint that reorders, never a filter ("the 2026 annual
   meeting" was announced in 2025).
5. **One copy of the same text**, across all websites: a paragraph repeated on many pages
   (a teaser on every podcast page, a disclaimer, a release syndicated by two websites)
   is kept once, at its best rank.
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

Comparisons (`per_site`): every website's best candidates are always reranked, and MMR
takes up to that many passages from each website. Diversity compares passages of one
website by their vectors and passages of two websites by their words (their vectors come
from different models). Before RFC 0005 the project search reranked and diversified each
website separately, then merged them: a passage the reranker would have ranked first
could be cut by one website's diversity step, and a release syndicated by two websites
came back twice.

## The concepts, and where each is used

| Concept | Here | Why |
|---|---|---|
| KNN (exact) | SQLite store: numpy over every vector; themes, similar pages, semantic report, topic drift | Exact and fast at this size (1,308 passages: 0.5 ms) |
| ANN / HNSW | Postgres store: `hnsw (vector_cosine_ops)` | The right index for many vectors that change; `ef_search` raised to 100 (pgvector's 40 returned at most 40 rows), iterative scan on pgvector 0.8+, exact rerun of a short filtered search on older versions |
| IVF, product quantization | Not used | Pay off at millions of vectors; IVF needs retraining as pages change |
| Vector index vs metadata indexes | HNSW for vectors; B-tree on site, offering, page, text hash, publication date; FTS5 / GIN for words | Hybrid search needs both |
| Pre- vs post-filtering | Pre-filtering in both stores, for offering, dates and passage role | Post-filtering after HNSW lost a small website's rows (all websites share one index) |
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

The same 40 questions as the 22 November test (`docs/eval/john-hancock-2026-11-22/`),
answered extractively (no local instruct model), on a copy of the store with the
publication dates added:

| | Before | After |
|---|---|---|
| Answer contains the right fact | 15 of 36 | **24 of 36** |
| Right page first in search | 18 of 36 | **29 of 36** |
| Right page in the first 3 / 10 | 28 / 30 of 36 | 31 / 31 of 36 |
| Unanswerable questions answered "not found" | 0 of 4 | 2 of 4 |
| Answerable questions wrongly "not found" | 1 | 1 |
| Quotes word for word | 81 of 81 | 103 of 103 |
| Time per question (typical, this computer) | 0.2-0.4 s | about 3 s search, 4-5 s answer (up to 15 s while a scrape runs) |

By intent after: facts 9 of 9, people 3 of 3, paraphrase and other language 2 of 2,
explanations 4 of 5, rules 2 of 3, recency 1 of 2, numbers in tables 3 of 10, exact
codes 0 of 2, not on the website 2 of 4.

What is still missing:

- **Numbers in tables** (7 of 10 fail): tables are stored one cell per line, so a value
  is not next to its fund and column. The fix is in extraction (rows with headers), then
  re-indexing.
- **Two unanswerable questions still answered**: "Does John Hancock offer pet
  insurance?" and a false premise ("Why did John Hancock shut down Vitality?") score like
  real answers with the reranker; a local instruct model is needed to say "no".
- **A form code alone** ("ACHEFTFM") finds its page first, but the code is only in the
  title and address, so no sentence answers it.
- **Speed**: the reranker is about 0.2 s per passage on this 20-thread CPU when it is not
  busy; a small container (2 vCPUs) would take several times longer and needs about
  1.1 GB of memory, so `CP_RERANK=auto` uses it only where the model was put in the
  model cache. `CP_RERANK_POOL`, `CP_RERANK_SENTENCES` and `CP_RERANK_THREADS` trade time
  for quality.

**Dates in existing stores**: the publication date is written when a website is indexed.
A store indexed before this change has none, and date hints stay off for it (they need
dates on most candidates) until the run is indexed again (unchanged sections keep their
vectors, so this is quick).
