"""SQLite-backed MemoryStore — episodic, provenance-stamped, full-text searchable.

Implements the Strands `MemoryStore` Protocol so the agent's long-term memory
lives in a local SQLite database. No external services, no API keys required.

What this store gives Loop beyond a flat key-value of facts:

* **Provenance on every memory.** The `add_memory` tool only lets the model pass
  *content* — it cannot attach metadata. So we stamp each saved memory with *who*
  said it, in *which channel / workspace*, via *which Loop app*, and *when*,
  pulled from the active request (`loop.context.RequestState`). This is what makes
  a saved episode auditable and what lets recall say "decided by @alice in #infra
  on 2026-06-20" instead of a context-free sentence.
* **Full-text recall (FTS5).** Search is an FTS5 `MATCH` (ranked by BM25, then
  recency) instead of a blind `LIKE '%q%'`, so "when's standup" finds "Team
  standup is at 10am" even though the words don't line up. Falls back to `LIKE`
  automatically if the SQLite build lacks FTS5.

The protocol requires:
  - attributes: name, description, max_search_results, writable, extraction
  - methods:    async search(query, options), async add(content, metadata)

Strands auto-injects search matches into the agent's context before each model
call, so recall is passive — the agent never has to remember to look things up.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strands.memory.types import MemoryEntry, SearchOptions

from loop import context as reqctx

log = logging.getLogger("loop.memory")

DEFAULT_DB_PATH = "./data/loop.db"

# Provenance keys we promote to dedicated, queryable columns. Everything else a
# caller passes in `metadata` is preserved in the JSON blob.
_PROVENANCE_COLS = ("kind", "author", "channel", "team", "source", "thread_ts")

# Words to ignore when building an FTS query — they carry no signal and FTS5
# would otherwise weight them. Kept tiny on purpose.
_STOPWORDS = frozenset(
    "a an the is are was were be been being of to in on at for and or but with "
    "what when where who whom which how why do does did our we i you it this that".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _ensure_parent(path: str) -> None:
    Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)


def _fts_match(query: str) -> str | None:
    """Turn a natural-language query into a safe FTS5 MATCH expression.

    FTS5 has its own query grammar (``"`` phrases, ``*`` prefix, ``AND/OR/NEAR``
    operators, column filters) — feeding raw user text in is both a syntax-error
    risk and an injection risk. We reduce the query to lowercase alphanumeric
    tokens (dropping stopwords), prefix-match each (``standup*`` matches
    ``standups``), and OR them for recall. Returns None when nothing usable is
    left, so the caller can fall back to LIKE.
    """
    tokens = [t for t in _TOKEN_RE.findall(query.lower()) if t not in _STOPWORDS and len(t) > 1]
    if not tokens:
        # Keep single meaningful chars / numbers if that's all there is.
        tokens = [t for t in _TOKEN_RE.findall(query.lower()) if t not in _STOPWORDS]
    if not tokens:
        return None
    return " OR ".join(f"{t}*" for t in tokens)


class SqliteMemoryStore:
    """Long-term, provenance-stamped, full-text-searchable memory for Loop."""

    name = "loop_memory"
    description = (
        "Loop's persistent memory of crucial facts, decisions, preferences, and "
        "episodes — each stamped with who said it, where, and when."
    )
    max_search_results = 5
    writable = True
    extraction = False  # the agent decides what is worth remembering via add_memory

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
        _ensure_parent(self.db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_entries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                content    TEXT NOT NULL,
                metadata   TEXT,
                kind       TEXT,
                author     TEXT,
                channel    TEXT,
                team       TEXT,
                source     TEXT,
                thread_ts  TEXT,
                created_at INTEGER NOT NULL DEFAULT (unixepoch())
            )
            """
        )
        self._migrate()
        self._fts = self._init_fts()
        self._conn.commit()

    # --- schema upkeep -----------------------------------------------------
    def _migrate(self) -> None:
        """Idempotently add any columns missing from an older DB."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(memory_entries)").fetchall()}
        for col in ("metadata", *_PROVENANCE_COLS):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE memory_entries ADD COLUMN {col} TEXT")

    def _init_fts(self) -> bool:
        """Create the FTS5 mirror of memory_entries + a sync trigger; backfill once.

        Returns False (and logs) if this SQLite build has no FTS5 — search then
        degrades to LIKE rather than crashing.
        """
        try:
            existed = bool(
                self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_fts'"
                ).fetchone()
            )
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(content, content='memory_entries', content_rowid='id')
                """
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
                # Backfill rows written before the FTS index existed.
                self._conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            return True
        except sqlite3.OperationalError as err:
            log.warning("FTS5 unavailable, falling back to LIKE search: %s", err)
            return False

    # --- read --------------------------------------------------------------
    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        limit = int((options or {}).get("max_search_results") or self.max_search_results)
        rows = self._search_fts(query, limit) if self._fts else None
        if rows is None:
            rows = self._search_like(query, limit)
        return [self._row_to_entry(row) for row in rows]

    def _search_fts(self, query: str, limit: int) -> list[tuple] | None:
        match = _fts_match(query)
        if not match:
            return self._search_like(query, limit)  # nothing to match — recent-ish via LIKE
        sql = """
            SELECT m.content, m.metadata, m.kind, m.author, m.channel,
                   m.team, m.source, m.thread_ts, m.created_at
            FROM memory_fts f
            JOIN memory_entries m ON m.id = f.rowid
            WHERE memory_fts MATCH ?
            ORDER BY bm25(memory_fts), m.created_at DESC
            LIMIT ?
        """
        try:
            with self._lock:
                return self._conn.execute(sql, (match, limit)).fetchall()
        except sqlite3.OperationalError as err:
            log.warning("FTS search failed (%s); falling back to LIKE", err)
            return None

    def _search_like(self, query: str, limit: int) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT content, metadata, kind, author, channel,
                       team, source, thread_ts, created_at
                FROM memory_entries
                WHERE content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()

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
        # Promote the structured columns (and a human-readable date) so the
        # injection format / search_memory tool can surface provenance.
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
        """Persist a memory, auto-stamping provenance from the active request.

        `metadata` (when a caller passes a dict) is merged over the request's
        provenance — explicit values win. Provenance keys (`author`, `channel`,
        `team`, `source`, `thread_ts`, `kind`) are promoted to columns for
        querying/scoping; the full merged dict is also kept as JSON.
        """
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
                    (content, metadata, kind, author, channel, team, source, thread_ts)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    content, meta_json, cols["kind"], cols["author"], cols["channel"],
                    cols["team"], cols["source"], cols["thread_ts"],
                ),
            )
            self._conn.commit()
            rid = int(cur.lastrowid or 0)
        log.info(
            "memory stored id=%s author=%s channel=%s source=%s chars=%d",
            rid, cols["author"], cols["channel"], cols["source"], len(content),
        )
        return rid
