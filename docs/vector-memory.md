# Hybrid memory — libSQL / Turso (lexical + semantic, Agentic RAG)

> Backend behind `LOOP_MEMORY_BACKEND=hybrid`. The default backend stays the
> zero-dep FTS5 + provenance store; turn this on for hybrid retrieval. Verified
> end-to-end (unit tests + a live MiniMax M3 agent turn) before merge to `main`.

## What it adds

`loop/vector_store.py` (`HybridMemoryStore`) fuses **two** retrievers over one
[libSQL / Turso](https://github.com/tursodatabase) database:

- **Lexical** — FTS5 `MATCH` ranked by BM25 (exact words, names, ids, numbers).
- **Semantic** — native vector search (`F32_BLOB` + `libsql_vector_idx` +
  `vector_top_k`) over text embeddings (meaning, paraphrase, synonyms).

…merged with **Reciprocal Rank Fusion (RRF)**: a memory strong on *either* signal
surfaces; strong on *both* wins. This is the standard hybrid-RAG pattern and beats
either signal alone — e.g. *"where do we keep the runbook for handling outages?"*
recalls *"the on-call playbook lives in Notion under SRE"* (no shared words) via
the semantic half, while an exact name/number is nailed by the lexical half.

Everything else matches the `main` store: one local SQLite-compatible file (no
server), same provenance stamping (who/where/when from `RequestState`), same
`MemoryEntry` shape — so the agent, tools, and injection format are unchanged.
Exact-duplicate content is de-duplicated on write.

Why libSQL/Turso over a standalone vector DB: it **is** SQLite, so content,
provenance columns, the FTS5 index, and vectors all live together (one file, one
transaction), and it can later sync to Turso cloud with no code change.

## Do I need an embedding API key?

**No — not by default.** libSQL/Turso *stores and searches* vectors but does
**not generate** them, so the semantic half needs an embedding model. It's
pluggable (`loop/embeddings.py`) and defaults to a local one:

| `LOOP_EMBEDDINGS` | Key? | Notes |
|---|---|---|
| `fastembed` (default) | **No** | Local ONNX, `BAAI/bge-small-en-v1.5` (384-dim). Downloads the model once (~90 MB). No rate limit. |
| `minimax` | Key only | `embo-01`. **No Group ID needed** on `api.minimax.io` — the `ANTHROPIC_AUTH_TOKEN`/`MINIMAX_API_KEY` alone works. Shares your account's RPM with the chat model. |
| `openai` | Yes | `text-embedding-3-small` (1536-dim); needs `OPENAI_API_KEY`. |
| `hashing` | No | Deterministic, **not** semantic. Tests / last-resort fallback only. |

If the chosen provider's dep/key is missing, the embedder falls back to `hashing`
with a warning rather than crashing. **Default is `fastembed`** — fully local, no
key, no rate limit (recommended for the demo).

## Enable it

```bash
pip install -e ".[vector]"         # libsql-experimental + fastembed
```
```dotenv
LOOP_MEMORY_BACKEND=hybrid         # aliases: libsql / turso / vector
LOOP_EMBEDDINGS=fastembed          # local, no API key
LOOP_VECTOR_DB_PATH=./data/loop_vec.db
# Optional — sync the embedded file to Turso cloud:
# TURSO_DATABASE_URL=libsql://<db>.turso.io
# TURSO_AUTH_TOKEN=...
```

Tests (the RRF unit test always runs; libSQL/fastembed cases skip if not installed):
```bash
python -m tests.test_vector_memory
```

## Status / caveats

- ✅ Verified: FTS5 + `F32_BLOB`/`vector_top_k` + RRF fusion + provenance + dedup
  (`tests/test_vector_memory.py`), **and** a live MiniMax M3 agent turn that saved
  then semantically recalled with provenance citation.
- ⚠️ The vector column dimension is fixed at create time by the embedder. Switching
  embedding models (different dim) needs a fresh `loop_vec.db`.
- ⚠️ Hosted embeddings (`minimax`/`openai`) add per-op latency and share RPM; the
  local `fastembed` default avoids both.
