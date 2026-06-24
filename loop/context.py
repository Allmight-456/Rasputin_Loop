"""Per-request state shared across the agent's hooks.

One `RequestState` exists for the duration of a single `agent.run()` call. It is
stored in a `ContextVar` so the tracing + guardrail hooks (which Strands invokes
deep inside the event loop) can attribute every model call, tool call, and
reasoning chunk to the right request — and so guardrails can enforce per-request
limits (counts, repeats, token budget).

`agent.run()` owns the lifecycle: it sets the state on entry and resets it on
exit. Works identically whether the call originates from Slack or the eval CLI;
when no id is supplied a fresh one is generated.
"""
from __future__ import annotations

import contextvars
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RequestState:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # counters / accumulators, mutated by the tracing + guardrail hooks
    model_calls: int = 0
    tool_calls: list[str] = field(default_factory=list)   # ordered tool names (incl. repeats/blocked attempts)
    tool_hashes: Counter = field(default_factory=Counter)  # name+input signature -> attempt count
    tokens: int = 0                                        # live cumulative total (from event_loop_metrics)
    guardrail_hits: int = 0
    reasoning: list[str] = field(default_factory=list)     # model "thinking" chunks, in order

    _seq: int = 0
    timers: dict[str, float] = field(default_factory=dict)  # toolUseId -> perf_counter() start
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def next_seq(self) -> int:
        with self.lock:
            self._seq += 1
            return self._seq


_CURRENT: contextvars.ContextVar[RequestState | None] = contextvars.ContextVar(
    "loop_request_state", default=None
)


def current() -> RequestState | None:
    return _CURRENT.get()


def set_current(state: RequestState) -> contextvars.Token:
    return _CURRENT.set(state)


def reset_current(token: contextvars.Token) -> None:
    _CURRENT.reset(token)
