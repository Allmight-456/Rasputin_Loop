# Loop — Progress & Roadmap

_Living status doc for cross-session continuity. Update the "Status log" at the
bottom every working session. Read this + `architecture.md` + `CLAUDE.md` before
starting work._

## Current status (2026-06-23)

- **Stack:** Python ≥3.10, Strands Agents, slack-bolt (Socket Mode), SQLite.
- **Provider:** migrating to **MiniMax M3** via MiniMax's Anthropic-compatible
  endpoint — **env-only change, no code change** (see `.env.example`, `architecture.md`).
- **Shape:** one process can run **several Slack apps** at once (e.g. `Loop` +
  `Rasputin_Loop`), each its own agent/persona; three entrypoints per app
  (`@mention`, DM, `/loop`); tools = `slack`, `slack_send_message`, `calculator`,
  `think` + long-term memory.
- **Production readiness:** harness landed (v0.3.0) — step tracing, enforcing
  guardrails, and a golden-set eval runner. Still missing: authz, PII redaction,
  thread context, semantic retrieval.

### MiniMax M3 migration — checklist
- [x] Confirm M3 reachable via Anthropic-compatible API (`https://api.minimax.io/anthropic`).
- [x] Document env vars + key source in `.env.example`.
- [ ] Fill real MiniMax key in local `.env` and smoke-test one `@Loop` mention.
- [ ] Confirm tool-calling works through MiniMax (slack lookup + add_memory round-trip).
- [ ] Decide whether to bump the code default model id off `claude-sonnet-4-6`.

---

## Production roadmap — the 5 Pillars of Production AI

Framework: _"Each pillar enables the next. Skip one and the whole thing breaks
in production."_ Below, each pillar is scored against Loop **today** and given
the next concrete moves.

### 01 — Evaluation · _Define success before code_  — ✅ in place (v0.3.0)
`loop-eval` runs `evals/golden.json` (~18 cases) through the real agent and scores
tool selection + reply content (must/must-not contain), prints a scorecard, and
exits non-zero on regression. Results persist to `eval_results`.
- [x] Golden set with per-case expectations (tools + content), per-category scoring.
- [x] CLI runner with CI exit code (`loop-eval`, `--limit`, `--case`, `--json`).
- [ ] Wire `loop-eval` into CI; expand to ~30 cases; add groundedness + cost metrics.
- [ ] Run Claude vs MiniMax M3 to quantify the migration.

### 02 — Observability · _See everything, always_  — ✅ in place (v0.3.0)
Per-interaction JSON telemetry (`loop.obs`) + an `interactions` row, **and**
per-step traces (`loop.trace`) for every model call, tool call, and reasoning
chunk + a `steps` row. `LOG_LEVEL=DEBUG` shows full thinking + tool I/O.
- [x] Structured logs per request (request id, channel, user, model, tokens, latency, outcome).
- [x] Step-level traces: reasoning + model API calls + tool inputs/outputs.
- [ ] Export traces to an OTLP backend (Phoenix/Langfuse); build a dashboard.
- [ ] Alert on error-rate / latency / quality drift.

### 03 — Data Foundation · _Question + Tracking data_  — ✅ much improved
`interactions` + `steps` + `eval_results` tables exist (substrate for 01/02), and
memory is now **episodic + provenance-stamped + full-text searchable**.
- [x] Stop dropping `metadata`; add the interaction/step event log.
- [x] **Provenance on every memory** — author/channel/team/source/thread_ts/date
  auto-stamped from the request (the `add_memory` tool can't carry metadata, so
  the store attaches it); surfaced back to the model via a custom injection format.
- [x] **FTS5 retrieval** — BM25 + recency, sanitized query, `LIKE` fallback.
      No new deps, no API key. Tested in `tests/test_memory.py`.
- [ ] Per-scope **filtering** in `search()` (columns + metadata are there; ranking
      is still global).
- [ ] **Semantic retrieval** — libSQL/Turso vector backend + pluggable embeddings
      on `feat/vector-memory-turso` (experimental; off `main` until verifiable).

### 04 — Orchestration · _Patterns that scale_  — ⚠️ improving
Guardrails add runaway-loop, repeat, and token-budget circuit breakers. Still
synchronous/blocking; agent rebuilt per request when MCP is active; no thread context.
- [x] Circuit breakers (max tool calls, repeats, token budget) via Strands hooks.
- [ ] Timeouts, retries with backoff, graceful degradation on provider errors.
- [ ] Non-blocking Slack handling (ack fast, process async).
- [ ] Pass thread/conversation context; cache the MCP-augmented agent.

### 05 — Governance · _What keeps you in production_  — ⚠️ partial
The `interactions` table is now an audit trail (who/what/which tools), and a risky
Slack-method blocklist + cost circuit breaker are enforced.
- [x] Audit log of interactions; blocklist for destructive Slack methods; token cap.
- [ ] AuthZ: allow-list workspaces/channels/users; scope tools per caller.
- [ ] PII redaction before persistence; retention/deletion policy.
- [ ] Per-user/day cost caps; document secret rotation.

---

## Why observability is the priority — the ROI case study

Six weeks post-launch numbers from the reference study (`Observability_study_ROI.png`):

| Metric | Result | Note |
|---|---|---|
| Accuracy | **87%** | target was 85% |
| Deflection rate | **62%** | ~$1M annual savings |
| Response time | **65% ↓** | customer wait time |
| CSAT | **4.4/5** | vs 4.2 for humans |

The point isn't the launch numbers — it's what happened next:

