# CLAUDE.md — Loop project harness

This file is auto-loaded each Claude Code session. It is the **entry point** for
cross-session continuity. Read the linked docs before doing substantive work.

## Read first (cross-session memory)
1. **`architecture.md`** — how Loop is wired (runtime flow, components, the
   SQLite memory store, the model-provider env contract).
2. **`progress.md`** — current status, the MiniMax M3 migration checklist, and
   the production roadmap mapped to the 5 Pillars of Production AI.
3. **`.env.example`** — the env contract (Slack tokens + where to get them,
   MiniMax M3 vs native Anthropic, storage, MCP, logging).

**When you finish a working session, update `progress.md` (Status log) and, if
the wiring changed, `architecture.md`.** Keep these honest — they are what the
next session relies on.

## What Loop is
A Slack-native AI agent on Strands Agents. One Strands agent is the only brain;
two Slack entrypoints (`@mention`, `/loop`). Tools: `slack`, `slack_send_message`,
`calculator`, `think`, plus long-term memory and optional MCP servers.

## Key facts (so a fresh session doesn't re-derive them)
- **Provider = MiniMax M3**, via MiniMax's **Anthropic-compatible** endpoint
  (`https://api.minimax.io/anthropic`). Strands `AnthropicModel` is just the
  Anthropic SDK, so this is **env-only — no code change**. Switch back to native
  Anthropic by changing env. Get a MiniMax key at
  https://www.minimax.io/platform/user-center/basic-information.
- **Database = local SQLite** (`DATABASE_PATH`, default `./data/loop.db`); single
  `memory_entries` table; `LIKE` substring search; `metadata` is dropped; memory
  is currently **global** (no per-team/user scoping).
- **Transport = Slack Socket Mode** — each app needs a bot token (xoxb) + an
  app-level token (xapp). **Multi-app:** one process runs N apps via
  `SLACK_BOT_TOKEN_<NAME>`/`SLACK_APP_TOKEN_<NAME>` pairs (bare
  `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` still works as one app). Per-app behavior via
  `LOOP_PERSONA_<NAME>`. The running app's identity is decided by which tokens are
  loaded — verify with `auth.test`. The workspace has two apps: `Loop`
  (`A0BC0NPB9LN`) and `Rasputin_Loop` (`A0BCVHES936`); the user wants BOTH to run.
- A Socket Mode app needs **Event Subscriptions** (`app_mention`, `message.im`)
  configured per app, or it connects but receives no events ("silent bot").
- Entry: `loop/main.py` → `slack_app.start()`. Agent: `loop/agent.py::run()`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in Slack tokens + MiniMax key
loop
```

## Working norms
- Keep changes minimal and match the existing terse, single-brain style — no new
  intent parsers / routers / extraction pipelines.
- Provider choice stays env-driven; don't hardcode a provider in code.
- Before adding features, weigh the roadmap in `progress.md`: observability
  (Pillar 02) and evaluation (Pillar 01) are the highest-ROI gaps.
- **Every new feature extends the eval set.** The golden dataset
  (`evals/golden.json`) is required — add case(s) for the new behavior and at
  least partial tests. Features that aren't single-turn agent behaviors (infra
  like the provider factory; background loops like ambient mode) are covered by
  unit tests in `tests/` instead, since the golden runner scores one agent turn.
