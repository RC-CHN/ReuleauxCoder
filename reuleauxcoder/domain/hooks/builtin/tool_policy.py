"""Built-in hook that evaluates tool policies before tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from reuleauxcoder.domain.approval_engine import ApprovalPolicyEngine, ToolApprovalContext
from reuleauxcoder.domain.config.models import ApprovalConfig
from reuleauxcoder.domain.hooks.base import GuardHook
from reuleauxcoder.domain.hooks.types import BeforeToolExecuteContext, GuardDecision
from reuleauxcoder.extensions.tools.policies import DEFAULT_TOOL_POLICIES, ToolPolicy


@dataclass(slots=True)
class ToolPolicyGuardHook(GuardHook[BeforeToolExecuteContext]):
    """Run configured tool policies before a tool executes."""

    policies: tuple[ToolPolicy, ...] = field(default_factory=lambda: DEFAULT_TOOL_POLICIES)
    approval_engine: ApprovalPolicyEngine | None = None

    def __init__(
        self,
        *,
        policies: tuple[ToolPolicy, ...] | None = None,
        approval_config: ApprovalConfig | None = None,
        priority: int = 0,
    ):
        GuardHook.__init__(
            self,
            name="tool_policy_guard",
            priority=priority,
            extension_name="core",
        )
        self.policies = policies or DEFAULT_TOOL_POLICIES
        self.approval_engine = (
            ApprovalPolicyEngine(approval_config) if approval_config is not None else None
        )

    def update_approval_config(self, approval_config: ApprovalConfig) -> None:
        """Replace approval config for live runtime updates."""
        self.approval_engine = ApprovalPolicyEngine(approval_config)

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
            if decision.requires_approval:
                return decision

        if self.approval_engine is not None:
            metadata = context.metadata or {}
            approval_context = ToolApprovalContext(
                tool_call=tool_call,
                tool_name=tool_call.name,
                tool_source=metadata.get("tool_source", _infer_tool_source(tool_call.name)),
                mcp_server=metadata.get("mcp_server"),
                tool_description=metadata.get("tool_description"),
                tool_schema=metadata.get("tool_schema"),
            )
            match = self.approval_engine.evaluate(approval_context)
            if match.action == "deny":
                return GuardDecision.deny(
                    f"Tool '{tool_call.name}' blocked by approval policy engine"
                )
            if match.action == "warn":
                warnings.append(f"Tool '{tool_call.name}' matched warning approval policy")
            elif match.action == "require_approval":
                return GuardDecision.require_approval(
                    f"Tool '{tool_call.name}' requires approval by policy"
                )

        if warnings:
            return GuardDecision.warn("; ".join(warnings))
        return GuardDecision.allow()


BUILTIN_TOOL_NAMES = {
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "agent",
}


def _infer_tool_source(tool_name: str) -> str:
    if tool_name in BUILTIN_TOOL_NAMES:
        return "builtin"
    return "mcp"
