"""LSP diagnostics injector hook — appends diagnostics before LLM requests.

BEFORE_LLM_REQUEST transform:
- Peeks at accumulated diagnostics blocks owned by the current session
- Renders them as XML diagnostics blocks
- Injects an untrusted diagnostics region into the request-time overlay
- Acknowledges batches only after the request payload was updated successfully

The LspManager reference is bound through the agent's scoped hook registry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.app.ui_events import UIEventKind
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.runtime_overlay import (
    has_runtime_overlay_tail,
    inject_runtime_overlay_region,
)
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    safe_observer_error_type,
)
from reuleauxcoder.extensions.lsp.diagnostic_projection import (
    project_diagnostic_results,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LspDiagnosticsInjectorHook(TransformHook[BeforeLLMRequestContext]):
    """Inject accumulated LSP diagnostics before each LLM request."""

    lsp_manager: LspManager | None = field(default=None)

    def __init__(
        self,
        *,
        lsp_manager: LspManager | None = None,
        priority: int = 100,
    ):
        TransformHook.__init__(
            self,
            name="lsp_diagnostics_injector",
            priority=priority,
            extension_name="core",
        )
        self.lsp_manager = lsp_manager

    @classmethod
    def create_from_config(cls, config: Config) -> LspDiagnosticsInjectorHook:
        """Create hook instance from config.  LspManager injected later."""
        return cls(lsp_manager=None, priority=100)

    def bind_runtime_service(self, name: str, service: object | None) -> None:
        if name == "lsp_manager":
            self.lsp_manager = service  # type: ignore[assignment]

    def clone_for_scope(self, scope: str) -> LspDiagnosticsInjectorHook:
        manager = None if scope == "subagent" else self.lsp_manager
        return LspDiagnosticsInjectorHook(lsp_manager=manager, priority=self.priority)

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        """Drain diagnostics and append to the runtime execution-state tail.

        Appended at the end of the message list (inside the dynamic tail block)
        rather than prepended at index 0, so that prompt-cache prefixes are not
        invalidated by fresh diagnostics after a session resume.
        """
        manager = self.lsp_manager
        if manager is None:
            return context

        if not manager.enabled:
            return context

        # Never claim a batch unless there is a valid request-time overlay to
        # receive it. This prevents a schema migration or malformed request
        # from acknowledging diagnostics that the model never saw.
        if not has_runtime_overlay_tail(context.messages):
            return context

        batches = manager.pending_diagnostic_batches_for_owner(
            agent_id=context.agent_id,
            session_generation=context.session_generation,
            session_id=context.session_id,
        )
        failure_outcomes = manager.pending_diagnostic_failure_outcomes_for_owner(
            agent_id=context.agent_id,
            session_generation=context.session_generation,
            session_id=context.session_id,
        )
        if not batches and not failure_outcomes:
            return context
        consumer_id = (
            f"lsp-inject:{context.agent_id or 'unknown'}:"
            f"{context.session_generation if context.session_generation is not None else 'unknown'}:"
            f"{context.turn_id or 'unknown'}"
        )

        projection = project_diagnostic_results(
            batches, failure_outcomes, manager.config
        )
        if projection.text is None:
            for batch in batches:
                manager.acknowledge_diagnostic_batch(
                    batch.batch_id,
                    consumer_id=f"lsp-filtered:{consumer_id}",
                    carried_forward=(
                        batch.route.turn_id is not None
                        and context.turn_id is not None
                        and batch.route.turn_id != context.turn_id
                    ),
                )
            return context

        # Keep generated diagnostics in their own untrusted-data region before
        # the trusted runtime instruction. The final overlay is volatile by
        # design, so this does not invalidate any earlier stable prefix.
        injection = projection.text
        if not inject_runtime_overlay_region(context.messages, injection):
            return context

        def commit_injection(dispatched: BeforeLLMRequestContext) -> None:
            def remove_injection() -> None:
                for message in dispatched.messages:
                    content = message.get("content")
                    if isinstance(content, str) and injection in content:
                        message["content"] = content.replace(injection, "", 1)
                        dispatched.mark_dispatch_payload_changed()

            if not any(
                injection in str(message.get("content") or "")
                for message in dispatched.messages
            ):
                return
            carried_forward_ids = {
                result.batch_id
                for result in (*batches, *failure_outcomes)
                if result.route.turn_id is not None
                and context.turn_id is not None
                and result.route.turn_id != context.turn_id
            }
            result_ids = tuple(
                result.batch_id for result in (*batches, *failure_outcomes)
            )
            try:
                acknowledged = manager.acknowledge_diagnostic_batches(
                    result_ids,
                    consumer_id=consumer_id,
                    carried_forward_ids=carried_forward_ids,
                )
            except Exception:
                remove_injection()
                raise
            if not acknowledged:
                remove_injection()
                ui_bus = getattr(manager, "ui_bus", None)
                if ui_bus is not None:
                    try:
                        ui_bus.warning(
                            "LSP diagnostics omitted because another request "
                            "already committed the same batch",
                            kind=UIEventKind.SYSTEM,
                            batch_count=len(result_ids),
                        )
                    except Exception as error:  # noqa: BLE001 — UI subscribers do not own delivery
                        logger.warning(
                            "LSP diagnostics race UI observer failed: error_type=%s",
                            safe_observer_error_type(error),
                        )
                return

            # Emit feedback only after the same payload crosses the dispatch
            # boundary; a failed final budget must remain invisible/retryable.
            ui_bus = getattr(manager, "ui_bus", None)
            if ui_bus is None:
                return
            if projection.summary:
                try:
                    ui_bus.info(
                        f"LSP injected: {projection.summary}",
                        kind=UIEventKind.SYSTEM,
                    )
                except Exception as error:  # noqa: BLE001 — UI subscribers do not own delivery
                    logger.warning(
                        "LSP diagnostics UI observer failed: error_type=%s",
                        safe_observer_error_type(error),
                    )

        context.defer_until_dispatch(commit_injection)

        return context
