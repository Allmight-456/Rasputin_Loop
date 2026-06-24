# Semantic memory — libSQL / Turso vector backend (experimental)

> **Branch:** `feat/vector-memory-turso`. Not merged to `main` until verified in a
> live Slack run. `main` ships the FTS5 + provenance store, which needs no extra
> deps and no API key.

## What it adds

A second `MemoryStore` (`loop/vector_store.py`) that swaps lexical FTS5 recall for
**semantic** recall using [libSQL / Turso](https://github.com/tursodatabase)
**native vector search** — `F32_BLOB(n)` columns, an ANN index via
`libsql_vector_idx`, and the `vector_top_k()` table function. Everything else is
identical to the `main` store: same single local SQLite-compatible file (no
server), same provenance stamping (who/where/when from `RequestState`), same
`MemoryEntry` shape — so the agent and the injection format are unchanged.

Why libSQL/Turso over a standalone vector DB: it **is** SQLite, so vectors live
next to the relational/provenance columns (one file, one query, transactional),
and it can later sync to Turso cloud / embedded replicas with no code change.

## Do I need an embedding API key?

**No — not by default.** Embeddings are pluggable (`loop/embeddings.py`):

| `LOOP_EMBEDDINGS` | Key needed? | Notes |
|---|---|---|
| `fastembed` (default) | **No** | Local ONNX, `BAAI/bge-small-en-v1.5` (384-dim). Downloads the model once (~90 MB). |
| `minimax` | Yes | `embo-01`; needs `MINIMAX_API_KEY` + `MINIMAX_GROUP_ID`. Separate endpoint from the chat model. |
| `openai` | Yes | `text-embedding-3-small` (1536-dim); needs `OPENAI_API_KEY`. |
| `hashing` | No | Deterministic, **not** semantic. Tests / last-resort fallback only. |

If the chosen provider's dep/key is missing, the embedder falls back to `hashing`
with a warning rather than crashing.

## Enable it

```bash
pip install "loop[vector]"        # libsql-experimental + fastembed
```
```dotenv
LOOP_MEMORY_BACKEND=libsql        # default is "sqlite" (the main FTS5 store)
LOOP_EMBEDDINGS=fastembed         # no API key
LOOP_VECTOR_DB_PATH=./data/loop_vec.db
# Optional — sync the embedded file to Turso cloud:
# TURSO_DATABASE_URL=libsql://<db>.turso.io
# TURSO_AUTH_TOKEN=...
```

Test (no model download — uses the hashing embedder):
```bash
python -m tests.test_vector_memory
```

## Status / caveats

- ✅ Verified end-to-end locally: table + `F32_BLOB`, vector index, `vector_top_k`
  ANN query, provenance round-trip (`tests/test_vector_memory.py`).
- ⚠️ The vector DB's dimension is fixed at create time by the embedder. Switching
  embedding models (different dim) needs a fresh `loop_vec.db`.
- ⚠️ Not yet exercised through a live `@Loop` Slack turn with `fastembed`, hence
  experimental / off `main`.
- 🔜 To merge: run a real Slack session on `LOOP_MEMORY_BACKEND=libsql` +
  `fastembed`, confirm save/recall, then fold the backend switch into `main`.
