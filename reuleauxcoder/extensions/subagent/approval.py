"""Approval delegation helpers for sub-agents.

Sub-agents inherit the parent's effective approval provider, but never inherit
one-shot approval results. Requests are attributed to the child and serialized
through the parent interaction surface.
"""

from __future__ import annotations

from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)


class _BubbledApprovalProvider:
    """Attribute and serialize child approvals through the parent provider."""

    def __init__(self, parent_provider, mode: str, task: str):
        self._parent_provider = parent_provider
        self._mode = mode
        self._task = task

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        request.metadata.update(
            {
                "is_subagent": True,
                "subagent_mode": self._mode,
                "subagent_task": self._task,
                "approval_route": "bubble_to_parent",
            }
        )
        request.reason = request.reason or "sub-agent approval request"
        return self._parent_provider.request_approval(request)


def build_subagent_approval_provider(
    parent_agent,
    subagent_mode: str,
    subagent_task: str,
) -> ApprovalProvider | None:
    """Create a ``SharedApprovalProvider`` for a sub-agent.

    The child inherits the effective provider/policy, while every interactive
    request bubbles to the parent's root-scoped coordinator with child
    attribution. The coordinator permits concurrent registration while
    serializing only the human-review focus.
    """
    parent_provider = getattr(parent_agent, "approval_provider", None)
    if parent_provider is None:
        return None

    return _BubbledApprovalProvider(parent_provider, subagent_mode, subagent_task)
