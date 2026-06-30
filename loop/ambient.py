"""Ambient (background) mode — the proactive project-manager teammate.

Off by default (`LOOP_AMBIENT=off`). When on, each Slack app gets a daemon thread
that periodically looks for threads which have **gone quiet without resolution**
and, if a nudge is warranted, posts a concise follow-up — like a PM keeping work
moving. This is Loop's take on Claude Tag's "ambient mode", but provider-agnostic
and able to run its background scanning on a cheaper model (`LOOP_AMBIENT_MODEL`).

Design (deliberately minimal — no new brain, no intent parser):
  • **Discovery is pluggable and starts narrow.** The default finds candidate
    threads from the `interactions` table — threads *this app already participated
    in* whose last genuine (non-ambient) activity is older than the stale window.
    No new Slack scopes, no full-channel surveillance. The enterprise track can
    swap in a live `conversations_history` channel scan by replacing one function.
  • **Live staleness re-check.** For each candidate we read the actual thread
    (`conversations_replies`) and only proceed if its newest message really is
    older than the stale window.
  • **Re-nudge guard.** We never nudge the same thread twice inside
    `LOOP_AMBIENT_RENUDGE_HOURS` (checked against our own `:ambient` interactions).
  • **Right identity.** We post through the per-app Bolt/Web client
    (`cfg.bot_token`), not the `slack_send_message` tool (which always uses the
    bare `SLACK_BOT_TOKEN`).
  • **Same telemetry.** Each scan is wrapped in `observability.record` with
    entrypoint `"<app>:ambient"`, so background activity is auditable and feeds
    future discovery + the re-nudge guard.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable

from slack_sdk import WebClient

from loop import observability as obs
from loop.agent import run as run_agent
from loop.storage import DEFAULT_DB_PATH

log = logging.getLogger("loop.ambient")

# A discovery function: given an app name + a Slack Web client, return candidate
# (channel, thread_ts, team) tuples to consider nudging. Two implementations ship
# (interactions / full-channel scan); selected by LOOP_AMBIENT_DISCOVERY.
Discoverer = Callable[[str, Any], "list[tuple[str, str, str | None]]"]


def _on(val: str | None) -> bool:
    return (val or "").strip().lower() in {"on", "1", "true", "yes"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def enabled() -> bool:
    return _on(os.environ.get("LOOP_AMBIENT", "off"))


def _channels_allowlist() -> set[str]:
    raw = os.environ.get("LOOP_AMBIENT_CHANNELS", "") or ""
    return {c.strip() for c in raw.split(",") if c.strip()}


# ── discovery (narrow default: threads this app already touched) ──────────────
def discover_from_interactions(app_name: str, client: Any = None) -> list[tuple[str, str, str | None]]:
    """Candidate stale threads from the telemetry DB: threads `app_name` handled
    whose last *genuine* (non-ambient) interaction is older than the stale window.
    `client` is unused here (kept for the Discoverer signature)."""
    stale_hours = _int_env("LOOP_AMBIENT_STALE_HOURS", 24)
    max_threads = _int_env("LOOP_AMBIENT_MAX_THREADS", 3)
    cutoff = int(time.time()) - stale_hours * 3600
    path = os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
    allow = _channels_allowlist()
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
        try:
            rows = conn.execute(
                """
                SELECT channel, thread_ts, MAX(ts) AS last_ts, MAX(team) AS team
                FROM interactions
                WHERE thread_ts IS NOT NULL AND channel IS NOT NULL
                  AND entrypoint LIKE ? AND entrypoint NOT LIKE '%:ambient'
                GROUP BY channel, thread_ts
                HAVING last_ts < ?
                ORDER BY last_ts ASC
                LIMIT ?
                """,
                (f"{app_name}:%", cutoff, max_threads * 4),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []  # interactions table not created yet (no traffic)
    out = [(r[0], r[1], r[3]) for r in rows if not allow or r[0] in allow]
    return out[:max_threads]


# ── discovery (full-channel scan: any stalled thread, not just ones we touched) ─
_team_cache: dict[int, str | None] = {}


def _team_id(client: Any) -> str | None:
    key = id(client)
    if key not in _team_cache:
        try:
            _team_cache[key] = client.auth_test().get("team_id")
        except Exception:  # noqa: BLE001
            _team_cache[key] = None
    return _team_cache[key]


def _bot_channels(client: Any) -> list[str]:
    """Channels the bot is a member of (paginated). Needs channels:read /
    groups:read. Prefer LOOP_AMBIENT_CHANNELS to avoid this call when you can."""
    out: list[str] = []
    cursor: str | None = None
    try:
        while True:
            resp = client.users_conversations(
                types="public_channel,private_channel", exclude_archived=True, limit=200, cursor=cursor,
            )
            out += [c["id"] for c in resp.get("channels", [])]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as err:  # noqa: BLE001
        log.warning("users_conversations failed (need channels:read?): %s", err)
    return out


def discover_from_channels(app_name: str, client: Any) -> list[tuple[str, str, str | None]]:
    """Full-channel scan: scan recent history across the channels the bot is in for
    thread roots whose own timestamp is older than the stale window. `_process_thread`
    then re-checks true staleness against the whole thread and the re-nudge guard, so
    active threads (recent replies) are dropped. Needs channels:history (+
    groups:history for private channels) and, without an allow-list, channels:read."""
    stale_hours = _int_env("LOOP_AMBIENT_STALE_HOURS", 24)
    max_threads = _int_env("LOOP_AMBIENT_MAX_THREADS", 3)
    hist_limit = _int_env("LOOP_AMBIENT_HISTORY_LIMIT", 50)
    cutoff = time.time() - stale_hours * 3600
    allow = _channels_allowlist()
    channels = list(allow) if allow else _bot_channels(client)
    team = _team_id(client)
    out: list[tuple[str, str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for ch in channels:
        try:
            resp = client.conversations_history(channel=ch, limit=hist_limit)
        except Exception as err:  # noqa: BLE001
            log.warning("[%s] conversations_history failed channel=%s: %s", app_name, ch, err)
            continue
        for m in resp.get("messages") or []:
            if m.get("subtype") or m.get("bot_id"):
                continue  # joins / system / bot posts aren't tasks
            try:
                if float(m.get("ts", 0) or 0) >= cutoff:
                    continue  # too recent to be "stale"
            except ValueError:
                continue
            root = m.get("thread_ts") or m.get("ts")
            key = (ch, root)
            if key in seen:
                continue
            seen.add(key)
            out.append((ch, root, team))
            if len(out) >= max_threads:
                return out
    return out


def _select_discoverer() -> Discoverer:
    mode = (os.environ.get("LOOP_AMBIENT_DISCOVERY", "interactions") or "interactions").strip().lower()
    if mode in {"channels", "channel", "full", "scan", "channel-scan"}:
        return discover_from_channels
    return discover_from_interactions


# ── per-thread processing ─────────────────────────────────────────────────────
def _recently_nudged(app_name: str, channel: str, thread_ts: str) -> bool:
    window_h = _int_env("LOOP_AMBIENT_RENUDGE_HOURS", 48)
    since = int(time.time()) - window_h * 3600
    path = os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
        try:
            row = conn.execute(
                "SELECT 1 FROM interactions WHERE channel=? AND thread_ts=? "
                "AND entrypoint=? AND ts > ? LIMIT 1",
                (channel, thread_ts, f"{app_name}:ambient", since),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except sqlite3.OperationalError:
        return False


def _fetch_thread(client: WebClient, channel: str, thread_ts: str) -> list[dict]:
    limit = _int_env("LOOP_AMBIENT_REPLIES_LIMIT", 20)
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        return list(resp.get("messages") or [])
    except Exception as err:  # noqa: BLE001
        log.warning("[%s] conversations_replies failed channel=%s ts=%s: %s", "ambient", channel, thread_ts, err)
        return []


def _is_stale(messages: list[dict]) -> bool:
    """True if the newest message in the thread is older than the stale window."""
    if not messages:
        return False
    stale_seconds = _int_env("LOOP_AMBIENT_STALE_HOURS", 24) * 3600
    try:
        newest = max(float(m.get("ts", 0) or 0) for m in messages)
    except ValueError:
        return False
    return (time.time() - newest) >= stale_seconds


def _transcript(messages: list[dict]) -> str:
    lines: list[str] = []
    for m in messages[-15:]:
        who = m.get("user") or m.get("bot_id") or "unknown"
        text = (m.get("text") or "").strip().replace("\n", " ")
        if text:
            lines.append(f"<@{who}>: {text[:300]}")
    return "\n".join(lines)


_PROMPT = """You are acting in AMBIENT (background) mode as a proactive project-manager teammate.
A thread in channel <#{channel}> has had no activity for over {hours}h and may be an unresolved or forgotten task.

