"""Base interfaces for tool execution policies."""

from __future__ import annotations

from typing import Protocol

from reuleauxcoder.domain.hooks.types import GuardDecision
from reuleauxcoder.domain.llm.models import ToolCall


class ToolPolicy(Protocol):
    """Policy interface for validating tool calls before execution."""

    def evaluate(self, tool_call: ToolCall) -> GuardDecision | None:
        """Return a decision when the policy applies, else None."""
