"""Observability for Loop — structured per-interaction telemetry.

This is the smallest real slice of the production roadmap's Pillar 02
(observability) + Pillar 03 (data foundation) in `progress.md`: *see every
interaction, and keep the data to learn from.*

Every agent interaction (mention, DM, slash command) is wrapped in `record()`,
which:
  - assigns a short request id,
  - times the call end-to-end,
  - captures model id, token usage, tool calls, and outcome,
  - emits ONE structured JSON log line on the ``loop.obs`` logger, and
  - persists a row to the ``interactions`` SQLite table.

Nothing here changes the agent's behaviour — it only watches it. Query the
data later with e.g.::

    sqlite3 data/loop.db \\
      "SELECT entrypoint, channel, model_id, total_tokens, latency_ms, outcome
       FROM interactions ORDER BY id DESC LIMIT 20;"
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from loop.storage import DEFAULT_DB_PATH

log = logging.getLogger("loop.obs")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    """Lazily open (and create) the telemetry tables. Own connection so it never
    contends with the memory store's; WAL mode makes that safe."""
    global _conn
    if _conn is None:
        path = os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
        Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interactions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id    TEXT NOT NULL,
                ts            INTEGER NOT NULL DEFAULT (unixepoch()),
                entrypoint    TEXT,
                team          TEXT,
                channel       TEXT,
                channel_type  TEXT,
                "user"        TEXT,
                thread_ts     TEXT,
                prompt        TEXT,
                model_id      TEXT,
                input_tokens  INTEGER,
                output_tokens INTEGER,
                total_tokens  INTEGER,
                tool_calls    TEXT,
                model_calls   INTEGER,
                guardrail_hits INTEGER,
                latency_ms    INTEGER,
                outcome       TEXT,
                error         TEXT
            )
            """
        )
        # Per-step trace: every model call, tool call, reasoning, guardrail hit,
        # correlated to an interaction by request_id. Substrate for Pillars 01/02.
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id  TEXT NOT NULL,
                ts          INTEGER NOT NULL DEFAULT (unixepoch()),
                seq         INTEGER,
                kind        TEXT,
                name        TEXT,
                detail      TEXT,
                tokens      INTEGER,
                duration_ms INTEGER,
                outcome     TEXT
            )
            """
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_request ON steps(request_id)")
        # Eval results, one row per golden case per run (Pillar 01 — trend tracking).
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                ts          INTEGER NOT NULL DEFAULT (unixepoch()),
                case_id     TEXT,
                category    TEXT,
                model_id    TEXT,
                passed      INTEGER,
                latency_ms  INTEGER,
                tool_calls  TEXT,
                reasons     TEXT
            )
            """
        )
        # Idempotent migration for DBs created before these columns existed.
        _add_missing_columns(_conn, "interactions", {"model_calls": "INTEGER", "guardrail_hits": "INTEGER"})
        _conn.commit()
    return _conn


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, decl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


@dataclass
class Interaction:
    """One unit of work flowing through Loop. Created at the Slack edge with the
    request envelope; metrics are filled in from the agent result via `from_run`."""

    entrypoint: str  # app_mention | dm | slash
    team: str | None = None
    channel: str | None = None
    channel_type: str | None = None
    user: str | None = None
    thread_ts: str | None = None
    prompt: str = ""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # filled in after the agent runs
    model_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)
    model_calls: int = 0
    guardrail_hits: int = 0
    latency_ms: int = 0
    outcome: str = "ok"
    error: str | None = None

    def from_run(self, run: Any) -> None:
        """Copy metrics off an agent.AgentRun (duck-typed; no import needed)."""
        self.model_id = getattr(run, "model_id", None) or self.model_id
        self.input_tokens = int(getattr(run, "input_tokens", 0) or 0)
        self.output_tokens = int(getattr(run, "output_tokens", 0) or 0)
        self.total_tokens = int(getattr(run, "total_tokens", 0) or 0)
        self.tool_calls = list(getattr(run, "tool_calls", []) or [])
        self.model_calls = int(getattr(run, "model_calls", 0) or 0)
        self.guardrail_hits = int(getattr(run, "guardrail_hits", 0) or 0)


@contextmanager
def record(ix: Interaction) -> Iterator[Interaction]:
    """Time the wrapped call, mark outcome, and emit + persist on exit."""
    start = time.perf_counter()
    try:
        yield ix
    except Exception as err:  # noqa: BLE001
        ix.outcome = "error"
        ix.error = f"{type(err).__name__}: {err}"
        raise
    finally:
        ix.latency_ms = int((time.perf_counter() - start) * 1000)
        _emit(ix)


def _emit(ix: Interaction) -> None:
    payload = {
        "request_id": ix.request_id,
        "entrypoint": ix.entrypoint,
        "team": ix.team,
        "channel": ix.channel,
        "channel_type": ix.channel_type,
        "user": ix.user,
        "thread_ts": ix.thread_ts,
        "model_id": ix.model_id,
        "input_tokens": ix.input_tokens,
        "output_tokens": ix.output_tokens,
        "total_tokens": ix.total_tokens,
        "tool_calls": ix.tool_calls,
        "model_calls": ix.model_calls,
        "guardrail_hits": ix.guardrail_hits,
        "latency_ms": ix.latency_ms,
        "outcome": ix.outcome,
        "error": ix.error,
        "prompt_preview": ix.prompt[:120],
    }
    log.info("interaction %s", json.dumps(payload, ensure_ascii=False))
    try:
        _persist(ix)
    except Exception:  # noqa: BLE001 — telemetry must never break the request
        log.warning("could not persist interaction %s", ix.request_id, exc_info=True)


def _persist(ix: Interaction) -> None:
    with _lock:
        conn = _db()
        conn.execute(
            """
            INSERT INTO interactions (
                request_id, entrypoint, team, channel, channel_type, "user",
                thread_ts, prompt, model_id, input_tokens, output_tokens,
                total_tokens, tool_calls, model_calls, guardrail_hits,
                latency_ms, outcome, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ix.request_id, ix.entrypoint, ix.team, ix.channel, ix.channel_type,
                ix.user, ix.thread_ts, ix.prompt, ix.model_id, ix.input_tokens,
                ix.output_tokens, ix.total_tokens, json.dumps(ix.tool_calls),
                ix.model_calls, ix.guardrail_hits, ix.latency_ms, ix.outcome, ix.error,
            ),
        )
        conn.commit()