Thread so far (oldest first):
{transcript}

Decide whether a brief, helpful nudge is warranted — e.g. an open question nobody answered, a decision left unmade, a task with no owner or next step, or a blocked item.
- If YES: write ONE concise Slack mrkdwn message (under ~600 chars) that moves it forward: summarize the open point and propose a concrete next step or ask who owns it. Do not invent facts not in the thread.
- If NO (small talk, already resolved, or nothing actionable): reply with exactly NOOP and nothing else.
Do not call any tools. Do not save anything to memory."""


def _process_thread(cfg_name: str, client: WebClient, channel: str, thread_ts: str, team: str | None) -> None:
    if _recently_nudged(cfg_name, channel, thread_ts):
        return
    messages = _fetch_thread(client, channel, thread_ts)
    if not _is_stale(messages):
        return
    transcript = _transcript(messages)
    if not transcript:
        return

    hours = _int_env("LOOP_AMBIENT_STALE_HOURS", 24)
    prompt = _PROMPT.format(channel=channel, hours=hours, transcript=transcript)
    ix = obs.Interaction(
        entrypoint=f"{cfg_name}:ambient",
        team=team,
        channel=channel,
        channel_type="ambient",
        thread_ts=thread_ts,
        prompt="(ambient scan)",
    )
    ambient_model = os.environ.get("LOOP_AMBIENT_MODEL") or None
    try:
        with obs.record(ix):
            run = run_agent(
                prompt,
                context={"channel": channel, "channel_type": "ambient", "team": team, "thread_ts": thread_ts},
                request_id=ix.request_id,
                app_name=cfg_name,
                model_id=ambient_model,
            )
            ix.from_run(run)
            text = (run.text or "").strip()
            if text and text.upper() != "NOOP" and not text.upper().startswith("NOOP"):
                client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
                log.info("[%s] ambient nudge posted channel=%s thread=%s", cfg_name, channel, thread_ts)
            else:
                log.info("[%s] ambient NOOP channel=%s thread=%s", cfg_name, channel, thread_ts)
    except Exception:  # noqa: BLE001 — a bad thread must not kill the loop
        log.exception("[%s] ambient processing failed channel=%s thread=%s", cfg_name, channel, thread_ts)


def _tick(cfg_name: str, client: WebClient, discover: Discoverer) -> None:
    for channel, thread_ts, team in discover(cfg_name, client):
        _process_thread(cfg_name, client, channel, thread_ts, team)


def _loop(cfg_name: str, bot_token: str, discover: Discoverer) -> None:  # pragma: no cover (daemon)
    client = WebClient(token=bot_token)
    interval = _int_env("LOOP_AMBIENT_INTERVAL_SEC", 900)
    log.info("[%s] ambient loop started (interval=%ds)", cfg_name, interval)
    # Stagger the first scan so startup isn't noisy.
    time.sleep(min(interval, 60))
    while True:
        try:
            _tick(cfg_name, client, discover)
        except Exception:  # noqa: BLE001
            log.exception("[%s] ambient tick failed", cfg_name)
        time.sleep(interval)


def maybe_start(configs: list[Any], discover: Discoverer | None = None) -> None:
    """Spawn one daemon ambient loop per app, if LOOP_AMBIENT is on. `configs` is
    a list of AppConfig (name + bot_token). The discoverer is chosen by
    LOOP_AMBIENT_DISCOVERY (interactions default | channels) unless one is passed.
    No-op when disabled."""
    if not enabled():
        return
    discover = discover or _select_discoverer()
    log.info("ambient discovery mode: %s", discover.__name__)
    for cfg in configs:
        t = threading.Thread(
            target=_loop,
            args=(cfg.name, cfg.bot_token, discover),
            name=f"ambient-{cfg.name}",
            daemon=True,
        )
        t.start()
    log.info("ambient mode ON for %d app(s): %s", len(configs), ", ".join(c.name for c in configs))
