# <img width="25" height="25" alt="loop" src="https://github.com/user-attachments/assets/20c20ad6-7a80-49eb-aa2e-5a0cf3cc1ac4" /> Loop

A Slack-native AI agent built on [Strands Agents](https://strandsagents.com/).

When you `@Loop` mention the bot (or invoke `/loop`), the Strands agent thinks, optionally uses one of its tools, and replies in the thread. It remembers things via Strands' built-in long/short-term memory, and it talks to Slack via the official `slack` tool from `strands-agents-tools`.

## Tools available to the agent

| Tool | Source | Purpose |
|---|---|---|
| `slack` | `strands_tools.slack` | Read/post messages, list users, look up channels — any Slack Web API method |
| `slack_send_message` | `strands_tools.slack` | Convenience wrapper for posting a message |
| `search_memory` | Strands `MemoryManager` | Long-term memory recall (auto-injected into context) |
| `add_memory` | Strands `MemoryManager` | Write to long-term memory |
| `calculator` | `strands_tools.calculator` | Math |
| `think` | `strands_tools.think` | Structured reasoning |

That's it. No hand-rolled Slack helpers, no scoring heuristics, no extraction pipeline. The agent decides what to do.

## Setup

```bash
# 1. Install
python -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Configure
cp .env.example .env
# edit .env with your Slack + Anthropic credentials

# 3. Run
loop
```

## Environment

| Var | Required | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | yes | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | yes (Socket Mode) | App-Level Token (`xapp-...`) |
| `ANTHROPIC_API_KEY` | one of these | Standard Anthropic API key |
| `ANTHROPIC_AUTH_TOKEN` | one of these | Proxy token (used when `ANTHROPIC_API_KEY` is unset) |
| `ANTHROPIC_BASE_URL` | no | Override for proxies |
| `ANTHROPIC_MODEL` | no | Defaults to `claude-sonnet-4-6` |
| `DATABASE_PATH` | no | SQLite file path (default `./data/loop.db`) |
| `LOG_LEVEL` | no | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Architecture

```
Slack @-mention  ──►  slack_bolt.App  ──►  loop.agent.run(prompt)
                                                │
                                                ▼
                                       with MCPClient(...) (if LOOP_MCP_SERVERS set):
                                                │
                                                ▼
                                       Strands Agent(AnthropicModel,
                                                     system_prompt,
                                                     tools=[slack, slack_send_message,
                                                            calculator, think,
                                                            search_memory, add_memory,
                                                            *mcp_tools],
                                                     memory_manager=MemoryManager(stores=[SqliteMemoryStore()]))
                                                │
                                                ▼
                                       agent(prompt)  ──►  final response
                                                │
                                                ▼
                                       slack_bolt "say" reply in thread
```

## Adding MCP tools at runtime

Set `LOOP_MCP_SERVERS` to a stdio command (and any args). The agent enters the
MCP context for each invocation, loads the server's tools, and exposes them
alongside the built-in ones. Drop a new server in with no code changes:

```bash
LOOP_MCP_SERVERS="uvx strands-agents-mcp-server" loop
```
