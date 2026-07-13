"""Stable internal extension ports.

These protocols separate control-flow authority from optional observation.
The core runtime owns stage ordering; extensions only contribute through the
smallest applicable capability.
"""

from __future__ import annotations

from typing import Protocol

from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    GuardDecision,
    HookContext,
    HookDiagnostic,
    HookPoint,
)


class AuthorizationPolicy(Protocol):
    def authorize_tool(
        self, context: BeforeToolExecuteContext
    ) -> tuple[GuardDecision, ...]: ...


class ContextContributor(Protocol):
    def contribute_tool_context(
        self, context: BeforeToolExecuteContext
    ) -> BeforeToolExecuteContext: ...


class OutcomeProcessor(Protocol):
    def process_tool_outcome(
        self, context: AfterToolExecuteContext
    ) -> AfterToolExecuteContext: ...


class RuntimeObserver(Protocol):
    def observe(
        self, hook_point: HookPoint, context: HookContext
    ) -> tuple[HookDiagnostic, ...]: ...


class LifecycleParticipant(Protocol):
    def start(self) -> None: ...

    def dispose(self) -> None: ...


class ToolExtensionRuntime(
    AuthorizationPolicy,
    ContextContributor,
    OutcomeProcessor,
    RuntimeObserver,
    Protocol,
):
    """Composite dependency consumed by the fixed core tool pipeline."""
