"""Hybrid (lexical + semantic) MemoryStore over libSQL / Turso — Agentic RAG.

Recall fuses two retrievers over one libSQL/Turso database:

  • **Lexical** — FTS5 `MATCH` ranked by BM25 (exact words, names, ids, numbers).
  • **Semantic** — native vector search (`F32_BLOB` + `libsql_vector_idx` +
    `vector_top_k`) over text embeddings (meaning, paraphrase, synonyms).

…then merges them with **Reciprocal Rank Fusion (RRF)** so a memory that ranks
well on *either* signal surfaces, and one that ranks well on *both* wins. This is
the standard hybrid-RAG retrieval pattern and beats either signal alone.

Why libSQL/Turso: it *is* SQLite, so the content, provenance columns, FTS5 index,
and vectors all live in one local file (no server), queried transactionally, and
can later sync to Turso cloud. It stores/searches vectors but does **not** create
them — embeddings come from `loop.embeddings` (default `fastembed`, local, no API
key; `minimax`/`openai` optional).

Same Strands `MemoryStore` contract, same provenance stamping, and same
`MemoryEntry` shape as the FTS5-only store on `main`, so the agent, injection
format, and tools are unchanged — only recall quality differs.

Enable:
    pip install "loop[vector]"
    LOOP_MEMORY_BACKEND=hybrid          # (libsql/turso/vector are aliases)
    LOOP_EMBEDDINGS=fastembed           # local, no key. See loop/embeddings.py
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strands.memory.types import MemoryEntry, SearchOptions

from loop import context as reqctx
from loop.embeddings import Embedder, get_embedder
from loop.storage import _fts_match  # reuse the injection-safe query sanitizer

log = logging.getLogger("loop.vector")

DEFAULT_VECTOR_DB_PATH = "./data/loop_vec.db"
_PROVENANCE_COLS = ("kind", "author", "channel", "team", "source", "thread_ts")
_RRF_K = 60  # Reciprocal Rank Fusion damping constant (standard default)


def _vector_literal(vec: list[float]) -> str:
    """Render an embedding as the text form vector32() accepts: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class HybridMemoryStore:
    """Hybrid lexical+semantic long-term memory over libSQL/Turso."""

    name = "loop_memory"
    description = (
        "Loop's hybrid (full-text + semantic) memory of crucial facts, decisions, "
        "preferences, and episodes — each stamped with who said it, where, and when."
    )
    max_search_results = 5
    writable = True
    extraction = False

    def __init__(self, db_path: str | None = None, embedder: Embedder | None = None) -> None:
        try:
            import libsql_experimental as libsql  # noqa: PLC0415 (optional dep)
        except ImportError as err:  # pragma: no cover
            raise RuntimeError(
                'libSQL not installed. Run: pip install "loop[vector]" '
                "(or set LOOP_MEMORY_BACKEND=sqlite for the FTS5-only store)."
            ) from err

        self.db_path = db_path or os.environ.get("LOOP_VECTOR_DB_PATH", DEFAULT_VECTOR_DB_PATH)
        Path(self.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder or get_embedder()
        self.dim = self.embedder.dim
        self._lock = threading.Lock()

        sync_url = os.environ.get("TURSO_DATABASE_URL")
        auth = os.environ.get("TURSO_AUTH_TOKEN")
        if sync_url:  # embedded replica synced to Turso cloud
            self._conn = libsql.connect(self.db_path, sync_url=sync_url, auth_token=auth)
            self._conn.sync()
        else:  # purely local embedded file (default)
            self._conn = libsql.connect(self.db_path)

        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS memory_entries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                content    TEXT NOT NULL,
                metadata   TEXT,
                kind TEXT, author TEXT, channel TEXT, team TEXT, source TEXT, thread_ts TEXT,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                embedding  F32_BLOB({self.dim})
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS memory_vec_idx "
            "ON memory_entries(libsql_vector_idx(embedding))"
        )
        self._init_fts()
        self._conn.commit()
        log.info("hybrid memory ready (embedder=%s dim=%d db=%s)", self.embedder.name, self.dim, self.db_path)

    def _init_fts(self) -> None:
        existed = bool(
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
            ).fetchone()
        )
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
            "USING fts5(content, content='memory_entries', content_rowid='id')"
        )
        self._conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memory_entries_ai
            AFTER INSERT ON memory_entries BEGIN
                INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
            END
            """
        )
        if not existed:
            self._conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")

    # --- read --------------------------------------------------------------
    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        limit = int((options or {}).get("max_search_results") or self.max_search_results)
        pool = max(limit * 4, 20)
        with self._lock:
            lexical = self._lexical_ids(query, pool)
            semantic = self._semantic_ids(query, pool)
            ranked = _rrf_fuse(lexical, semantic)[:limit]
            if not ranked:
                return []
            rows = self._fetch(ranked)
        return [self._row_to_entry(rows[i]) for i in ranked if i in rows]

    def _lexical_ids(self, query: str, pool: int) -> list[int]:
        match = _fts_match(query)
        if not match:
            return []
        try:
            rows = self._conn.execute(
                "SELECT f.rowid FROM memory_fts f WHERE memory_fts MATCH ? "
                "ORDER BY bm25(memory_fts) LIMIT ?",
                (match, pool),
            ).fetchall()
            return [int(r[0]) for r in rows]
        except Exception as err:  # noqa: BLE001
            log.warning("lexical search failed: %s", err)
            return []

    def _semantic_ids(self, query: str, pool: int) -> list[int]:
        try:
            qvec = _vector_literal(self.embedder.embed_one(query))
            rows = self._conn.execute(
                "SELECT id FROM vector_top_k('memory_vec_idx', vector32(?), ?)",
                (qvec, pool),
            ).fetchall()
            return [int(r[0]) for r in rows]
        except Exception as err:  # noqa: BLE001
            log.warning("semantic search failed: %s", err)
            return []

    def _fetch(self, ids: list[int]) -> dict[int, tuple]:
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"""
            SELECT id, content, metadata, kind, author, channel, team, source, thread_ts, created_at
            FROM memory_entries WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall()
        return {int(r[0]): r[1:] for r in rows}

    def _row_to_entry(self, row: tuple) -> MemoryEntry:
        content, meta_json, kind, author, channel, team, source, thread_ts, created_at = row
        metadata: dict[str, Any] = {}
        if meta_json:
            try:
                parsed = json.loads(meta_json)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError):
                pass
        for key, val in (
            ("kind", kind), ("author", author), ("channel", channel),
            ("team", team), ("source", source), ("thread_ts", thread_ts),
        ):
            if val and key not in metadata:
                metadata[key] = val
        if created_at and "date" not in metadata:
            metadata["date"] = datetime.fromtimestamp(int(created_at), timezone.utc).strftime("%Y-%m-%d")
        return MemoryEntry(content=content, store_name=self.name, metadata=metadata or None)

    # --- write -------------------------------------------------------------
    async def add(self, content: str, metadata: Any = None) -> int:
        merged: dict[str, Any] = {}
        state = reqctx.current()
        if state is not None:
            merged.update(state.provenance())
        if isinstance(metadata, dict):
            merged.update(metadata)
        elif metadata is not None:
            merged["note"] = str(metadata)

        cols = {c: merged.get(c) for c in _PROVENANCE_COLS}
        meta_json = json.dumps(merged, ensure_ascii=False, default=str) if merged else None
        vec = _vector_literal(self.embedder.embed_one(content))

        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM memory_entries WHERE content = ? LIMIT 1", (content,)
            ).fetchone()
            if dup:
                log.info("memory dedup: identical content already stored id=%s", dup[0])
                return int(dup[0])
            cur = self._conn.execute(
                """
                INSERT INTO memory_entries
                    (content, metadata, kind, author, channel, team, source, thread_ts, embedding)
                VALUES (?,?,?,?,?,?,?,?, vector32(?))
                """,
                (
                    content, meta_json, cols["kind"], cols["author"], cols["channel"],
                    cols["team"], cols["source"], cols["thread_ts"], vec,
                ),
            )
            self._conn.commit()
            rid = int(getattr(cur, "lastrowid", 0) or 0)
        log.info("hybrid memory stored id=%s author=%s channel=%s chars=%d",
                 rid, cols["author"], cols["channel"], len(content))
        return rid


def _rrf_fuse(*ranked_lists: list[int]) -> list[int]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank). Higher = better; ties keep
    first-seen order. A doc strong on either retriever surfaces; strong on both wins."""
    scores: dict[int, float] = {}
    order: list[int] = []
    for ids in ranked_lists:
        for rank, doc_id in enumerate(ids):
            if doc_id not in scores:
                scores[doc_id] = 0.0
                order.append(doc_id)
            scores[doc_id] += 1.0 / (_RRF_K + rank)
    return sorted(order, key=lambda d: scores[d], reverse=True)


# Back-compat alias (the backend selector + older imports use this name).
LibsqlVectorMemoryStore = HybridMemoryStore
