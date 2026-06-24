"""libSQL / Turso vector-backed MemoryStore — semantic recall.

EXPERIMENTAL — lives on the `feat/vector-memory-turso` branch, not on `main`.

Why libSQL/Turso: it is SQLite-compatible (so the schema, provenance columns, and
mental model are identical to `loop.storage.SqliteMemoryStore`) but adds **native
vector search** — `F32_BLOB(n)` columns, `vector32()`, an ANN index via
`libsql_vector_idx`, and the `vector_top_k()` table function. Same single local
file as today (no server); optionally syncable to Turso cloud / embedded replicas
later by passing `sync_url` + `auth_token` to `libsql.connect`.

This swaps lexical FTS5 recall for semantic recall: "where do we keep the deploy
runbook" can find "the on-call playbook lives in Notion" even with zero shared
words, because both map to nearby embedding vectors.

Enable (on the branch):
    pip install "loop[vector]"          # libsql-experimental + fastembed
    LOOP_MEMORY_BACKEND=libsql
    LOOP_EMBEDDINGS=fastembed           # default; no API key. See loop/embeddings.py

Drop-in: it implements the same Strands `MemoryStore` protocol, stamps the same
provenance (from `RequestState`), and returns `MemoryEntry` with provenance
metadata — so the agent and the injection format are unchanged.
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

log = logging.getLogger("loop.vector")

DEFAULT_VECTOR_DB_PATH = "./data/loop_vec.db"
_PROVENANCE_COLS = ("kind", "author", "channel", "team", "source", "thread_ts")


def _vector_literal(vec: list[float]) -> str:
    """Render an embedding as the text form vector32() accepts: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class LibsqlVectorMemoryStore:
    """Semantic long-term memory over libSQL/Turso native vector search."""

    name = "loop_memory"
    description = (
        "Loop's semantic memory of crucial facts, decisions, preferences, and "
        "episodes — each stamped with who said it, where, and when."
    )
    max_search_results = 5
    writable = True
    extraction = False

    def __init__(self, db_path: str | None = None, embedder: Embedder | None = None) -> None:
        try:
            import libsql_experimental as libsql  # noqa: PLC0415 (optional dep)
        except ImportError as err:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                'libSQL not installed. Run: pip install "loop[vector]" '
                "(or set LOOP_MEMORY_BACKEND=sqlite to use the FTS5 store)."
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
        self._conn.commit()

    # --- read --------------------------------------------------------------
    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        limit = int((options or {}).get("max_search_results") or self.max_search_results)
        qvec = _vector_literal(self.embedder.embed_one(query))
        sql = """
            SELECT m.content, m.metadata, m.kind, m.author, m.channel,
                   m.team, m.source, m.thread_ts, m.created_at
            FROM vector_top_k('memory_vec_idx', vector32(?), ?) AS v
            JOIN memory_entries m ON m.rowid = v.id
            ORDER BY m.created_at DESC
        """
        with self._lock:
            rows = self._conn.execute(sql, (qvec, limit)).fetchall()
        return [self._row_to_entry(row) for row in rows]

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
        log.info("vector memory stored id=%s author=%s channel=%s chars=%d",
                 rid, cols["author"], cols["channel"], len(content))
        return rid
