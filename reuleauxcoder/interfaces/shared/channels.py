"""Agent channels — thread-safe queues bridging agent worker ↔ UI main thread.

Payload dataclasses are included here so every UI target (CLI, TUI,
future VSCode, …) sees the same contract.  Each payload carries a
``meta: dict`` for forward-compatible extension without breaking the
existing structure.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from typing import Any, Literal


# ── Payload dataclasses ─────────────────────────────────────────────────


@dataclass(slots=True)
class StreamToken:
    """Single token emitted during LLM streaming.

    ``type`` is ``"token"`` for normal output or ``"reasoning"`` for
    thinking / chain-of-thought content.
    """

    type: Literal["token", "reasoning"]
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolEvent:
    """Tool execution lifecycle event.

    ``start`` events carry ``name`` and ``args``.
    ``end`` events carry ``name``, ``result``, ``success``.
    """

    event: Literal["start", "end"]
    name: str
    args: dict[str, Any] | None = None      # start
    result: str | None = None               # end
    success: bool = True                    # end
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StateUpdate:
    """Periodic snapshot for sidebar / status bar.

    Emitted by the agent thread whenever significant state changes
    (round advance, token count update, sub-agent spawn/complete).
    """

    active: bool = True
    current_round: int = 0
    tokens_used: int = 0
    tokens_total: int = 0
    active_subagent_count: int = 0
    mode: str | None = None
    model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# ── AgentChannels ────────────────────────────────────────────────────────


@dataclass
class AgentChannels:
    """Thread-safe channels bridging agent worker ↔ UI main thread.

    Direction (relative to the UI):
      ``user_input``     ← UI puts, agent gets
      ``stream_tokens``  ← agent puts, UI gets (polled / async consumed)
      ``tool_events``    ← agent puts, UI gets
      ``approvals``      ← agent puts, UI gets
      ``state_updates``  ← agent puts, UI gets
    """

    user_input: queue.Queue = field(default_factory=queue.Queue)
    stream_tokens: queue.Queue = field(default_factory=queue.Queue)
    tool_events: queue.Queue = field(default_factory=queue.Queue)
    approvals: queue.Queue = field(default_factory=queue.Queue)
    state_updates: queue.Queue = field(default_factory=queue.Queue)
