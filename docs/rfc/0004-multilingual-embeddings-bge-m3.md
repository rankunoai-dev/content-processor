# RFC 0004: Multilingual meaning search with bge-m3 (Ollama)

**Status**: Proposed, slice 1 implemented on `feat/multilingual-bge-m3` · **Date**: 2026-11-16
**Related**: RFC 0002 (knowledge base, stages D–E), `src/modules/content_processor/knowledge/`

## 1. Goal

Search and question-answering over the captured websites should find passages by meaning across languages (for example an English question answered from a Hindi page) and should handle long passages. bge-m3 is the target model: 1024-dimensional vectors, up to 8192 tokens per input, trained for multilingual retrieval. It runs locally through Ollama at $0 with the existing local-only endpoint policy.

## 2. What already exists (verified in code)

| Piece | State |
|---|---|
| Model choice | `CP_EMBED_MODEL=bge-m3`; `open_embedder("auto")` uses Ollama when reachable and the model is pulled, else the CPU MiniLM fallback, else hashing |
| Upgrade on index | `index_run`: a store made with a lower-ranked model is reset when a better one indexes (`model_rank`: words 0, CPU 1, model server 2) |
| Downgrade on index | refused with a message when a worse model would mix into a better store |
| Federated search | embeds with each store's own model, catches `RuntimeError`/`ValueError` and falls back to exact words, and says so |
| Local-only endpoint | `endpoint_decision` accepts this machine, private addresses and in-house hosts |
| Chunking | 500-token target, 80 minimum, 2000 hard cap (`chunking.py`) |

The CPU MiniLM model has a 128-token input limit, so today's 500-token chunks are silently cut for meaning. bge-m3 reads the whole chunk, which is a quality gain in itself.

## 3. Breaking points found

Each row is a failure we can reproduce or read in code. Items marked **fixed in slice 1** have a test.

| # | Breaking point | Where | Consequence | Handling |
|---|---|---|---|---|
| B1 | Query model differs from the store's model (single-store callers: knowledge CLI, question answering) | `retrieval.hybrid_search` | Different dimensions: a raw `ValueError` (matmul shape). Same dimensions, different model: silently meaningless ranking | **Fixed in slice 1**: `EmbedderMismatchError` (a `RuntimeError`) with a plain message; federated search already catches it and falls back to exact words |
| B2 | Ollama not installed or stopped | `open_embedder` | Silent drop to the CPU model or hashing; a store could mix models over time | Existing rank rule refuses to mix (B1 guard now covers queries too); the store keeps its model |
| B3 | Model pulled under a different tag (`bge-m3` vs `bge-m3:567m`) | `ModelServer.has` | Looks missing although it is present | Existing `:latest` handling; add the exact tag to the settings doc |
| B4 | Batch of long chunks exceeds the 60 s request timeout on CPU | `LocalEmbedder.embed` sends all texts in one request | `None` returned: the batch is lost and the run reports failures | **To do (slice 2)**: batches of N texts with a timeout that scales with size; retry a failed batch once, smaller |
| B5 | Store built with 384-dim vectors, then bge-m3 (1024) | `index_run` reset | Full re-embedding of every page: slow on CPU (time in section 6) | Reset is intentional; progress is reported; the store keeps the old vectors until the new model finishes (**to do**: build into a new store, switch on success) |
| B6 | Model server down halfway through indexing | `index_run` | Partially embedded store | Chunks already stored are kept with their model name; the next run resumes (the known-vector cache is keyed by text hash and model) |
| B7 | Ollama returns vectors not of the stored dimension | `LocalEmbedder` | Corrupt store or matmul failure | **To do (slice 2)**: check every vector's length against the expected dimension before storing; reject the batch otherwise |
| B8 | Deployment: the target container has 1–4 GB RAM (`deployment-resource-limits`) | deploy | bge-m3 needs roughly 1.5–2.5 GB resident beside Ollama and the app | The model runs on a separate in-house model server (`CP_MODEL_ENDPOINT` = a private host). The container only needs the endpoint. Decision needed (section 7) |
| B9 | Long chunks raise per-query cost in the question path | `retrieval.ask` | More tokens to the local chat model | Unchanged prompt budget; check in slice 3 |
| B10 | Cross-language recall is not measured | all | We would ship without knowing it is better | A labelled set of paired passages (English, Hindi, others) with expected matches; acceptance in section 6 |
| B11 | Vector search holds the whole matrix in memory | `SqliteVectorStore._load_matrix` | 1024 floats per chunk: a 7,000-page run with ~10 chunks each is ~290 MB of float32 | Measure in slice 3; move to a memory-mapped matrix or a vector index if over budget |
| B12 | Model name is a setting; a typo silently selects the fallback | `open_embedder` | Quality drops without an error | Log which embedder was chosen on each index run (already returned as a note) and show it in the run report |

## 4. Architecture

```
run pages ──> chunking (500 tokens) ──> embedder (auto)
                                          │ ollama bge-m3  (1024-d, primary)
                                          │ cpu MiniLM     (384-d, fallback)
                                          └ hashing        (words only)
                       │
                 vector store (model + dims recorded in meta)
                       │
   query ──> same embedder as the store (B1 guard) ──> hybrid search (vectors + exact words, RRF)
```

Invariant: a query is embedded with the model the store was built with. Enforced in slice 1 (B1). Stores never mix models; a better model resets the store, a worse one is refused.

## 5. Slices

1. **Slice 1 (this branch, no model needed):** query-model guard (B1) and tests. Safe to merge: it only turns a crash or a silent wrong answer into a clear error.
2. **Slice 2:** batched, size-aware Ollama calls with per-batch retry and dimension checks (B4, B7); build into a new store and switch on success (B5).
3. **Slice 3:** install Ollama and pull `bge-m3` on this machine (needs approval, about 1.2 GB download); run the labelled cross-language set; measure latency, memory (B11) and recall; record results in this RFC.
4. **Slice 4:** deployment: separate model server, endpoint settings and the B8 runbook.

## 6. Acceptance

- Zero stores mix model families (checked by test).
- On the labelled cross-language set, bge-m3 finds at least as many correct passages as the CPU MiniLM at the same top-k, and clearly more across languages. Target to be set after slice 3 from the measured baseline, not guessed.
- Indexing time per page and query latency measured on this machine and reported; a 5,000-page run must finish within its budget.
- Peak memory during search recorded (B11).

## 7. Decisions needed from you

1. **Install Ollama and pull bge-m3 on this computer?** Needed for slice 3. About 1.2 GB download and a system install.
2. **Where should bge-m3 run in production?** On an in-house server (recommended, given the 1–4 GB container limit) or in the same container if the limit is raised.
3. **Labelled cross-language set:** who provides the paired passages? Without them, slice 3 can measure speed and memory but not quality.

## 8. Edge cases to test

- Query in a language the store does not contain: returns exact-word hits or an empty list, never an error.
- Empty query, very long query (longer than 8192 tokens): truncated with a note.
- A chunk longer than the model's input limit: split before embedding (chunking already caps at 2000 tokens).
- Two stores in one run, one with bge-m3 and one with the CPU model (federated): each searched with its own model.
- Model server returns an error only for some batches: those chunks are retried, the rest are kept.
- Restarting halfway through an index with a changed model: the store resets only when the new model finishes its first batch successfully (decision in slice 2).
