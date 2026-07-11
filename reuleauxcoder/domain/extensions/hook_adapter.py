"""Compatibility adapter from legacy hooks to typed extension ports."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    GuardDecision,
    HookContext,
    HookDiagnostic,
    HookPoint,
)


@dataclass(frozen=True, slots=True)
class HookExtensionAdapter:
    """Keep legacy hook discovery behind the new internal port boundary."""

    registry: HookRegistry

    def authorize_tool(
        self, context: BeforeToolExecuteContext
    ) -> tuple[GuardDecision, ...]:
        return tuple(
            self.registry.run_guards(HookPoint.BEFORE_TOOL_EXECUTE, context)
        )

    def contribute_tool_context(
        self, context: BeforeToolExecuteContext
    ) -> BeforeToolExecuteContext:
        authorized_signature = self._tool_call_signature(context.tool_call)
        working_context = deepcopy(context)
        result = self.registry.run_transforms(
            HookPoint.BEFORE_TOOL_EXECUTE, working_context
        )
        if not isinstance(result, BeforeToolExecuteContext):
            raise TypeError("before-tool contributor returned an invalid context")
        if self._tool_call_signature(result.tool_call) != authorized_signature:
            raise ValueError(
                "context contributors cannot modify the authorized tool call"
            )
        return result

    @staticmethod
    def _tool_call_signature(tool_call) -> tuple | None:
        if tool_call is None:
            return None
        return (tool_call.id, tool_call.name, deepcopy(tool_call.arguments))

    def process_tool_outcome(
        self, context: AfterToolExecuteContext
    ) -> AfterToolExecuteContext:
        result = self.registry.run_transforms(HookPoint.AFTER_TOOL_EXECUTE, context)
        if not isinstance(result, AfterToolExecuteContext):
            raise TypeError("tool outcome processor returned an invalid context")
        return result

    def observe(
        self, hook_point: HookPoint, context: HookContext
    ) -> tuple[HookDiagnostic, ...]:
        return self.registry.run_observers(hook_point, context)
