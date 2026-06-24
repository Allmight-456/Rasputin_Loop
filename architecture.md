# Loop — Architecture

_Last updated: 2026-06-24 (v0.3.1: file attachments + multi-app: one process can run several Slack apps). Keep this current; it is the cross-session source of truth for how Loop is wired._

Loop is a Slack-native AI agent built on [Strands Agents](https://strandsagents.com/).
When a user `@Loop`-mentions the bot or runs `/loop`, a single Strands agent
thinks, optionally calls a tool, and replies in-thread. There is **one brain**
(the agent) — no intent parser, no command router, no extraction pipeline.

## Runtime flow

```
Slack (@mention, DM, or /loop)
        │
        ▼
slack_bolt.App × N  (Socket Mode, slack_app.py — _discover_apps() builds one per
                     configured app: bare SLACK_BOT_TOKEN/SLACK_APP_TOKEN, and/or
                     SLACK_BOT_TOKEN_<NAME>/SLACK_APP_TOKEN_<NAME> pairs; each its
                     own socket thread, each tags telemetry "<name>:<entrypoint>")
   • app_mention  → strip self-mention → run_agent(..., app_name) → say() in thread
   • message      → log channel/user (tracking); answer only if DM (incl. file uploads)
   • /loop        → ack() → run_agent(...) → respond()
   • _collect_files(): download event["files"] via url_private_download + bot token
     (needs files:read); allow image/* + text/* + json, size-capped
   • UX: 👀 reaction on receipt → ✅ on success / ⚠️ on error
        │  wraps every call in observability.record(Interaction)
        ▼
loop.agent.run(prompt, attachments=…, context=…, request_id=…)   (agent.py)
   • prepend a [context] line (who/where) so tools can act
   • _build_message(): no files → str; else Strands content list
     [{text}, {image:…} (M3 multimodal) / inlined text for text/code/json/pdf(pypdf)]
   • set RequestState in a ContextVar (loop/context.py)
   • if LOOP_MCP_SERVERS set: enter MCPClient stdio context, merge its tools
        │
        ▼
Strands Agent
   • model       = AnthropicModel(client_args={api_key, base_url?}, model_id, max_tokens=2048)
   • system_prompt = Loop persona (Slack mrkdwn, concise, tool guidance)
   • tools       = [slack, slack_send_message, calculator, think] (+ MCP tools)
   • memory      = MemoryManager(stores=[_memory_store()], injection=provenance fmt)
                   _memory_store(): sqlite FTS5 (default) | hybrid libSQL+vectors (RRF)
   • hooks       = [LoopTracer, Guardrails]   callback_handler = reasoning_callback
        │           │
        │           ├─ LoopTracer  → log + persist every model call / tool call / reasoning (steps table)
        │           └─ Guardrails  → cancel_tool on limit/repeat/risky-method/token breaches
        ▼
agent(prompt) → AgentRun(text + tokens + tool_calls + model_calls + guardrail_hits + reasoning)
        │
        ▼
final text → Slack thread;  one JSON telemetry line + interactions row recorded
```

## Components

| File | Responsibility |
|---|---|
| `loop/main.py` | Entrypoint (`loop` script). Loads `.env`, configures logging, calls `slack_app.start()`. |
| `loop/slack_app.py` | Slack Bolt app(s) over **Socket Mode**. `_discover_apps()` + `start()` run **one or many** apps concurrently (one socket thread each). Three handlers (`app_mention`, `message`, `/loop`); `message` tracks all channels + answers DMs (incl. file uploads). `_collect_files()` downloads attachments (`files:read`). Wraps every call in `observability.record` (entrypoint = `<app>:<kind>`). |
| `loop/agent.py` | Builds **one Strands `Agent` per app** (`get_agent(app_name)`), each with its own system prompt via `_system_prompt()` (base + optional `LOOP_PERSONA_<NAME>`). `run(prompt, attachments=…, app_name=…)` builds a string or multimodal content list via `_build_message()`, returns an `AgentRun` (text + telemetry), manages the per-request `RequestState`. |
| `loop/context.py` | `RequestState` + a `ContextVar` so the hooks can attribute steps and enforce per-request limits across one `run()`. |
| `loop/tracing.py` | `LoopTracer` (Strands `HookProvider`) + `reasoning_callback`: logs & persists each model call, tool call, and reasoning chunk. |
| `loop/guardrails.py` | `Guardrails` (`HookProvider`): enforcing tool-call limits via `cancel_tool`, with per-rule env toggles. |
| `loop/observability.py` | `Interaction` + `record()` (edge telemetry), `record_step()`, `record_eval()`; owns the `interactions` / `steps` / `eval_results` tables. |
| `loop/eval.py` | `loop-eval` CLI: runs `evals/golden.json` through the real agent and scores tool selection + reply expectations. |
| `loop/storage.py` | `SqliteMemoryStore` (default backend) — Strands `MemoryStore` over local SQLite. **Episodic + provenance-stamped:** every memory auto-tagged with who/where/when (from `RequestState`); recall is **FTS5** (BM25 + recency) with a sanitized query + `LIKE` fallback; exact-content dedup on write. |
| `loop/vector_store.py` | `HybridMemoryStore` (opt-in, `LOOP_MEMORY_BACKEND=hybrid`) — same contract over **libSQL/Turso**: fuses FTS5 (BM25) + native vector search (`F32_BLOB`/`vector_top_k`) via **Reciprocal Rank Fusion** for hybrid Agentic-RAG recall. Same provenance + dedup. |
| `loop/embeddings.py` | Pluggable text embedder for the hybrid store: `fastembed` (local, no key — default), `minimax`/`openai` (hosted), `hashing` (dep-free fallback). |
| `loop/__init__.py` | Version marker (`0.3.1`). |

## Model provider

The agent uses Strands' `AnthropicModel`, which wraps the Anthropic Python SDK.
Provider selection is **env-driven** (see `.env.example`):

- `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN` as fallback) — credential.
- `ANTHROPIC_BASE_URL` — optional endpoint override.
- `ANTHROPIC_MODEL` — model id (code default: `claude-sonnet-4-6`).

