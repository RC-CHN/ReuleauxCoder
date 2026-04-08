"""Built-in hook that evaluates tool policies before tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from reuleauxcoder.domain.hooks.base import GuardHook
from reuleauxcoder.domain.hooks.types import BeforeToolExecuteContext, GuardDecision
from reuleauxcoder.extensions.tools.policies import DEFAULT_TOOL_POLICIES, ToolPolicy


@dataclass(slots=True)
class ToolPolicyGuardHook(GuardHook[BeforeToolExecuteContext]):
    """Run configured tool policies before a tool executes."""

    policies: tuple[ToolPolicy, ...] = field(default_factory=lambda: DEFAULT_TOOL_POLICIES)

    def __init__(
        self,
        *,
        policies: tuple[ToolPolicy, ...] | None = None,
        priority: int = 0,
    ):
        super().__init__(name="tool_policy_guard", priority=priority, extension_name="core")
        self.policies = policies or DEFAULT_TOOL_POLICIES

    def run(self, context: BeforeToolExecuteContext) -> GuardDecision:
        tool_call = context.tool_call
        if tool_call is None:
            return GuardDecision.allow()

        warnings: list[str] = []
        for policy in self.policies:
            decision = policy.evaluate(tool_call)
            if decision is None:
                continue
            if not decision.allowed:
                return decision
            if decision.warning:
                warnings.append(decision.warning)

        if warnings:
            return GuardDecision.warn("; ".join(warnings))
        return GuardDecision.allow()
