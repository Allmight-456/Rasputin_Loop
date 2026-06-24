"""Guardrails for Loop (Pillars 04 orchestration / 05 governance).

Keeps the agent from going haywire: runaway tool loops, repeated identical calls,
risky Slack methods, and runaway token spend. Implemented as a Strands
`HookProvider` on `BeforeToolCallEvent`; a tripped rule sets ``cancel_tool`` so
the call is replaced with an error tool-result the model sees and adapts to —
the agent keeps control and replies gracefully instead of crashing.

Enforcing, with per-rule env toggles (a rule is OFF when its value is 0/empty):

  LOOP_GUARDRAILS=on               master switch (default on)
  LOOP_MAX_TOOL_CALLS=12           cap tool calls per request   (0 = off)
  LOOP_MAX_TOOL_REPEAT=2           cap identical name+input calls (0 = off)
  LOOP_MAX_TOKENS_PER_REQUEST=20000  token circuit breaker      (0 = off)
  LOOP_BLOCKED_SLACK_METHODS=chat.delete,conversations.archive,conversations.kick,admin.*

Method matching is dot/underscore-insensitive (`chat.delete` == `chat_delete`),
with a trailing ``*`` wildcard for prefix bans like ``admin.*``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from loop import context
from loop import observability as obs
from loop.tracing import tool_signature

log = logging.getLogger("loop.guard")

_DEFAULT_BLOCKED = "chat.delete,conversations.archive,conversations.kick,admin.*"


def _enabled() -> bool:
    return os.environ.get("LOOP_GUARDRAILS", "on").strip().lower() not in {"off", "0", "false", "no"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _blocked_methods() -> list[str]:
    raw = os.environ.get("LOOP_BLOCKED_SLACK_METHODS", _DEFAULT_BLOCKED)
    return [_norm(m) for m in raw.split(",") if m.strip()]


def _norm(method: str) -> str:
    return method.strip().lower().replace(".", "_")


def _method_blocked(action: str, patterns: list[str]) -> bool:
    a = _norm(action)
    for p in patterns:
        if p.endswith("*") and a.startswith(p[:-1]):
            return True
        if a == p:
            return True
    return False


class Guardrails(HookProvider):
    """Enforce per-request safety limits before each tool call."""

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        # order > 0 so the tracer's BeforeToolCallEvent callback (which increments
        # the counters we read) runs first.
        registry.add_callback(BeforeToolCallEvent, self._before_tool, order=10)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        if not _enabled():
            return
        st = context.current()
        if st is None:
            return
        tu = event.tool_use or {}
        name = tu.get("name", "?")
        tool_input = tu.get("input") or {}

        reason = self._violation(st, name, tool_input)
        if reason is None:
            return

        rule, message = reason
        event.cancel_tool = message  # Strands turns this into an error tool-result
        st.guardrail_hits += 1
        seq = st.next_seq()
        log.warning("blocked rid=%s rule=%s tool=%s — %s", st.request_id, rule, name, message)
        obs.record_step(
            st.request_id, seq, "guardrail", rule,
            detail={"tool": name, "reason": message, "input": tool_input},
            outcome="blocked",
        )

    def _violation(self, st: context.RequestState, name: str, tool_input: dict) -> tuple[str, str] | None:
        # 1) risky Slack method blocklist (most important — destructive actions)
        if name == "slack":
            action = str(tool_input.get("action", ""))
            if action and _method_blocked(action, _blocked_methods()):
                return "blocked_method", f"Slack method '{action}' is blocked by policy."

        # 2) repeated identical call (name + input)
        max_repeat = _int_env("LOOP_MAX_TOOL_REPEAT", 2)
        if max_repeat > 0:
            count = st.tool_hashes.get(tool_signature(name, tool_input), 0)
            if count > max_repeat:
                return "repeat", f"Repeated identical '{name}' call ({count}× > {max_repeat}) blocked."

        # 3) too many tool calls this request (runaway loop)
        max_calls = _int_env("LOOP_MAX_TOOL_CALLS", 12)
        if max_calls > 0 and len(st.tool_calls) > max_calls:
            return "max_calls", f"Tool-call limit reached ({len(st.tool_calls)} > {max_calls}); wrap up and answer."

        # 4) token circuit breaker
        max_tokens = _int_env("LOOP_MAX_TOKENS_PER_REQUEST", 20000)
        if max_tokens > 0 and st.tokens > max_tokens:
            return "token_budget", f"Token budget exceeded ({st.tokens} > {max_tokens}); answer with what you have."

        return None
