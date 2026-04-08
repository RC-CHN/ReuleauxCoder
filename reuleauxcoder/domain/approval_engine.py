"""Approval policy engine for tool execution decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from reuleauxcoder.domain.config.models import ApprovalAction, ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.domain.llm.models import ToolCall

ToolSource = Literal["builtin", "mcp", "unknown"]


@dataclass(slots=True)
class ToolApprovalContext:
    """Structured input for approval policy evaluation."""

    tool_call: ToolCall
    tool_name: str
    tool_source: ToolSource = "unknown"
    mcp_server: str | None = None
    effect_class: str | None = None
    profile: str | None = None
    tool_description: str | None = None
    tool_schema: dict[str, Any] | None = None


@dataclass(slots=True)
class ApprovalPolicyMatch:
    """Resolved approval action with optional matched rule."""

    action: ApprovalAction
    rule: ApprovalRuleConfig | None = None


class ApprovalPolicyEngine:
    """Evaluate approval actions from config-driven rules."""

    def __init__(self, config: ApprovalConfig):
        self.config = config

    def evaluate(self, context: ToolApprovalContext) -> ApprovalPolicyMatch:
        """Resolve the approval action for a tool context."""
        ranked_rules = sorted(self.config.rules, key=lambda rule: self._specificity(rule), reverse=True)
        for rule in ranked_rules:
            if self._matches(rule, context):
                return ApprovalPolicyMatch(action=rule.action, rule=rule)
        return ApprovalPolicyMatch(action=self.config.default_mode, rule=None)

    @staticmethod
    def _specificity(rule: ApprovalRuleConfig) -> int:
        """Rank rules by specificity so narrower MCP/tool rules override broader ones.

        Higher score means a more specific rule. In practice this gives the
        desired precedence of tool-level MCP rules over server-level rules,
        and server-level rules over generic `tool_source = mcp` rules.
        """
        score = 0
        if rule.tool_source is not None:
            score += 1
        if rule.mcp_server is not None:
            score += 2
        if rule.tool_name is not None:
            score += 4
        if rule.effect_class is not None:
            score += 1
        if rule.profile is not None:
            score += 1
        return score

    @staticmethod
    def _matches(rule: ApprovalRuleConfig, context: ToolApprovalContext) -> bool:
        if rule.tool_name is not None and rule.tool_name != context.tool_name:
            return False
        if rule.tool_source is not None and rule.tool_source != context.tool_source:
            return False
        if rule.mcp_server is not None and rule.mcp_server != context.mcp_server:
            return False
        if rule.effect_class is not None and rule.effect_class != context.effect_class:
            return False
        if rule.profile is not None and rule.profile != context.profile:
            return False
        return True

