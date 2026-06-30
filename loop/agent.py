"""Loop's Strands Agent.

Built per the official Strands Agents README (sdk-python):

    from strands import Agent
    from strands.models.anthropic import AnthropicModel
    from strands_tools import slack, slack_send_message, calculator, think
    from strands.tools.mcp import MCPClient
    from mcp import stdio_client, StdioServerParameters

    with MCPClient(lambda: stdio_client(StdioServerParameters(command=..., args=[...]))) as mcp:
        agent = Agent(model=..., tools=[...], memory_manager=...)
        response = agent("...")

We follow the documented pattern: the MCPClient owns its stdio connection's
lifetime, so we enter its context before the agent runs and exit after. The
agent itself is built lazily on first use (cheap — just config wiring).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.memory import InjectionFormatContext, MemoryInjectionConfig, MemoryManager
from strands.models.anthropic import AnthropicModel
from strands.tools.mcp import MCPClient
from strands_tools import calculator, slack, think
from strands_tools.slack import slack_send_message

from loop import context as reqctx
from loop.guardrails import Guardrails
from loop.storage import SqliteMemoryStore
from loop.tracing import LoopTracer, reasoning_callback

log = logging.getLogger("loop.agent")

_agents: dict[str, Agent] = {}


@dataclass
class AgentRun:
    """Result of one agent invocation: the reply text plus the telemetry the
    observability layer records (tokens, tools used, model, reasoning, etc.)."""

    text: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)
    model_calls: int = 0
    guardrail_hits: int = 0
    reasoning: str = ""
    stop_reason: str | None = None


_DEFAULT_MODEL = "claude-sonnet-4-6"


def _model_id() -> str:
    """The model id, provider-neutral. ``LOOP_MODEL`` wins; ``ANTHROPIC_MODEL`` is
    kept as the back-compat fallback (MiniMax M3 sets it today)."""
    return os.environ.get("LOOP_MODEL") or os.environ.get("ANTHROPIC_MODEL", _DEFAULT_MODEL)


def _max_tokens() -> int:
    return int(os.environ.get("LOOP_MAX_TOKENS", "2048"))


def _openai_style_client_args() -> dict[str, Any]:
    """Client args for OpenAI-format clients (OpenAI/LiteLLM). Optional — LiteLLM
    also reads provider keys straight from the environment."""
    args: dict[str, Any] = {}
    if key := (os.environ.get("OPENAI_API_KEY") or os.environ.get("LOOP_LLM_API_KEY")):
        args["api_key"] = key
    if base := (os.environ.get("OPENAI_BASE_URL") or os.environ.get("LOOP_LLM_BASE_URL")):
        args["base_url"] = base
    return args


def _build_model(model_id: str | None = None) -> Any:
    """Construct the Strands model for the configured provider.

    This is Loop's USP made real: the provider is **env-selected** rather than
    hardcoded, so the same agent can run on Anthropic, MiniMax, OpenAI, Bedrock,
    a LiteLLM multi-provider gateway, or a local Ollama model — no vendor lock-in.
    ``LOOP_MODEL_PROVIDER`` picks the backend (default ``anthropic``, which keeps
    the existing MiniMax-M3-via-Anthropic-compatible-endpoint path unchanged).
    Provider SDKs beyond ``anthropic`` are lazy-imported so they're only required
    when actually selected (install via ``pip install "loop[providers]"``).

    ``model_id`` overrides the env model id — used by the ambient loop for cheap
    background scanning (``LOOP_AMBIENT_MODEL``).
    """
    provider = os.environ.get("LOOP_MODEL_PROVIDER", "anthropic").strip().lower()
    model_id = model_id or _model_id()
    max_tokens = _max_tokens()

    # Anthropic SDK — also serves any Anthropic-wire-compatible endpoint
    # (MiniMax M3, native Claude, compatible proxies) via ANTHROPIC_BASE_URL.
    if provider in {"anthropic", "minimax", ""}:
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN must be set")
        client_args: dict[str, Any] = {"api_key": api_key}
        if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
            client_args["base_url"] = base_url
        return AnthropicModel(client_args=client_args, model_id=model_id, max_tokens=max_tokens)

    # LiteLLM gateway — one class, many providers (model_id like "openai/gpt-4o"
    # or "anthropic/claude-3-7-sonnet"). The cleanest multi-provider story.
    if provider == "litellm":
        from strands.models.litellm import LiteLLMModel  # noqa: PLC0415

        return LiteLLMModel(
            client_args=_openai_style_client_args() or None,
            model_id=model_id,
            params={"max_tokens": max_tokens},
        )

    if provider == "openai":
        from strands.models.openai import OpenAIModel  # noqa: PLC0415

        return OpenAIModel(
            client_args=_openai_style_client_args() or None,
            model_id=model_id,
            params={"max_tokens": max_tokens},
        )

    # AWS Bedrock — region-resident inference (boto3 already available).
    if provider == "bedrock":
        from strands.models.bedrock import BedrockModel  # noqa: PLC0415

        kwargs: dict[str, Any] = {"model_id": model_id, "max_tokens": max_tokens}
        if region := (os.environ.get("LOOP_BEDROCK_REGION") or os.environ.get("AWS_REGION")):
            kwargs["region_name"] = region
        return BedrockModel(**kwargs)

    # Local / self-hosted models via Ollama — fully sovereign, no external calls.
    if provider == "ollama":
        from strands.models.ollama import OllamaModel  # noqa: PLC0415

        host = os.environ.get("LOOP_OLLAMA_HOST", "http://localhost:11434")
        return OllamaModel(host=host, model_id=model_id, max_tokens=max_tokens)

    if provider == "gemini":
        from strands.models.gemini import GeminiModel  # noqa: PLC0415

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        return GeminiModel(
            client_args={"api_key": api_key} if api_key else None,
            model_id=model_id,
            params={"max_tokens": max_tokens},
        )

    raise RuntimeError(
        f"Unknown LOOP_MODEL_PROVIDER={provider!r}. "
        "Supported: anthropic (default), litellm, openai, bedrock, ollama, gemini."
    )


SYSTEM_PROMPT = """You are Loop — a Slack-native AI agent for teams.

