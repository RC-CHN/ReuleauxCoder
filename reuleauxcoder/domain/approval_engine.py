"""Approval policy engine for tool execution decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from reuleauxcoder.domain.config.models import (
    ApprovalAction,
    ApprovalConfig,
    ApprovalRuleConfig,
)
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
    subjects: tuple[str, ...] = ()
    scope_key: str | None = None


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
        ranked_rules = sorted(
            self.config.rules, key=lambda rule: self._specificity(rule), reverse=True
        )
        if not context.subjects:
            for rule in ranked_rules:
                if self._matches_dimensions(rule, context) and rule.pattern is None:
                    return ApprovalPolicyMatch(action=rule.action, rule=rule)
            return ApprovalPolicyMatch(
                action=self._fallback_action(context),
                rule=None,
            )

        # Resolve the most specific policy independently for every stable
        # resource identity. A multi-resource call is then governed by the
        # strictest resulting action. This lets several exact session grants
        # collectively cover a later multi-file call without letting one exact
        # grant authorize unrelated subjects from the same invocation.
        matches: list[ApprovalPolicyMatch] = []
        for subject in context.subjects:
            match = next(
                (
                    ApprovalPolicyMatch(action=rule.action, rule=rule)
                    for rule in ranked_rules
                    if self._matches_dimensions(rule, context)
                    and self._matches_subject(rule.pattern, subject)
                ),
                ApprovalPolicyMatch(
                    action=self._fallback_action(context),
                    rule=None,
                ),
            )
            matches.append(match)

        action_priority = {
            "allow": 0,
            "warn": 1,
            "require_approval": 2,
            "deny": 3,
        }
        return max(matches, key=lambda match: action_priority[match.action])

    def _fallback_action(self, context: ToolApprovalContext) -> ApprovalAction:
        if context.effect_class in {"read_only_internal", "control_plane_internal"}:
            return "allow"
        return self.config.default_mode

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
        if rule.pattern is not None:
            if rule.pattern == "*":
                score += 1
            elif rule.pattern.endswith("/**"):
                score += 2
            else:
                score += 3
        if rule.scope_key is not None:
            score += 2
        return score

    @staticmethod
    def _matches_dimensions(
        rule: ApprovalRuleConfig, context: ToolApprovalContext
    ) -> bool:
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
        if rule.scope_key is not None and rule.scope_key != context.scope_key:
            return False
        return True

    @staticmethod
    def _matches_subject(pattern: str | None, subject: str) -> bool:
        if pattern is None or pattern == "*":
            return True
        if pattern.endswith("/**"):
            base = pattern[:-3].rstrip("/")
            return subject == base or subject.startswith(base + "/")
        return subject == pattern


def approval_pattern_matches(pattern: str | None, subject: str) -> bool:
    """Public matcher shared by policy evaluation and grant queue coverage."""
    return ApprovalPolicyEngine._matches_subject(pattern, subject)


__all__ = [
    "ApprovalPolicyEngine",
    "ApprovalPolicyMatch",
    "ToolApprovalContext",
    "ToolSource",
    "approval_pattern_matches",
]
