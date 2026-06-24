# Native Slack AI "Assistant" surface (experimental)

> **Branch:** `feat/slack-assistant`. Off `main` because it can't be verified
> without the Slack-side "Agents & AI Apps" toggle on a real workspace. The code
> is gated behind `LOOP_SLACK_ASSISTANT=on` and never affects the default
> mention/DM/slash flow.

## Why this exists (hackathon)

The hackathon requires using at least one of: **Slack AI capabilities**, **MCP
server integration**, or **Real-Time Search API**. Loop already has **MCP server
integration** (`LOOP_MCP_SERVERS`), so it qualifies today. This branch adds a
clean, first-class **Slack AI capabilities** claim: it serves Slack's native
**Assistant** container (the AI side panel / app DM) — `assistant_thread_started`
greeting, **suggested prompts**, and a live **"is thinking…" status** — routed to
the same single Strands agent (same memory, guardrails, observability).

## What it does

`loop/slack_app.py` gains `_attach_assistant()`, wired via Bolt's `Assistant`
class when `LOOP_SLACK_ASSISTANT=on`:

- **thread_started** → greets the user and sets suggested prompts (recall a
  decision / summarize a channel / remember something).
- **user_message** → sets status `is thinking…`, runs `run_agent(...)` with full
  provenance + telemetry (`entrypoint = "<app>:assistant"`), and replies in the
  assistant thread.

## Enable it

On the Slack app (per app) at <https://api.slack.com/apps>:

1. **Agents & AI Apps** → enable the feature.
2. **OAuth & Permissions** → add **`assistant:write`** (plus the existing scopes)
   → reinstall.
3. **Event Subscriptions → Subscribe to bot events** → add
   `assistant_thread_started` and `assistant_thread_context_changed` (keep
   `message.im`).

Then:
```dotenv
LOOP_SLACK_ASSISTANT=on
```
```bash
loop
```

## Status / caveats

- ✅ Code imports + attaches cleanly; off by default; reuses the agent path so
  memory/guardrails/telemetry all apply.
- ⚠️ Not yet exercised against a live workspace with the feature enabled — hence
  experimental / off `main`.
- 🔜 To merge: enable the feature on one app, open the assistant pane, confirm
  suggested prompts + a round-trip answer, then fold the toggle into `main`.