Personality:
- Concise but warm
- Honest — if you don't know or can't find something, say so
- Proactive — surface relevant memory when it adds context

Tools you have:
- slack + slack_send_message — talk to Slack (any Web API method)
- search_memory / add_memory — long-term memory (auto-injected before each reply)
- calculator — math
- think — structured reasoning for hard problems

When the user asks a question:
- "tag @alice" → use slack to look up the user, then post a message
- "what did we decide about X" → long-term memory (auto-injected) usually has it
- "summarize this channel" → use slack with conversations_history / conversations_replies
- "remember this" / "save this decision" → call add_memory
- a message with attached files → their contents (images, text, or code) are
  included with the message — read them directly and answer about them
- greetings, thanks, small talk → reply conversationally, no tools

Memory discipline — you are the filter, so save signal, not noise:
- DO save with add_memory: decisions ("we're going with X"), durable facts
  (schedules, owners, links, config), team/user preferences, commitments, and
  the distilled outcome of an important discussion (an "episode": a one-to-three
  sentence summary of what was decided/learned, not the raw transcript).
- DON'T save: greetings, thanks, chit-chat, one-off lookups, or anything already
  obvious. When in doubt, prefer NOT saving — a noisy memory hurts recall.
- When you're only ANSWERING a recall/lookup question (the fact is already in the
  injected <memory>), just answer — do NOT call add_memory to re-save it.
- Write each memory as a self-contained statement (who/what, and any specifics)
  so it still makes sense months later. Provenance — who said it, the channel,
  and the date — is attached automatically; you don't need to add it yourself.
- Injected memories arrive in a <memory> block tagged with that provenance; cite
  it when it matters ("per @alice on 2026-06-20 …").

Respond in Slack mrkdwn (*bold*, _italic_, `code`, • bullets, > quotes). Keep replies under 1500 chars when possible. Don't announce tool calls — use them and respond naturally."""


def _mcp_client() -> MCPClient | None:
    """Build one MCPClient from a single env var: LOOP_MCP_SERVERS='uvx strands-agents-mcp-server'.

    Returns None if not set. For more servers, add multiple invocations here.
    """
    spec = os.environ.get("LOOP_MCP_SERVERS")
    if not spec:
        return None

    parts = spec.split()
    command, args = parts[0], parts[1:]
    log.info("loop: MCP client registered (command=%s, args=%s)", command, args)
    return MCPClient(lambda: stdio_client(StdioServerParameters(command=command, args=args)))


def _system_prompt(app_name: str | None) -> str:
    """Base persona, plus an optional per-app override from LOOP_PERSONA_<NAME>.

    This is the seam for giving each Slack app its own behavior: set e.g.
    LOOP_PERSONA_RASPUTIN="You are the on-call incident assistant ..." and only
    the `rasputin` app's agent picks it up. Unset → the shared base persona.
    """
    if app_name:
        persona = os.environ.get(f"LOOP_PERSONA_{app_name.upper()}")
        if persona and persona.strip():
            return f"{SYSTEM_PROMPT}\n\n## This app's specific role\n{persona.strip()}"
    return SYSTEM_PROMPT


