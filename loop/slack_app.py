"""Slack Bolt app(s).

Loop can run as one OR several Slack apps in a single process (see `_discover_apps`
+ `start`): each configured app gets its own Bolt App, Socket Mode connection, and
agent persona, but they all share the same code, memory store, and telemetry. So
`@Loop` and `@Rasputin_Loop` can coexist and even behave differently.

Each app has three handlers — all route straight to that app's Strands agent:
  - `app_mention` : user @mentions Loop in a channel → run() → reply in thread
  - `message`     : tracks every message Loop can see (channel id/type/user in
                    the logs); answers it ONLY when it's a direct message (DM).
                    This is also what silences Bolt's "unhandled request" 404s
                    for `message` events.
  - `/loop`       : slash command → run() → respond

No passive ingestion in channels, no intent parsing, no command templates. The
agent is the only brain. Every interaction is wrapped in `observability.record`
so we capture request id, channel, user, model, tokens, tool calls, latency, and
outcome (one JSON log line + a row in the `interactions` table).

The `<@U...>` mention strip is purely text-cleaning (so the agent doesn't see its
own name); it is not parsing intent or routing commands.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import urllib.request
from dataclasses import dataclass

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from loop import observability as obs
from loop.agent import run as run_agent

log = logging.getLogger("loop.slack")

_MENTION_RE = re.compile(r"<@[^>]+>")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text).strip()


# File attachments: which mimetypes we hand to the model, and a size cap. Images
# go to the model natively (MiniMax M3 is multimodal); text/code/json files are
# inlined as text; PDFs are extracted to text (pypdf) by the agent.
_FILE_ALLOWED = ("image/", "text/", "application/json", "application/pdf")
_FILE_MAX_BYTES = int(os.environ.get("LOOP_MAX_FILE_BYTES", str(8 * 1024 * 1024)) or 0)


def _download(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted Slack URL)
        return resp.read()


def _collect_files(event: dict, client) -> list[dict]:
    """Download eligible Slack attachments on this event into {name, mimetype, bytes}.

    Slack file URLs are private — they must be fetched with the bot token in an
    Authorization header (a plain GET returns a login page), which is why this
    needs the `files:read` scope. Oversized / disallowed types are skipped, not
    fatal. Returns [] when there are no files (the common case).
    """
    out: list[dict] = []
    for f in event.get("files") or []:
        name, mt, size = f.get("name") or "file", f.get("mimetype") or "", int(f.get("size") or 0)
        if not mt.startswith(_FILE_ALLOWED):
            log.info("file skipped (type) name=%s mimetype=%s", name, mt)
            continue
        if _FILE_MAX_BYTES and size > _FILE_MAX_BYTES:
            log.info("file skipped (size) name=%s bytes=%d cap=%d", name, size, _FILE_MAX_BYTES)
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        try:
            data = _download(url, client.token)
        except Exception:  # noqa: BLE001
            log.exception("file download failed name=%s", name)
            continue
        log.info("file attached name=%s mimetype=%s bytes=%d", name, mt, len(data))
        out.append({"name": name, "mimetype": mt, "bytes": data})
    return out


def _answer(ix: obs.Interaction, event_ctx: dict, say, client, logger, attachments=None, app_name=None) -> None:
    """Shared path for mention + DM: react, run the agent under telemetry, reply."""
    channel, thread_ts = ix.channel, ix.thread_ts
    _react(client, channel, thread_ts, "eyes")
    try:
        with obs.record(ix):
            run = run_agent(
                ix.prompt, attachments=attachments, context=event_ctx,
                request_id=ix.request_id, app_name=app_name,
            )
            ix.from_run(run)
            say(text=run.text or "_(no response)_", thread_ts=thread_ts)
        _swap(client, channel, thread_ts, "white_check_mark")
    except Exception as err:  # noqa: BLE001
        logger.exception("agent invocation failed rid=%s", ix.request_id)
        _swap(client, channel, thread_ts, "warning")
        say(text=f":warning: Something went wrong: {err}", thread_ts=thread_ts)


@dataclass
class AppConfig:
    """One Slack app Loop runs as: a short name + its bot/app token pair."""

    name: str
    bot_token: str
    app_token: str


def _discover_apps() -> list[AppConfig]:
    """Find every configured Slack app from the environment.

    Two conventions, both supported so multiple apps coexist:
      • Named pairs (preferred):  SLACK_BOT_TOKEN_<NAME> + SLACK_APP_TOKEN_<NAME>
          e.g. SLACK_BOT_TOKEN_LOOP/SLACK_APP_TOKEN_LOOP and
               SLACK_BOT_TOKEN_RASPUTIN/SLACK_APP_TOKEN_RASPUTIN  → two apps.
      • Bare pair (back-compat):  SLACK_BOT_TOKEN + SLACK_APP_TOKEN  → one app,
          named by SLACK_APP_NAME (default "loop").
    Each app gets its own Bolt App + Socket Mode connection in `start()`, and its
    own agent persona (LOOP_PERSONA_<NAME>).
    """
    pairs: dict[str, dict[str, str]] = {}
    for key, val in os.environ.items():
        if not val:
            continue
        if key.startswith("SLACK_BOT_TOKEN_"):
            pairs.setdefault(key[len("SLACK_BOT_TOKEN_"):].lower(), {})["bot"] = val
        elif key.startswith("SLACK_APP_TOKEN_"):
            pairs.setdefault(key[len("SLACK_APP_TOKEN_"):].lower(), {})["app"] = val
    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_APP_TOKEN"):
        name = (os.environ.get("SLACK_APP_NAME") or "loop").lower()
        slot = pairs.setdefault(name, {})
        slot.setdefault("bot", os.environ["SLACK_BOT_TOKEN"])
        slot.setdefault("app", os.environ["SLACK_APP_TOKEN"])

    configs: list[AppConfig] = []
    for name, toks in pairs.items():
        if toks.get("bot") and toks.get("app"):
            configs.append(AppConfig(name=name, bot_token=toks["bot"], app_token=toks["app"]))
        else:
            missing = "app token" if toks.get("bot") else "bot token"
            log.warning("slack app %r missing its %s — skipping", name, missing)
    return configs


def _build_app(cfg: AppConfig) -> App:
    app = App(token=cfg.bot_token)

    @app.event("app_mention")
    def on_mention(event, say, client, logger):
        attachments = _collect_files(event, client)
        text = _strip_mentions(event.get("text", "") or "")
        if not text:
            text = "Please look at the attached file(s)." if attachments else "hello"
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:app_mention",
            team=event.get("team"),
            channel=event["channel"],
            channel_type=event.get("channel_type"),
            user=event.get("user"),
            thread_ts=event.get("ts"),
            prompt=text,
        )
        log.info(
            "[%s] mention rid=%s channel=%s user=%s thread=%s files=%d",
            cfg.name, ix.request_id, ix.channel, ix.user, ix.thread_ts, len(attachments),
        )
        _answer(ix, _ctx(event), say, client, logger, attachments=attachments, app_name=cfg.name)

    @app.event("message")
    def on_message(event, say, client, logger):
        # Tracking + DM support. We log EVERY message Loop receives so each
        # channel it can see is visible in the logs, but we only *answer* DMs.
        # Channel messages stay observe-only — @mention / /loop are how you
        # invoke Loop in a channel. (Registering this handler is also what stops
        # Bolt's 404 "unhandled request" warnings for `message` events.)
        if event.get("bot_id"):
            return  # ignore our own messages (avoids loops)
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return  # ignore edits/joins/etc., but KEEP file uploads (file_share)
        channel = event.get("channel")
        channel_type = event.get("channel_type")
        user = event.get("user")
        raw = event.get("text", "") or ""
        text = _strip_mentions(raw)
        log.info(
            "[%s] message rid=- channel=%s type=%s user=%s preview=%r",
            cfg.name, channel, channel_type, user, text[:80],
        )
        if channel_type != "im":
            return  # observe-only (channel chatter) — not a DM to Loop
        if "<@" in raw:
            return  # a mention inside a DM: let the app_mention handler take it
        attachments = _collect_files(event, client)
        if not text and not attachments:
            return  # empty DM noise
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:dm",
            team=event.get("team"),
            channel=channel,
            channel_type=channel_type,
            user=user,
            thread_ts=event.get("ts"),
            prompt=text or "(see attached file)",
        )
        log.info("[%s] dm rid=%s channel=%s user=%s files=%d", cfg.name, ix.request_id, channel, user, len(attachments))
        _answer(ix, _ctx(event), say, client, logger, attachments=attachments, app_name=cfg.name)

    @app.command("/loop")
    def on_loop(ack, respond, command):
        ack()
        text = (command.get("text") or "").strip() or "Give me a digest of recent memory."
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:slash",
            team=command.get("team_id"),
            channel=command.get("channel_id"),
            channel_type="slash",
            user=command.get("user_id"),
            prompt=text,
        )
        log.info("[%s] slash rid=%s channel=%s user=%s", cfg.name, ix.request_id, ix.channel, ix.user)
        try:
            with obs.record(ix):
                run = run_agent(text, context=_ctx_cmd(command), request_id=ix.request_id, app_name=cfg.name)
                ix.from_run(run)
                respond(run.text or "_(no response)_")
        except Exception as err:  # noqa: BLE001
            log.exception("[%s] slash command failed rid=%s", cfg.name, ix.request_id)
            respond(f":warning: Something went wrong: {err}")

    if _assistant_enabled():
        _attach_assistant(app, cfg)
    return app


def _ctx(event: dict) -> dict:
    return {
        "user": event.get("user"),
        "channel": event.get("channel"),
        "channel_type": event.get("channel_type"),
        "team": event.get("team"),
        "thread_ts": event.get("thread_ts") or event.get("ts"),
    }


def _ctx_cmd(command: dict) -> dict:
    return {
        "user": command.get("user_id"),
        "channel": command.get("channel_id"),
        "channel_type": "slash",
        "team": command.get("team_id"),
    }


# ── Native Slack AI "Assistant" surface (Slack AI capabilities) ───────────────
# Opt-in via LOOP_SLACK_ASSISTANT=on. Requires, on the Slack app: the "Agents & AI
# Apps" feature enabled, the `assistant:write` scope, and the `assistant_thread_*`
# events subscribed. Routes the same single Strands agent into the assistant pane
# (side panel / app DM) with suggested prompts + a live "is thinking…" status.
_SUGGESTED_PROMPTS = [
    {"title": "Recall a decision", "message": "What did we decide about our database?"},
    {"title": "Summarize this channel", "message": "Summarize the latest messages in this channel."},
    {"title": "Remember something", "message": "Remember that our standup is at 10am every weekday."},
]


def _assistant_enabled() -> bool:
    return os.environ.get("LOOP_SLACK_ASSISTANT", "off").strip().lower() in {"on", "1", "true", "yes"}


def _attach_assistant(app: "App", cfg: "AppConfig") -> None:
    """Wire Bolt's Assistant (AI app pane) to the same agent. Best-effort: if this
    slack_bolt build lacks the Assistant class, log and skip rather than crash."""
    try:
        from slack_bolt import Assistant
    except Exception:  # noqa: BLE001 — older bolt without Assistant
        log.warning("[%s] slack_bolt has no Assistant class; assistant disabled", cfg.name)
        return

    assistant = Assistant()

    @assistant.thread_started
    def _started(say, set_suggested_prompts, logger):
        say(":wave: Hi! I'm *Loop*. Ask me to recall a decision, summarize a channel, or remember something.")
        try:
            set_suggested_prompts(prompts=_SUGGESTED_PROMPTS, title="Try one of these:")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] set_suggested_prompts failed", cfg.name)

    @assistant.user_message
    def _on_message(payload, client, set_status, say, logger, context):
        text = _strip_mentions(payload.get("text", "") or "")
        attachments = _collect_files(payload, client)
        if not text and not attachments:
            return
        try:
            set_status("is thinking…")
        except Exception:  # noqa: BLE001
            pass
        ix = obs.Interaction(
            entrypoint=f"{cfg.name}:assistant",
            team=context.get("team_id") or payload.get("team"),
            channel=payload.get("channel"),
            channel_type="assistant",
            user=payload.get("user"),
            thread_ts=payload.get("thread_ts"),
            prompt=text or "(see attached file)",
        )
        log.info("[%s] assistant rid=%s channel=%s user=%s files=%d",
                 cfg.name, ix.request_id, ix.channel, ix.user, len(attachments))
        try:
            with obs.record(ix):
                run = run_agent(
                    ix.prompt, attachments=attachments,
                    context={
                        "user": ix.user, "channel": ix.channel, "channel_type": "assistant",
                        "team": ix.team, "thread_ts": ix.thread_ts,
                    },
                    request_id=ix.request_id, app_name=cfg.name,
                )
                ix.from_run(run)
                say(text=run.text or "_(no response)_")
        except Exception as err:  # noqa: BLE001
            logger.exception("[%s] assistant turn failed rid=%s", cfg.name, ix.request_id)
            say(text=f":warning: Something went wrong: {err}")

    app.assistant(assistant)
    log.info("[%s] Slack AI Assistant attached (needs assistant:write + Agents & AI Apps)", cfg.name)


def _react(client, channel: str, ts: str, name: str) -> None:
    try:
        client.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception:  # noqa: BLE001
        pass


def _swap(client, channel: str, ts: str, name: str) -> None:
    try:
        client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
    except Exception:  # noqa: BLE001
        pass
    _react(client, channel, ts, name)


def start() -> None:  # pragma: no cover
    configs = _discover_apps()
    if not configs:
        raise RuntimeError(
            "No Slack apps configured. Set SLACK_BOT_TOKEN + SLACK_APP_TOKEN, and/or "
            "SLACK_BOT_TOKEN_<NAME> + SLACK_APP_TOKEN_<NAME> pairs."
        )
    for cfg in configs:
        handler = SocketModeHandler(_build_app(cfg), cfg.app_token)
        handler.connect()  # non-blocking: opens this app's socket on a background thread
        log.info("loop: Slack app %r connected (socket mode)", cfg.name)
    log.info("loop: %d app(s) running: %s", len(configs), ", ".join(c.name for c in configs))
    # Ambient (proactive PM) mode — off unless LOOP_AMBIENT=on; one daemon per app.
    from loop import ambient  # noqa: PLC0415 (optional path; keeps import cost off the default)

    ambient.maybe_start(configs)
    threading.Event().wait()  # block forever; each app runs in its own background thread
