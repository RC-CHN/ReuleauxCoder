"""Built-in hook that injects project-level context files into LLM requests."""

from __future__ import annotations

from pathlib import Path
import stat
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.hooks.base import ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    HookContextSnapshot,
    RunnerStartupContext,
)
from reuleauxcoder.domain.llm.context_messages import synthetic_user_message


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


class ProjectContextHook(TransformHook[BeforeLLMRequestContext]):
    """Inject project-level context files (AGENT.md, etc.) into messages.

    Searches for project context files in the current working directory
    and injects the content as a tagged synthetic user message after the
    sole system prompt. This keeps the provider-visible system prefix singular.
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
        self._cache_lock = threading.Lock()
        self._cache_signature: tuple | None = None
        self._cache_parts: tuple[tuple[str, str], ...] = ()
        self._cache_rendered: str | None = None

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
                synthetic_user_message(
                    "project_context",
                    self._format_multi_message(parts),
                    source="workspace_instruction_files",
                    attributes={
                        "files": ",".join(filename for filename, _ in parts)
                    },
                ),
            )
        return context

    def clone_for_scope(self, scope: str) -> "ProjectContextHook":
        del scope
        return ProjectContextHook(
            context_files=list(self.context_files), priority=self.priority
        )

    def _load_all_project_contexts(self) -> list[tuple[str, str]]:
        """Load all existing project context files from cwd.

        Returns:
            List of (filename, content) tuples in DEFAULT_CONTEXT_FILES order.
            Empty list if no files found.
        """
        cwd = Path.cwd().resolve(strict=False)
        with self._cache_lock:
            signature = self._context_signature(cwd)
            if signature == self._cache_signature:
                return list(self._cache_parts)

            found: list[tuple[str, str]] = []
            for filename in self.context_files:
                candidate = cwd / filename
                file_signature = next(
                    (
                        item
                        for item in signature[1]
                        if item[0] == filename
                    ),
                    None,
                )
                if file_signature is None or file_signature[1] is None:
                    continue
                try:
                    content = candidate.read_text(encoding="utf-8").strip()
                    if content:
                        found.append((filename, content))
                except OSError:
                    # Skip files that can't be read
                    continue

            # Do not retain a value if a file changed while it was being read.
            verified_signature = self._context_signature(cwd)
            if verified_signature == signature:
                self._cache_signature = signature
                self._cache_parts = tuple(found)
                self._cache_rendered = None
            else:
                self._cache_signature = None
                self._cache_parts = ()
                self._cache_rendered = None
            return found

    def _context_signature(self, cwd: Path) -> tuple:
        files: list[tuple[str, tuple[int, int, int, int] | None]] = []
        for filename in self.context_files:
            try:
                status = (cwd / filename).stat()
            except OSError:
                files.append((filename, None))
                continue
            if not stat.S_ISREG(status.st_mode):
                files.append((filename, None))
                continue
            files.append(
                (
                    filename,
                    (
                        status.st_ino,
                        status.st_size,
                        status.st_mtime_ns,
                        status.st_ctime_ns,
                    ),
                )
            )
        return (str(cwd), tuple(files))

    def _format_multi_message(self, parts: list[tuple[str, str]]) -> str:
        """Format multiple project context files into a single system message."""
        parts_key = tuple(parts)
        with self._cache_lock:
            if parts_key == self._cache_parts and self._cache_rendered is not None:
                return self._cache_rendered
        header = (
            "[Project Context]\n"
            "This is project-level context from local file(s) "
            "(e.g. AGENT.md, CLAUDE.md). "
            "It provides project-specific instructions and conventions.\n"
        )
        sections: list[str] = [header]
        for filename, content in parts:
            sections.append(f"--- {filename} ---\n{content}")
        rendered = "\n".join(sections)
        with self._cache_lock:
            if parts_key == self._cache_parts:
                self._cache_rendered = rendered
        return rendered


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

    def run(self, context: HookContextSnapshot) -> None:
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

    def clone_for_scope(self, scope: str) -> "ProjectContextStartupNotifier":
        del scope
        return ProjectContextStartupNotifier(priority=self.priority)
