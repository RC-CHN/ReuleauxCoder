"""Built-in hook that injects project-level context files into LLM requests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint


# Candidate filenames to search for project context, in priority order
DEFAULT_CONTEXT_FILES = [
    "AGENT.md",
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
        super().__init__(name="project_context", priority=priority, extension_name="core")
        self.context_files = context_files or DEFAULT_CONTEXT_FILES

    @classmethod
    def create_from_config(cls, config: "Config") -> "ProjectContextHook":
        """Create hook instance from config."""
        return cls(priority=50)

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        content = self._load_project_context()
        if content:
            # Insert after system prompt (index 0), before conversation history
            # This ensures stable prefix for KV cache matching
            context.messages.insert(1, {
                "role": "system",
                "content": self._format_message(content),
            })
        return context

    def _load_project_context(self) -> str | None:
        """Load the first found project context file from cwd."""
        cwd = Path.cwd()
        for filename in self.context_files:
            candidate = cwd / filename
            if candidate.exists() and candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8").strip()
                except OSError:
                    # Skip files that can't be read
                    continue
        return None

    def _format_message(self, content: str) -> str:
        """Format project context as a system message."""
        return (
            "[Project Context]\n"
            "This is project-level context from a local file (e.g., AGENT.md). "
            "It provides project-specific instructions and conventions.\n"
            f"{content}"
        )