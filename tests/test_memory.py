"""Tests for Loop's episodic, provenance-stamped, FTS5-backed memory store.

Runnable two ways:
    python -m tests.test_memory       # plain runner, exits non-zero on failure
    pytest tests/test_memory.py       # if pytest is installed

No network, no API key, no model calls — pure storage-layer checks.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from loop import context as reqctx
from loop.agent import _format_memories
from loop.storage import SqliteMemoryStore, _fts_match
from strands.memory import InjectionFormatContext


def _store() -> SqliteMemoryStore:
    tmp = Path(tempfile.mkdtemp()) / "mem.db"
    return SqliteMemoryStore(db_path=str(tmp))


def test_fts_match_sanitizes() -> None:
    # punctuation / operators stripped to safe prefix-OR tokens
    assert _fts_match("when is our standup?") == "standup*"
    # "or" is a stopword, single chars dropped → injection-safe token OR
    assert _fts_match("DROP TABLE; OR 1=1 --") == "drop* OR table*"
    # stopwords-only → None so caller falls back to LIKE
    assert _fts_match("what is the") is None
    assert _fts_match("???") is None


def test_add_stamps_provenance_from_request() -> None:
    store = _store()
    state = reqctx.RequestState()
    state.author, state.channel, state.team, state.source = "U_ALICE", "C_INFRA", "T1", "rasputin"
    token = reqctx.set_current(state)
    try:
        rid = asyncio.run(store.add("We're going with Postgres as the primary database."))
    finally:
        reqctx.reset_current(token)
    assert rid > 0
    row = store._conn.execute(
        "SELECT author, channel, team, source FROM memory_entries WHERE id=?", (rid,)
    ).fetchone()
    assert row == ("U_ALICE", "C_INFRA", "T1", "rasputin"), row


def test_fts_recall_across_wording() -> None:
    store = _store()
    asyncio.run(store.add("Team standup is at 10am every weekday."))
    # query wording differs from stored wording — LIKE '%when is standup%' would miss
    hits = asyncio.run(store.search("when is our standup"))
    assert hits, "FTS should recall the standup memory"
    assert "10am" in hits[0].content


def test_search_returns_provenance_metadata() -> None:
    store = _store()
    state = reqctx.RequestState()
    state.author, state.channel = "U_BOB", "C_GENERAL"
    token = reqctx.set_current(state)
    try:
        asyncio.run(store.add("The Q3 launch date is September 12."))
    finally:
        reqctx.reset_current(token)
    hits = asyncio.run(store.search("launch date"))
    assert hits
    meta = hits[0].metadata or {}
    assert meta.get("author") == "U_BOB"
    assert meta.get("channel") == "C_GENERAL"
    assert "date" in meta  # ISO day auto-stamped


def test_like_fallback_when_fts_disabled() -> None:
    store = _store()
    store._fts = False  # simulate a SQLite build without FTS5
    asyncio.run(store.add("The deploy runbook lives in the #ops channel."))
    hits = asyncio.run(store.search("runbook"))
    assert hits and "runbook" in hits[0].content


def test_exact_content_dedup() -> None:
    store = _store()
    id1 = asyncio.run(store.add("Deploy window is Fridays 3-5pm."))
    id2 = asyncio.run(store.add("Deploy window is Fridays 3-5pm."))  # identical
    assert id1 == id2, "identical content should not create a second row"
    n = store._conn.execute("SELECT count(*) FROM memory_entries").fetchone()[0]
    assert n == 1, f"expected 1 row, found {n}"


def test_injection_format_surfaces_provenance() -> None:
    store = _store()
    state = reqctx.RequestState()
    state.author, state.channel = "U_ALICE", "C_INFRA"
    token = reqctx.set_current(state)
    try:
        asyncio.run(store.add("Decision: adopt Postgres."))
    finally:
        reqctx.reset_current(token)
    hits = asyncio.run(store.search("postgres"))
    rendered = _format_memories(InjectionFormatContext(entries=hits))
    assert "<memory>" in rendered
    assert 'from="@U_ALICE"' in rendered
    assert 'in="#C_INFRA"' in rendered
    assert "Postgres" in rendered


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
