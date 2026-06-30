"""Tests for Loop's ambient (proactive PM) mode — discovery, staleness, posting.

Runnable two ways:
    python -m tests.test_ambient
    pytest tests/test_ambient.py

No network, no model calls — Slack client + agent run are stubbed; discovery uses
a throwaway telemetry DB.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def _fresh_db() -> str:
    """Point telemetry at a clean DB and force observability to re-open it."""
    import loop.observability as obs

    path = str(Path(tempfile.mkdtemp()) / "obs.db")
    os.environ["DATABASE_PATH"] = path
    obs._conn = None  # drop any cached connection so _db() reopens at the new path
    obs._db()         # create the interactions/steps/eval_results schema
    return path


def _seed_interaction(entrypoint: str, channel: str, thread_ts: str, ts: int, team: str = "T1") -> None:
    import loop.observability as obs

    obs._db().execute(
        'INSERT INTO interactions (request_id, ts, entrypoint, team, channel, thread_ts, prompt) '
        'VALUES (?,?,?,?,?,?,?)',
        ("r" + str(ts), ts, entrypoint, team, channel, thread_ts, "x"),
    )
    obs._db().commit()


class _FakeRun:
    def __init__(self, text: str) -> None:
        self.text = text
        self.model_id = "fake"
    input_tokens = output_tokens = total_tokens = model_calls = guardrail_hits = 0
    tool_calls: list = []


class _FakeClient:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self._messages = messages or []
        self.posted: list[tuple] = []

    def conversations_replies(self, channel, ts, limit):
        return {"messages": self._messages}

    def conversations_history(self, channel, limit):
        return {"messages": self._messages}

    def chat_postMessage(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))
        return {"ok": True}

    def auth_test(self):
        return {"team_id": "T_WS"}

    def users_conversations(self, **kw):
        return {"channels": [{"id": "C1"}]}


def test_is_stale() -> None:
    import loop.ambient as amb

    os.environ["LOOP_AMBIENT_STALE_HOURS"] = "24"
    now = time.time()
    assert amb._is_stale([{"ts": str(now - 30 * 3600)}]) is True
    assert amb._is_stale([{"ts": str(now - 3600)}]) is False
    assert amb._is_stale([]) is False


def test_discover_from_interactions_finds_stale_only() -> None:
    import loop.ambient as amb

    _fresh_db()
    now = int(time.time())
    _seed_interaction("rasputin:app_mention", "C_STALE", "111.1", now - 30 * 3600)
    _seed_interaction("rasputin:dm", "C_FRESH", "222.2", now - 3600)
    _seed_interaction("loop:app_mention", "C_OTHER", "333.3", now - 30 * 3600)  # different app
    cands = amb.discover_from_interactions("rasputin")
    assert cands == [("C_STALE", "111.1", "T1")], cands


def test_discover_from_channels_skips_fresh_and_bots() -> None:
    import loop.ambient as amb

    os.environ["LOOP_AMBIENT_STALE_HOURS"] = "24"
    os.environ["LOOP_AMBIENT_MAX_THREADS"] = "5"
    os.environ.pop("LOOP_AMBIENT_CHANNELS", None)
    now = time.time()
    old, fresh = str(now - 30 * 3600), str(now - 3600)
    client = _FakeClient([
        {"ts": old, "text": "who owns the migration?"},          # stale root → candidate
        {"ts": fresh, "text": "recent chatter"},                  # too recent → skip
        {"ts": old, "subtype": "channel_join", "text": "joined"}, # system → skip
        {"ts": old, "bot_id": "B1", "text": "bot post"},          # bot → skip
    ])
    cands = amb.discover_from_channels("rasputin", client)
    assert cands == [("C1", old, "T_WS")], cands


def test_process_thread_posts_then_renudge_guard(monkeypatch=None) -> None:
    import loop.ambient as amb

    _fresh_db()
    os.environ["LOOP_AMBIENT_STALE_HOURS"] = "24"
    now = time.time()
    msgs = [{"user": "U1", "text": "who owns the migration?", "ts": str(now - 30 * 3600)}]
    client = _FakeClient(msgs)
    amb.run_agent = lambda *a, **k: _FakeRun("Following up — who owns the migration? Next step: assign by EOD.")

    amb._process_thread("rasputin", client, "C_STALE", "111.1", "T1")
    assert len(client.posted) == 1, "should post one nudge"
    # second pass on the same thread is suppressed by the re-nudge guard
    amb._process_thread("rasputin", client, "C_STALE", "111.1", "T1")
    assert len(client.posted) == 1, "re-nudge guard should prevent a second post"


def test_process_thread_noop_does_not_post() -> None:
    import loop.ambient as amb

    _fresh_db()
    os.environ["LOOP_AMBIENT_STALE_HOURS"] = "24"
    now = time.time()
    client = _FakeClient([{"user": "U1", "text": "ok thanks all, resolved!", "ts": str(now - 30 * 3600)}])
    amb.run_agent = lambda *a, **k: _FakeRun("NOOP")
    amb._process_thread("rasputin", client, "C_DONE", "999.9", "T1")
    assert client.posted == [], "NOOP must not post"


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