**Current target: MiniMax M3.** MiniMax exposes an Anthropic-compatible endpoint,
so Loop runs on it with zero code change — just env:
`ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic`, `ANTHROPIC_MODEL=MiniMax-M3`,
and the MiniMax key in `ANTHROPIC_API_KEY`. Switching back to native Anthropic is
also env-only.

## Database (storage layer)

**Engine:** local **SQLite** (`sqlite3` stdlib), file at `DATABASE_PATH`
(default `./data/loop.db`). No external DB/service. WAL journal mode; a
`threading.Lock` guards writes because the connection is shared across the Bolt
handler threads (`check_same_thread=False`).

**Schema (tables):**

```sql
-- long-term memory (loop/storage.py) — episodic + provenance-stamped
CREATE TABLE memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    metadata TEXT,                      -- full provenance dict (JSON)
    kind TEXT, author TEXT, channel TEXT, team TEXT, source TEXT, thread_ts TEXT,
                                        -- who/where/which-app, promoted to columns for scoping
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
-- FTS5 mirror for ranked recall; kept in sync by an AFTER INSERT trigger.
CREATE VIRTUAL TABLE memory_fts USING fts5(
    content, content='memory_entries', content_rowid='id');

-- telemetry / data foundation (loop/observability.py)
CREATE TABLE interactions (             -- one row per Slack interaction (edge)
    id, request_id, ts, entrypoint, team, channel, channel_type, "user",
    thread_ts, prompt, model_id, input_tokens, output_tokens, total_tokens,
    tool_calls, model_calls, guardrail_hits, latency_ms, outcome, error );
CREATE TABLE steps (                    -- one row per model call / tool call / reasoning / guardrail
    id, request_id, ts, seq, kind, name, detail, tokens, duration_ms, outcome );
CREATE TABLE eval_results (             -- one row per golden case per loop-eval run
    id, run_id, ts, case_id, category, model_id, passed, latency_ms, tool_calls, reasons );
```

