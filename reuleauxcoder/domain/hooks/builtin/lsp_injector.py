"""LSP diagnostics injector hook — prepends diagnostics before LLM requests.

BEFORE_LLM_REQUEST transform:
- Drains accumulated diagnostics blocks from LspManager
- Renders them as XML diagnostics blocks
- Prepends a synthetic user message to the message list

The LspManager reference is injected post-construction via set_lsp_manager().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint
from reuleauxcoder.extensions.lsp.diagnostics import render_blocks


@register_hook(HookPoint.BEFORE_LLM_REQUEST, priority=100)
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
    def create_from_config(cls, config: "Config") -> "LspDiagnosticsInjectorHook":
        """Create hook instance from config.  LspManager injected later."""
        return cls(lsp_manager=None, priority=100)

    def set_lsp_manager(self, mgr: "LspManager") -> None:
        """Inject the LspManager reference post-construction."""
        self.lsp_manager = mgr

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        """Drain diagnostics and append to the runtime system_context tail.

        Appended at the end of the message list (inside the dynamic tail block)
        rather than prepended at index 0, so that prompt-cache prefixes are not
        invalidated by fresh diagnostics after a session resume.
        """
        if self.lsp_manager is None:
            return context

        if not self.lsp_manager.enabled:
            return context

        blocks = self.lsp_manager.drain_diagnostics()
        if not blocks:
            return context

        rendered = render_blocks(
            blocks,
            max_diagnostics=self.lsp_manager.config.max_diagnostics,
            include_warnings=self.lsp_manager.config.include_warnings,
        )
        if rendered is None:
            return context

        # Append diagnostics to the last message in the list (the runtime
        # <system_context> tail).  The tail changes every turn anyway
        # (time, directory listing, notes), so adding diagnostics here does
        # not cause additional cache breaks.
        if context.messages:
            last = context.messages[-1]
            if last.get("role") == "user" and "</system_context>" in last.get("content", ""):
                last["content"] = last["content"].replace(
                    "</system_context>",
                    "\n[LSP DIAGNOSTICS]\n" + rendered + "\n</system_context>",
                )

        return context