def _xml_escape(value: Any) -> str:
    s = "" if value is None else str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _format_memories(ctx: InjectionFormatContext) -> str:
    """Render recalled memories with their provenance so the model can weigh and
    cite them. The default Strands format drops entry metadata; we surface who
    said it, where, and when as attributes on each <entry>."""
    lines: list[str] = []
    for e in ctx.entries:
        meta = e.metadata or {}
        attrs = ""
        if meta.get("author"):
            attrs += f' from="@{_xml_escape(meta["author"])}"'
        if meta.get("channel"):
            attrs += f' in="#{_xml_escape(meta["channel"])}"'
        if meta.get("date"):
            attrs += f' when="{_xml_escape(meta["date"])}"'
        lines.append(f"<entry{attrs}>{_xml_escape(e.content)}</entry>")
    body = "\n".join(lines)
    return f"<memory>\n{body}\n</memory>" if body else ""


def _memory_store() -> Any:
    """Pick the memory backend from env.

    Default ``sqlite`` = the FTS5 + provenance store (no deps, no key). ``hybrid``
    (aliases: ``libsql``/``turso``/``vector``) = the libSQL/Turso hybrid store
    (FTS5 + semantic vectors, RRF-fused); needs ``pip install "loop[vector]"`` and
    raises a clear error if missing, so the choice is explicit.
    """
    backend = os.environ.get("LOOP_MEMORY_BACKEND", "sqlite").strip().lower()
    if backend in {"hybrid", "libsql", "turso", "vector"}:
        from loop.vector_store import HybridMemoryStore  # noqa: PLC0415 (optional path)

        log.info("memory backend: libsql/turso hybrid (FTS5 + semantic vectors, RRF-fused)")
        return HybridMemoryStore()
    return SqliteMemoryStore()


def _build_agent(app_name: str | None = None, model_id: str | None = None) -> Agent:
    """Build an agent for one app. Caller is responsible for holding any MCP context.

    ``model_id`` overrides the env model — used for cost-routed background runs.
    """
    tools: list[Any] = [slack, slack_send_message, calculator, think]

    memory_manager = MemoryManager(
        stores=[_memory_store()],
        search_tool_config=True,
        add_tool_config=True,
        injection=MemoryInjectionConfig(format=_format_memories),
    )

    return Agent(
        model=_build_model(model_id),
        system_prompt=_system_prompt(app_name),
        tools=tools,
        memory_manager=memory_manager,
        hooks=[LoopTracer(), Guardrails()],
        callback_handler=reasoning_callback,
    )


def get_agent(app_name: str | None = None, model_id: str | None = None) -> Agent:
    """Return the cached agent for `app_name` (one agent per Slack app).

    A non-default `model_id` is cached under its own key so cost-routed runs
    (e.g. ambient scanning on a cheaper model) don't evict the primary agent.
    """
    key = app_name or "_default"
    if model_id:
        key = f"{key}#{model_id}"
    agent = _agents.get(key)
    if agent is None:
        agent = _build_agent(app_name, model_id)
        _agents[key] = agent
        log.info("loop agent initialised (app=%s)", key)
    return agent


def run(
    prompt: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    request_id: str | None = None,
    app_name: str | None = None,
    model_id: str | None = None,
) -> AgentRun:
    """Invoke the agent and return its reply plus telemetry (`AgentRun`).

    `context` is the Slack request envelope (user/channel/channel_type). When
    present it is prepended as a small, clearly-marked context line so the agent
    knows *who* is asking and *where* — e.g. enough for its slack tool to tag the
    right user or read the right channel. This is the only "context" plumbing;
    there is still no intent parser or router — the agent remains the only brain.

    A `RequestState` is established for the call (using `request_id` if given, so
    it lines up with the edge `Interaction`) and made the current context, so the
    tracing + guardrail hooks can attribute steps and enforce per-request limits.

    If an MCP client is registered, we enter its context manager so the stdio
    connection is alive for the duration of the call (the documented pattern from
    the Strands README), and MCP tools are added inside that scope.
    """
    message = _build_message(_with_context(prompt, context), attachments)
    state = reqctx.RequestState(request_id=request_id) if request_id else reqctx.RequestState()
    _apply_provenance(state, context, app_name)
    token = reqctx.set_current(state)
    try:
        client = _mcp_client()
        if client is None:
            return _result_to_run(get_agent(app_name, model_id)(message), state)
        with client:
            tools = list(get_agent(app_name, model_id).tool_registry.registry.values())
            tools.extend(client.list_tools_sync())
            agent = _build_agent(app_name, model_id)
            agent.tool_registry.process_tools(tools)
            return _result_to_run(agent(message), state)
    finally:
        reqctx.reset_current(token)