> Week 6: accuracy dropped 87% → 81%. **Dashboard flagged it within 4 hours.**
> Traced to a policy update that didn't propagate to the knowledge base —
> **identified in 90 minutes. Fixed in 2 days.** Accuracy restored, **3 new test
> cases added to prevent recurrence.**

_"Observable. Debuggable. Improvable. That's what you get when you build
measurement first."_ → For Loop, this argues for doing **Pillar 02 (observability)
+ Pillar 01 (eval)** before adding features. Without them, a MiniMax M3 swap or a
prompt edit could silently regress quality and we'd never know.

### Suggested next sprint (now that the harness exists)
1. Wire `loop-eval` into CI and run **Claude vs MiniMax M3** for a baseline diff.
2. Export traces to an OTLP backend (Phoenix/Langfuse) + a simple dashboard.
3. AuthZ allow-list + PII redaction before persistence (Pillar 05).

---

## Status log
- **2026-06-24 (later)** — **Episodic memory overhaul (Pillar 03), shipped to
  `main` — no new deps, no API key.** `SqliteMemoryStore` now: (1) **stamps
  provenance** on every memory (author/channel/team/source/thread_ts/date) pulled
  from `RequestState` — the `add_memory` tool only carries content, so the store
  attaches who/where/when itself; (2) recalls via **FTS5** (`bm25()` + recency)
  with a sanitized prefix-OR query (`_fts_match`, injection-safe) and a `LIKE`
  fallback if the SQLite build lacks FTS5; (3) surfaces provenance back to the
  model through a custom Strands **injection format** (`agent._format_memories`)
  that tags each `<entry>` with `from/in/when` (the default format drops
  metadata). System prompt gained **memory discipline** (save decisions/facts/
  prefs/episodes; skip chit-chat — the agent is the filter). Added
  `tests/test_memory.py` (6 cases, all green; runnable without pytest). Migration
  is additive + idempotent; verified against a copy of the committed `loop.db`
  (existing row re-indexed, recall works across wording). **Experimental, on
  branches (not merged):** `feat/vector-memory-turso` (libSQL/Turso native vector
  search + pluggable embeddings — fastembed default = no key) and
  `feat/slack-assistant` (native Slack "Assistant" AI surface). Hackathon: Loop
  qualifies today via **MCP server integration** (`LOOP_MCP_SERVERS`); the
  Assistant branch adds a clean **Slack AI capabilities** claim.
- **2026-06-24 (pm)** — **Multi-app support.** `slack_app._discover_apps()` +
  `start()` now run N Slack apps in one process (each its own Bolt App + Socket
  Mode thread), via `SLACK_BOT_TOKEN_<NAME>`/`SLACK_APP_TOKEN_<NAME>` pairs (bare
  `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` still works). One agent per app
  (`get_agent(app_name)`), each with optional `LOOP_PERSONA_<NAME>` system-prompt
  override — the seam for giving Loop vs Rasputin_Loop different jobs. Telemetry
  `entrypoint` is now `<app>:<kind>`. Diagnosed the "@Rasputin_Loop silent" issue:
  tokens were correct but **stale `loop` processes** (one on Loop's old env) were
  answering `@Loop`, and Rasputin_Loop almost certainly lacks **Event
  Subscriptions** (`app_mention`/`message.im`). `.env.example` documents the
  multi-app + persona convention. Verified discovery + persona via a scratch test.
- **2026-06-24** — Shipped **v0.3.1: file attachments / multimodal input**.
  `slack_app._collect_files()` downloads Slack attachments via
  `url_private_download` + bot token (needs **`files:read`** scope); allows
  `image/*`, `text/*`, `application/json`, size-capped by `LOOP_MAX_FILE_BYTES`.
  `agent.run(prompt, attachments=…)` → `_build_message()` builds a Strands
  multimodal content list — images go to **MiniMax M3 natively**, text/code/json
  inlined. DM `file_share` uploads now answered (subtype no longer dropped).
  System prompt + `.env.example` + `architecture.md` updated. PDFs extracted via
  **pypdf** (lightweight) and inlined as text. Diagnosed the silent-bot issue:
  `.env` had Loop's tokens in `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` and the
  Rasputin_Loop bot token in an unread var — fixed the bot token, flagged the
  xapp to regenerate under app `A0BCVHES936`. **Open:** verify M3's
  *Anthropic-compatible* endpoint accepts image blocks (else fall back to MiniMax
  OpenAI-compatible endpoint for images).
- **2026-06-23 (pm)** — Shipped the **v0.3.0 harness**. Fixed the `_extract_text`
  dict bug (replies were "_(no response)_"). Added a `message` handler (channel
  tracking + DM support, kills Bolt 404s). **Observability:** edge telemetry
  (`interactions`) + step tracing (`steps`) for every model call / tool call /
  reasoning chunk (`loop.obs`/`loop.trace`). **Guardrails:** enforcing tool-call
  limits (max calls, repeats, risky Slack methods, token budget) via Strands
  hooks + `cancel_tool`, per-rule env toggles. **Evals:** `evals/golden.json`
  (~18 cases) + `loop-eval` runner (`eval_results` table, CI exit code).
  New modules: `context.py`, `tracing.py`, `guardrails.py`, `eval.py`. Persisted
  memory `metadata`. Updated `.env.example`, `architecture.md`.
- **2026-06-23 (am)** — Adjusted Loop for **MiniMax M3** (Anthropic-compatible
  endpoint, env-only). Rewrote `.env.example` with Slack-token + MiniMax-key
  guidance. Created the cross-session harness: `CLAUDE.md`, `architecture.md`,
  this file. Mapped current state to the 5 Pillars and the observability ROI study.
