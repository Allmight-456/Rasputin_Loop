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
import os
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RequestState:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # provenance of the originating Slack message (who / where / which app).
    # Set by agent.run() from the Slack envelope so the memory store can stamp
    # every saved memory with who provided it and where — the `add_memory` tool
    # only lets the model pass content, so provenance must be attached here.
    author: str | None = None       # Slack user id of the asker
    channel: str | None = None      # Slack channel id
    team: str | None = None         # Slack team/workspace id
    source: str | None = None       # which Loop app handled it (e.g. "rasputin")
    thread_ts: str | None = None    # thread the message belongs to

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

    def provenance(self) -> dict[str, str]:
        """The non-empty provenance fields, as a flat dict for memory metadata."""
        fields = {
            "author": self.author,
            "channel": self.channel,
            "team": self.team,
            "source": self.source,
            "thread_ts": self.thread_ts,
        }
        return {k: v for k, v in fields.items() if v}


_CURRENT: contextvars.ContextVar[RequestState | None] = contextvars.ContextVar(
    "loop_request_state", default=None
)


def current() -> RequestState | None:
    return _CURRENT.get()


def set_current(state: RequestState) -> contextvars.Token:
    return _CURRENT.set(state)


def reset_current(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def memory_scope() -> tuple[str, str] | None:
    """The active memory-recall scope as ``(column, value)``, or ``None`` for no
    filter (global recall).

    This is what makes channel-isolated memory possible (Claude-Tag-style): a fact
    saved in #support is not recalled in #data-team. Resolution order:
    ``LOOP_MEMORY_SCOPE_<SOURCE>`` (per-app override, SOURCE = the Loop app name)
    → ``LOOP_MEMORY_SCOPE`` → default ``channel``. Values:

    * ``channel`` (default) — scope to the originating channel.
    * ``team``              — scope to the workspace/team.
    * ``global``            — no filter (legacy behavior).

    Returns ``None`` (no filter) when scope is ``global``, when there's no active
    request, or when the scoping column is empty on the request — so recall never
    silently returns nothing just because provenance was missing.
    """
    state = current()
    if state is None:
        return None
    source = (state.source or "").upper()
    scope = (
        os.environ.get(f"LOOP_MEMORY_SCOPE_{source}")
        or os.environ.get("LOOP_MEMORY_SCOPE")
        or "channel"
    ).strip().lower()
    if scope == "global":
        return None
    if scope == "team":
        return ("team", state.team) if state.team else None
    return ("channel", state.channel) if state.channel else None