def _apply_provenance(state: reqctx.RequestState, context: dict[str, Any] | None, app_name: str | None) -> None:
    """Stamp the request with who/where so memories saved during it carry it."""
    ctx = context or {}
    state.author = ctx.get("user")
    state.channel = ctx.get("channel")
    state.team = ctx.get("team")
    state.thread_ts = ctx.get("thread_ts")
    state.source = app_name


def _with_context(prompt: str, context: dict[str, Any] | None) -> str:
    if not context:
        return prompt
    bits: list[str] = []
    if context.get("user"):
        bits.append(f"from Slack user <@{context['user']}>")
    if context.get("channel"):
        bits.append(f"in channel <#{context['channel']}>")
    if context.get("channel_type"):
        bits.append(f"({context['channel_type']})")
    if not bits:
        return prompt
    return f"[context] message {' '.join(bits)}\n\n{prompt}"


# Slack mimetype → Strands image block format. Anything not listed (text/code/
# json) is inlined as text by _build_message.
_IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _pdf_text(data: bytes) -> str:
    """Extract text from a PDF. Lazy import of pypdf (lightweight, pure-Python)."""
    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:  # noqa: BLE001
        log.exception("pdf text extraction failed")
        return ""


def _build_message(text: str, attachments: list[dict[str, Any]] | None) -> Any:
    """Plain string when there are no files; otherwise a Strands multimodal
    content list ``[{"text": …}, {"image": …}, …]``. Images go to the model
    natively (MiniMax M3 is multimodal); text/code/json files are decoded and
    inlined as text blocks. Anything that won't decode is skipped, not fatal."""
    if not attachments:
        return text
    blocks: list[dict[str, Any]] = [{"text": text}]
    for a in attachments:
        mt, data, name = a.get("mimetype", ""), a.get("bytes", b""), a.get("name", "file")
        fmt = _IMAGE_FORMATS.get(mt)
        if fmt:
            blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
            continue
        if mt == "application/pdf":
            body = _pdf_text(data)
            blocks.append({"text": f"\n--- attached PDF: {name} ---\n{body or '(no extractable text)'}"})
            continue
        try:
            body = data.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            log.info("attachment not decodable as text, skipped: %s (%s)", name, mt)
            continue
        blocks.append({"text": f"\n--- attached file: {name} ---\n{body}"})
    return blocks


def _result_to_run(result: Any, state: reqctx.RequestState | None = None) -> AgentRun:
    """Pull text + metrics off a Strands AgentResult, folding in the per-request
    counters the hooks accumulated (model calls, ordered tool calls, guardrail
    hits, reasoning)."""
    model_id = _model_id()
    usage: dict[str, Any] = {}
    tool_calls: list[str] = []
    metrics = getattr(result, "metrics", None)
    if metrics is not None:
        acc = getattr(metrics, "accumulated_usage", None)
        if isinstance(acc, dict):
            usage = acc
        tm = getattr(metrics, "tool_metrics", None)
        if isinstance(tm, dict):
            tool_calls = list(tm.keys())

    # Prefer the tracer's ordered, repeat-aware tool list when available.
    if state is not None and state.tool_calls:
        tool_calls = state.tool_calls

    return AgentRun(
        text=_extract_text(result),
        model_id=model_id,
        input_tokens=int(usage.get("inputTokens", 0) or 0),
        output_tokens=int(usage.get("outputTokens", 0) or 0),
        total_tokens=int(usage.get("totalTokens", 0) or 0),
        tool_calls=tool_calls,
        model_calls=state.model_calls if state else 0,
        guardrail_hits=state.guardrail_hits if state else 0,
        reasoning="\n".join(state.reasoning).strip() if state else "",
        stop_reason=getattr(result, "stop_reason", None),
    )


def _extract_text(result: Any) -> str:
    """Final assistant text from a Strands AgentResult.

    Strands' `message` is a dict (`{"content": [{"text": ...}]}`), so attribute
    access (`block.text`) never matches — its own `__str__` is the canonical
    extractor (handles text blocks, citations, structured output). We use that,
    with a manual dict/attr parse as a defensive fallback.
    """
    try:
        text = str(result).strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass

    msg = getattr(result, "message", None)
    if msg is None:
        return ""
    content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, "content", []) or []
    parts: list[str] = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()