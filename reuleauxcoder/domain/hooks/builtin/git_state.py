"""Inject a compact Git working-state summary into the dynamic overlay."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.infrastructure.version_control import GitMonitor

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.runtime_overlay import (
    has_runtime_overlay_tail,
    inject_runtime_overlay_region,
)
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext


def render_git_snapshot(snapshot: dict[str, Any]) -> str:
    """Render the internal snapshot as lightly structured, readable text."""
    if snapshot.get("reason") == "not_initialized":
        return "git: not initialized in current workspace"
    if snapshot.get("reason") == "git_not_installed":
        return "git: unavailable — executable not found on this system"
    lines = [f"repository: {snapshot.get('repository_root') or 'unknown'}"]
    if not snapshot.get("available"):
        reason = snapshot.get("reason") or "unavailable"
        suffix = " (scan truncated)" if snapshot.get("truncated") else ""
        lines.append(f"status: unavailable — {reason}{suffix}")
        return "\n".join(lines)

    lines.append(f"branch: {snapshot.get('branch') or 'unknown'}")
    lines.append(f"head: {snapshot.get('head') or 'no commits'}")
    changes = dict(snapshot.get("changes") or {})
    any_changes = False
    for category in ("staged", "unstaged", "untracked"):
        bucket = dict(changes.get(category) or {})
        count = bucket.get("count", 0)
        items = list(bucket.get("items") or [])
        if count in {0, "0", ">=0"} and not items:
            continue
        any_changes = True
        lines.append(f"{category} ({count}):")
        lines.extend(f"  - {item}" for item in items)
    if not any_changes:
        lines.append("working tree: clean")
    if snapshot.get("status_output_truncated"):
        lines.append("status scan: truncated; displayed counts are lower bounds")

    notice = snapshot.get("head_change")
    if isinstance(notice, dict):
        kind = str(notice.get("kind") or "changed")
        source = notice.get("from")
        target = notice.get("to")
        transition = f" {source} -> {target}" if source or target else ""
        count = notice.get("count")
        count_text = f", {count} commit(s)" if count is not None else ""
        lines.append(f"head change: {kind}{transition}{count_text}")
        lines.extend(f"  - {item}" for item in notice.get("commits") or [])
    return "\n".join(lines)


@dataclass(slots=True)
class GitStateInjectorHook(TransformHook[BeforeLLMRequestContext]):
    """Append bounded Git observations at the volatile request tail."""

    git_monitor: GitMonitor | None = field(default=None)

    def __init__(
        self,
        *,
        git_monitor: GitMonitor | None = None,
        priority: int = 90,
    ) -> None:
        TransformHook.__init__(
            self,
            name="git_state_injector",
            priority=priority,
            extension_name="core",
        )
        self.git_monitor = git_monitor

    @classmethod
    def create_from_config(cls, config: "Config") -> "GitStateInjectorHook":
        del config
        return cls(priority=90)

    def bind_runtime_service(self, name: str, service: object | None) -> None:
        if name == "git_monitor":
            self.git_monitor = service  # type: ignore[assignment]

    def clone_for_scope(self, scope: str) -> "GitStateInjectorHook":
        # Child workers receive task-scoped context and may run in another
        # process/worktree. The root monitor must never be shared into them.
        del scope
        return GitStateInjectorHook(git_monitor=None, priority=self.priority)

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        monitor = self.git_monitor
        if monitor is None or not has_runtime_overlay_tail(context.messages):
            return context
        try:
            snapshot = monitor.snapshot(turn_id=context.turn_id)
            if snapshot is None:
                return context
            rendered_text = render_git_snapshot(snapshot)
            if len(rendered_text) > 2_400:
                rendered_text = render_git_snapshot(
                    monitor.compact(snapshot) or snapshot
                )
            rendered = escape(rendered_text, quote=False)
            region = (
                "[GIT STATE]\n"
                '<git_state trust="untrusted_data">\n'
                f"{rendered}\n"
                "</git_state>\n"
            )
            inject_runtime_overlay_region(context.messages, region)
        except Exception:
            # Observation is optional. A Git or filesystem failure must never
            # block an otherwise valid model request.
            return context
        return context
