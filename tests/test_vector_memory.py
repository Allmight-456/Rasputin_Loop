"""Tests for the hybrid (lexical + semantic) libSQL/Turso memory store.

Skips libSQL/fastembed cases cleanly when those optional extras aren't installed
(`pip install "loop[vector]"`). The RRF unit test always runs.

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

try:
    import fastembed  # noqa: F401
    HAVE_FASTEMBED = True
except ImportError:
    HAVE_FASTEMBED = False

from loop import context as reqctx


def _store(embedder=None):
    from loop.embeddings import HashingEmbedder
    from loop.vector_store import HybridMemoryStore

    db = Path(tempfile.mkdtemp()) / "vec.db"
    return HybridMemoryStore(db_path=str(db), embedder=embedder or HashingEmbedder(dim=256))


def test_rrf_fuse_orders_by_combined_rank() -> None:
    from loop.vector_store import _rrf_fuse

    lexical = [10, 20, 30]   # 20 is #2 here
    semantic = [40, 20, 50]  # 20 is #2 here too → strong on both → should win
    fused = _rrf_fuse(lexical, semantic)
    assert fused[0] == 20, fused
    assert set(fused) == {10, 20, 30, 40, 50}


def test_hybrid_roundtrip_provenance_and_dedup() -> None:
    if not HAVE_LIBSQL:
        print("  SKIP  libsql-experimental not installed")
        return
    store = _store()
    state = reqctx.RequestState()
    state.author, state.channel = "U_ALICE", "C_INFRA"
    token = reqctx.set_current(state)
    try:
        asyncio.run(store.add("We chose Postgres as the primary database."))
        id_a = asyncio.run(store.add("Team standup is at 10am every weekday."))
        id_dup = asyncio.run(store.add("Team standup is at 10am every weekday."))  # exact dup
    finally:
        reqctx.reset_current(token)
    assert id_a == id_dup, "exact-content dedup should return the existing id"

    hits = asyncio.run(store.search("when is standup", {"max_search_results": 2}))
    assert hits and "10am" in hits[0].content
    meta = hits[0].metadata or {}
    assert meta.get("author") == "U_ALICE" and meta.get("channel") == "C_INFRA"
    assert "date" in meta


def test_hybrid_semantic_recall_no_shared_words() -> None:
    """The semantic half should recall across wording with little lexical overlap."""
    if not (HAVE_LIBSQL and HAVE_FASTEMBED):
        print("  SKIP  needs libsql + fastembed (pip install 'loop[vector]')")
        return
    from loop.embeddings import FastEmbedEmbedder

    store = _store(embedder=FastEmbedEmbedder())
    asyncio.run(store.add("The on-call playbook lives in Notion under SRE."))
    asyncio.run(store.add("Lunch options near the office are pretty limited."))
    # FTS alone would rank lunch ~equally; semantics should pull the playbook up.
    hits = asyncio.run(store.search("where do we keep the incident runbook", {"max_search_results": 1}))
    assert hits, "hybrid returned nothing"
    assert "playbook" in hits[0].content.lower(), hits[0].content


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
    print(f"\n{len(tests) - failures}/{len(tests)} passed "
          f"(libsql={'y' if HAVE_LIBSQL else 'n'}, fastembed={'y' if HAVE_FASTEMBED else 'n'})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