All telemetry tables are opened on a separate connection from the memory store
(WAL makes concurrent access safe) and created/migrated lazily on first write.

**How memory is used:**
- `SqliteMemoryStore.add(content, metadata)` — auto-stamps provenance from the
  active `RequestState` (author/channel/team/source/thread_ts) into columns +
  the JSON blob, then INSERTs. The `add_memory` *tool* only lets the model pass
  content, so this is **where who/where/when gets attached** — the agent never
  has to (and can't) supply it.
- `SqliteMemoryStore.search(query)` — FTS5 `MATCH` ranked by `bm25()` then
  recency, capped at `max_search_results` (5). The query is reduced to safe
  prefix-OR tokens (`_fts_match`) — stopwords dropped, no FTS5 syntax injection;
  empty → `LIKE` fallback. Returns `MemoryEntry` carrying the provenance metadata.
- **Injection format** (`agent._format_memories`) — recalled memories are
  injected before each model call as a `<memory>` block where each `<entry>`
  carries `from="@user" in="#channel" when="date"`, so the model can weigh and
  cite provenance. (The Strands default format drops metadata; this restores it.)
- `writable=True`, `extraction=False` — the agent is the filter: it decides what
  is worth persisting via `add_memory` (system prompt steers it to save
  decisions/facts/preferences/episodes, skip chit-chat); nothing is auto-extracted.

**Memory backends (env `LOOP_MEMORY_BACKEND`):**
- `sqlite` (default) — FTS5 lexical recall, zero deps, no API key.
- `hybrid` (aliases `libsql`/`turso`/`vector`) — libSQL/Turso **hybrid** recall:
  FTS5 + semantic vectors fused with RRF (`loop/vector_store.py`); needs
  `pip install -e ".[vector]"` + embeddings (`fastembed` local, no key by
  default). Verified live (memory add→recall through MiniMax M3). See
  `docs/vector-memory.md`.

**Remaining data-layer gaps (see `progress.md` Pillar 3):**
- Scoping columns (channel/team/user) are stored and returned but `search()`
  ranks globally; per-scope filtering is a small follow-up.
- Dedup is exact-content only; near-duplicate (semantic) dedup is a future step.

## Extensibility — MCP tools at runtime

Set `LOOP_MCP_SERVERS="<command> <args...>"` (a stdio command). On each request
the agent enters the `MCPClient` context, loads that server's tools, and exposes
them alongside the built-ins. Note: when MCP is active, the agent is **rebuilt
per request** (the cached agent is only used on the no-MCP path).

## Harness (observability · guardrails · evals)

- **Observability (Pillar 02):** every interaction emits a JSON telemetry line +
  an `interactions` row; every model call, tool call, and reasoning chunk emits a
  `loop.trace` line + a `steps` row. `LOOP_TRACE=off` silences logs (DB still
  written); `LOG_LEVEL=DEBUG` adds full reasoning + tool I/O. Logger names:
  `loop.obs`, `loop.trace`, `loop.guard`.
- **Guardrails (Pillars 04/05):** `Guardrails` cancels a tool call before it runs
  when a per-request limit trips — max tool calls, repeated identical call, risky
  Slack method, or token budget — each independently toggled by env. Enforcing by
  default; the model receives the cancel reason and replies gracefully.
- **Evaluation (Pillar 01):** `loop-eval` runs `evals/golden.json` through the
  real agent, scores tool selection + reply content, prints a scorecard, persists
  `eval_results`, and exits non-zero on regression (CI gate).

## What Loop does NOT have yet

No authn/authorization (allow-list of workspaces/users), no PII redaction before
persistence, no rate limiting across requests, no thread/conversation history
passed to the agent (each turn is stateless apart from long-term memory), and no
export of traces to an external OTEL backend. These map to the deferred items in
`progress.md`.
