"""Tests for the experimental libSQL/Turso vector memory store.

Skips cleanly if `libsql-experimental` isn't installed (it's an optional extra:
`pip install "loop[vector]"`). Uses the dependency-free HashingEmbedder so the
suite needs no model download, no network, and no API key.

    python -m tests.test_vector_memory
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

try:
    import libsql_experimental  # noqa: F401
    HAVE_LIBSQL = True
except ImportError:
    HAVE_LIBSQL = False

from loop import context as reqctx


def _store():
    from loop.embeddings import HashingEmbedder
    from loop.vector_store import LibsqlVectorMemoryStore

    db = Path(tempfile.mkdtemp()) / "vec.db"
    return LibsqlVectorMemoryStore(db_path=str(db), embedder=HashingEmbedder(dim=256))


def test_vector_roundtrip_and_ranking() -> None:
    if not HAVE_LIBSQL:
        print("  SKIP  libsql-experimental not installed")
        return
    store = _store()
    state = reqctx.RequestState()
    state.author, state.channel = "U_ALICE", "C_INFRA"
    token = reqctx.set_current(state)
    try:
        asyncio.run(store.add("The on-call deploy runbook lives in Notion under SRE."))
        asyncio.run(store.add("Team standup is at 10am every weekday."))
        asyncio.run(store.add("We chose Postgres as the primary database."))
    finally:
        reqctx.reset_current(token)

    hits = asyncio.run(store.search("where is the deploy runbook", {"max_search_results": 2}))
    assert hits, "vector search returned nothing"
    assert "runbook" in hits[0].content.lower()
    meta = hits[0].metadata or {}
    assert meta.get("author") == "U_ALICE"
    assert meta.get("channel") == "C_INFRA"
    assert "date" in meta


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
    print(f"\n{len(tests) - failures}/{len(tests)} passed (libsql={'yes' if HAVE_LIBSQL else 'no'})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
