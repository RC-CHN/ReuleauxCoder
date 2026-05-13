"""Built-in hook that injects project-level context files into LLM requests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.hooks.base import ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    HookPoint,
    RunnerStartupContext,
)


# Candidate filenames to search for project context.
# All existing files are loaded and concatenated in this fixed order
# so the KV cache prefix stays stable across requests.
DEFAULT_CONTEXT_FILES = [
    "AGENT.md",
    "AGENTS.md",
    ".agent.md",
    "CLAUDE.md",
    ".claude.md",
]


@register_hook(HookPoint.BEFORE_LLM_REQUEST, priority=50)
class ProjectContextHook(TransformHook[BeforeLLMRequestContext]):
    """Inject project-level context files (AGENT.md, etc.) into messages.

    Searches for project context files in the current working directory
    and injects the content as a separate system message after the main
    system prompt. This enables prompt caching stability since the
    project context remains constant during a session.
    """

    def __init__(
        self,
        *,
        context_files: list[str] | None = None,
        priority: int = 50,
    ):
        super().__init__(
            name="project_context", priority=priority, extension_name="core"
        )
        self.context_files = context_files or DEFAULT_CONTEXT_FILES

    @classmethod
    def create_from_config(cls, config: "Config") -> "ProjectContextHook":
        """Create hook instance from config."""
        return cls(priority=50)

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        parts = self._load_all_project_contexts()
        if parts:
            # Insert after system prompt (index 0), before conversation history.
            # All found files are concatenated in the fixed DEFAULT_CONTEXT_FILES
            # order so the KV cache prefix stays stable.
            context.messages.insert(
                1,
                {
                    "role": "system",
                    "content": self._format_multi_message(parts),
                },
            )
        return context

    def _load_all_project_contexts(self) -> list[tuple[str, str]]:
        """Load all existing project context files from cwd.

        Returns:
            List of (filename, content) tuples in DEFAULT_CONTEXT_FILES order.
            Empty list if no files found.
        """
        cwd = Path.cwd()
        found: list[tuple[str, str]] = []
        for filename in self.context_files:
            candidate = cwd / filename
            if candidate.exists() and candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8").strip()
                    if content:
                        found.append((filename, content))
                except OSError:
                    # Skip files that can't be read
                    continue
        return found

    def _format_multi_message(self, parts: list[tuple[str, str]]) -> str:
        """Format multiple project context files into a single system message."""
        header = (
            "[Project Context]\n"
            "This is project-level context from local file(s) "
            "(e.g. AGENT.md, CLAUDE.md). "
            "It provides project-specific instructions and conventions.\n"
        )
        sections: list[str] = [header]
        for filename, content in parts:
            sections.append(f"--- {filename} ---\n{content}")
        return "\n".join(sections)


@register_hook(HookPoint.RUNNER_STARTUP, priority=0)
class ProjectContextStartupNotifier(ObserverHook[RunnerStartupContext]):
    """Notify the UI when project context files are found at startup."""

    def __init__(self, *, priority: int = 0):
        super().__init__(
            name="project_context_startup_notifier",
            priority=priority,
            extension_name="core",
        )

    @classmethod
    def create_from_config(cls, config: "Config") -> "ProjectContextStartupNotifier":
        """Create hook instance from config."""
        return cls(priority=0)

    def run(self, context: RunnerStartupContext) -> None:
        cwd = Path.cwd()
        found: list[str] = []
        for filename in DEFAULT_CONTEXT_FILES:
            candidate = cwd / filename
            if candidate.exists() and candidate.is_file():
                found.append(filename)
        if found and (ui_bus := (context.metadata or {}).get("ui_bus")):
            try:
                from reuleauxcoder.interfaces.events import UIEventKind

                names = ", ".join(found)
                ui_bus.info(
                    f"Loaded project context: {names}",
                    kind=UIEventKind.CONTEXT,
                )
            except Exception:
                pass
