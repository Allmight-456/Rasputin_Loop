"""Step-level tracing for Loop (Pillar 02 — observability).

Turns each agent invocation from a black box into a visible sequence of steps:
the model's *thinking*, every *model API call*, and every *tool call* with its
inputs/outputs. Implemented as a Strands `HookProvider` plus a reasoning
callback, so it observes the real event loop without changing behaviour.

Each step is:
  - logged as one JSON line on the ``loop.trace`` logger (compact at INFO, with
    full reasoning + tool I/O at DEBUG / ``LOG_LEVEL=DEBUG``), and
  - persisted via ``observability.record_step`` to the ``steps`` table.

Set ``LOOP_TRACE=off`` to silence the log lines (rows are still written).

The tracer also OWNS the per-request counters in `RequestState` (model_calls,
tool_calls, tool_hashes, tokens, reasoning). The guardrails read those counters
to make enforcement decisions, so the tracer is always registered.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from loop import context
from loop import observability as obs

log = logging.getLogger("loop.trace")

_DETAIL_MAX = 800


def _trace_enabled() -> bool:
    return os.environ.get("LOOP_TRACE", "on").strip().lower() not in {"off", "0", "false", "no"}


def tool_signature(name: str, tool_input: Any) -> str:
    """Stable signature for a tool call (name + normalized input) — used by the
    tracer for repeat counting and by guardrails for repeat detection."""
    try:
        norm = json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        norm = str(tool_input)
    return f"{name}:{norm}"


def _truncate(value: Any, limit: int = _DETAIL_MAX) -> Any:
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"


def _log_step(payload: dict) -> None:
    if _trace_enabled():
        log.info("step %s", json.dumps(payload, ensure_ascii=False, default=str))


class LoopTracer(HookProvider):
    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(AfterModelCallEvent, self._after_model)
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)
        registry.add_callback(AfterInvocationEvent, self._after_invocation)

    # --- model calls -------------------------------------------------------
    def _before_model(self, event: BeforeModelCallEvent) -> None:
        st = context.current()
        if st is not None:
            st.timers["__model__"] = time.perf_counter()

    def _after_model(self, event: AfterModelCallEvent) -> None:
        st = context.current()
        if st is None:
            return
        st.model_calls += 1
        # live cumulative token total off the agent's running metrics
        try:
            acc = event.agent.event_loop_metrics.accumulated_usage
            st.tokens = int(acc.get("totalTokens", st.tokens) or st.tokens)
        except Exception:  # noqa: BLE001
            pass
        t0 = st.timers.pop("__model__", None)
        dur = int((time.perf_counter() - t0) * 1000) if t0 else 0
        stop_reason = getattr(getattr(event, "stop_response", None), "stop_reason", None)
        outcome = "error" if event.exception else "ok"
        seq = st.next_seq()
        _log_step({
            "request_id": st.request_id, "seq": seq, "kind": "model",
            "name": os.environ.get("ANTHROPIC_MODEL", "model"),
            "stop_reason": stop_reason, "cumulative_tokens": st.tokens,
            "duration_ms": dur, "outcome": outcome,
        })
        obs.record_step(
            st.request_id, seq, "model",
            os.environ.get("ANTHROPIC_MODEL", "model"),
            detail={"stop_reason": stop_reason, "exception": str(event.exception) if event.exception else None},
            tokens=st.tokens, duration_ms=dur, outcome=outcome,
        )

    # --- tool calls --------------------------------------------------------
    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        st = context.current()
        if st is None:
            return
        tu = event.tool_use or {}
        name = tu.get("name", "?")
        st.tool_calls.append(name)
        st.tool_hashes[tool_signature(name, tu.get("input"))] += 1
        tuid = tu.get("toolUseId", name)
        st.timers[tuid] = time.perf_counter()

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        st = context.current()
        if st is None:
            return
        tu = event.tool_use or {}
        name = tu.get("name", "?")
        tuid = tu.get("toolUseId", name)
        t0 = st.timers.pop(tuid, None)
        dur = int((time.perf_counter() - t0) * 1000) if t0 else 0
        outcome = "error" if event.exception else "ok"
        seq = st.next_seq()
        line = {
            "request_id": st.request_id, "seq": seq, "kind": "tool",
            "name": name, "duration_ms": dur, "outcome": outcome,
        }
        if log.isEnabledFor(logging.DEBUG):
            line["input"] = _truncate(tu.get("input"))
            line["result"] = _truncate(event.result)
        _log_step(line)
        obs.record_step(
            st.request_id, seq, "tool", name,
            detail={
                "input": _truncate(tu.get("input")),
                "result": _truncate(event.result),
                "exception": str(event.exception) if event.exception else None,
            },
            duration_ms=dur, outcome=outcome,
        )

    # --- invocation close-out (persist accumulated reasoning once) ---------
    def _after_invocation(self, event: AfterInvocationEvent) -> None:
        st = context.current()
        if st is None or not st.reasoning:
            return
        reasoning = "\n".join(st.reasoning).strip()
        if not reasoning:
            return
        seq = st.next_seq()
        _log_step({
            "request_id": st.request_id, "seq": seq, "kind": "reasoning",
            "chars": len(reasoning),
            **({"text": _truncate(reasoning)} if log.isEnabledFor(logging.DEBUG) else {}),
        })
        obs.record_step(st.request_id, seq, "reasoning", "thinking", detail=_truncate(reasoning, 4000))


def reasoning_callback(**kwargs: Any) -> None:
    """Custom callback_handler: capture the model's reasoning ("thinking").

    Strands streams `reasoningText` chunks here; we accumulate them on the active
    RequestState and (at DEBUG) log them live. We deliberately do not print the
    `data` text chunks — Slack receives the final reply via the normal path.
    """
    rt = kwargs.get("reasoningText")
    if not rt:
        return
    st = context.current()
    if st is not None:
        st.reasoning.append(rt)
    if _trace_enabled() and log.isEnabledFor(logging.DEBUG):
        log.debug("reasoning %s", rt)