def record_step(
    request_id: str,
    seq: int,
    kind: str,
    name: str,
    *,
    detail: Any = None,
    tokens: int = 0,
    duration_ms: int = 0,
    outcome: str = "ok",
) -> None:
    """Persist one step of an interaction (model call / tool call / reasoning /
    guardrail). Logging of steps is the tracer's job; this only stores them.
    Telemetry must never break a request, so failures are swallowed + warned."""
    detail_str = None
    if detail is not None:
        detail_str = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, default=str)
    try:
        with _lock:
            conn = _db()
            conn.execute(
                """
                INSERT INTO steps (request_id, seq, kind, name, detail, tokens, duration_ms, outcome)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (request_id, seq, kind, name, detail_str, tokens, duration_ms, outcome),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("could not persist step %s/%s for %s", kind, name, request_id, exc_info=True)


def record_eval(
    run_id: str,
    case_id: str,
    category: str,
    model_id: str,
    passed: bool,
    latency_ms: int,
    tool_calls: list[str],
    reasons: list[str],
) -> None:
    """Persist one golden-case result so eval pass-rate can be tracked over time."""
    try:
        with _lock:
            conn = _db()
            conn.execute(
                """
                INSERT INTO eval_results
                    (run_id, case_id, category, model_id, passed, latency_ms, tool_calls, reasons)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, case_id, category, model_id, int(passed), latency_ms,
                    json.dumps(tool_calls), json.dumps(reasons),
                ),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.warning("could not persist eval result %s", case_id, exc_info=True)
