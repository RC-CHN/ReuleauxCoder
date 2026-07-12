"""Built-in hook that truncates oversized tool output and archives full results."""

from __future__ import annotations

import time
import uuid
import re
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolArchiveReference,
    ToolOutcome,
    ToolRetentionStrategy,
    ToolTruncation,
)
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.infrastructure.fs.paths import get_tool_outputs_dir
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir


@register_hook(HookPoint.AFTER_TOOL_EXECUTE, priority=0)
class ToolOutputTruncationHook(TransformHook[AfterToolExecuteContext]):
    """Archive oversized tool output and replace it with a truncated summary."""

    def __init__(
        self,
        *,
        max_chars: int,
        max_lines: int,
        store_full_output: bool,
        store_dir: str | None = None,
        sessions_dir: str | None = None,
        priority: int = 0,
    ):
        super().__init__(
            name="tool_output_truncation", priority=priority, extension_name="core"
        )
        self.max_chars = max_chars
        self.max_lines = max_lines
        self.store_full_output = store_full_output
        self.output_dir = get_tool_outputs_dir(store_dir)
        self.sessions_dir = (
            Path(sessions_dir).expanduser()
            if sessions_dir
            else get_sessions_dir()
        )

    @classmethod
    def create_from_config(cls, config: "Config") -> "ToolOutputTruncationHook":
        """Create hook instance from config."""
        return cls(
            max_chars=config.tool_output_max_chars,
            max_lines=config.tool_output_max_lines,
            store_full_output=config.tool_output_store_full,
            store_dir=config.tool_output_store_dir,
            sessions_dir=config.session_dir,
            priority=0,
        )

    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        tool_call = context.tool_call
        if tool_call is None:
            return context

        if self._should_bypass_truncation(tool_call.name, tool_call.arguments):
            return context

        outcome = context.outcome or ToolOutcome.from_legacy(context.result)
        result = outcome.model_text
        line_count = len(result.splitlines())
        char_count = len(result)
        if line_count <= self.max_lines and char_count <= self.max_chars:
            return context

        archive_path: Path | None = None
        artifact_ref: str | None = None
        if self.store_full_output:
            archive_path, artifact_ref = self._archive_output(
                tool_call.name,
                result,
                context.round_index,
                session_id=context.session_id,
                tool_call_id=tool_call.id,
            )

        strategy = outcome.retention_hint.strategy
        truncated_text = _retain_text(
            result,
            max_lines=self.max_lines,
            max_chars=self.max_chars,
            strategy=strategy,
        )

        summary_lines = [
            f"[truncated] Tool output exceeded limits ({line_count} lines, {char_count} chars).",
            _retention_summary(
                strategy,
                retained_lines=len(truncated_text.splitlines()),
                max_chars=self.max_chars,
                anchor_line=outcome.retention_hint.anchor_line,
            ),
        ]
        if archive_path is not None:
            if artifact_ref and context.session_id:
                summary_lines.append(f"Full output artifact: {artifact_ref}")
                summary_lines.append(
                    "To recover it, call artifact_read with this session_id and artifact_ref."
                )
            else:
                summary_lines.append(f"Full output saved to: {archive_path}")

        model_projection = (
            "\n".join(summary_lines)
            + "\n\n--- BEGIN TRUNCATED OUTPUT ---\n"
            + truncated_text
            + "\n--- END TRUNCATED OUTPUT ---"
        )
        context.outcome = outcome.with_model_projection(
            model_projection,
            truncation=ToolTruncation(
                original_chars=char_count,
                original_lines=line_count,
                retained_chars=len(truncated_text),
                retained_lines=len(truncated_text.splitlines()),
                strategy=strategy.value,
            ),
            archive_reference=(
                ToolArchiveReference(
                    path=artifact_ref or str(archive_path),
                    checksum_sha256=hashlib.sha256(result.encode("utf-8")).hexdigest(),
                    size_bytes=len(result.encode("utf-8")),
                )
                if archive_path is not None
                else None
            ),
        )
        context.result = context.outcome.model_text
        return context

    def clone_for_scope(self, scope: str) -> "ToolOutputTruncationHook":
        del scope
        return ToolOutputTruncationHook(
            max_chars=self.max_chars,
            max_lines=self.max_lines,
            store_full_output=self.store_full_output,
            store_dir=str(self.output_dir),
            sessions_dir=str(self.sessions_dir),
            priority=self.priority,
        )

    def _archive_output(
        self, tool_name: str, content: str, round_index: int | None,
        *,
        session_id: str | None,
        tool_call_id: str | None,
    ) -> tuple[Path, str | None]:
        safe_session = (
            re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) if session_id else None
        )
        if safe_session:
            artifact_dir = self.sessions_dir / safe_session / "artifacts" / "tools"
            artifact_ref = f"tools/{tool_call_id or uuid.uuid4().hex[:8]}.txt"
            path = self.sessions_dir / safe_session / "artifacts" / artifact_ref
            artifact_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return path, artifact_ref

        day_dir = self.output_dir / time.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        round_part = (
            f"round-{round_index:02d}" if round_index is not None else "round-na"
        )
        filename = f"{round_part}-{tool_name}-{uuid.uuid4().hex[:8]}.txt"
        path = day_dir / filename
        path.write_text(content, encoding="utf-8")
        return path, None

    def _should_bypass_truncation(self, tool_name: str, arguments: dict) -> bool:
        return self._is_override_read(
            tool_name, arguments
        ) or self._is_skills_markdown_read(tool_name, arguments)

    def _is_override_read(self, tool_name: str, arguments: dict) -> bool:
        return tool_name == "read_file" and arguments.get("override") is True

    def _is_skills_markdown_read(self, tool_name: str, arguments: dict) -> bool:
        if tool_name != "read_file":
            return False
        file_path = arguments.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            return False

        try:
            resolved = Path(file_path).expanduser().resolve()
        except OSError:
            return False

        if resolved.suffix.lower() != ".md":
            return False

        roots = [
            (Path.home() / ".rcoder" / "skills").resolve(strict=False),
            (Path.cwd() / ".rcoder" / "skills").resolve(strict=False),
        ]
        for root in roots:
            if resolved == root or resolved.is_relative_to(root):
                return True
        return False


def _retain_text(
    text: str,
    *,
    max_lines: int,
    max_chars: int,
    strategy: ToolRetentionStrategy,
) -> str:
    lines = text.splitlines()
    if strategy is ToolRetentionStrategy.TAIL:
        selected = "\n".join(lines[-max_lines:])
        return selected[-max_chars:].lstrip()
    if strategy is ToolRetentionStrategy.HEAD_TAIL:
        head_count = max(1, (max_lines + 1) // 2)
        tail_count = max(0, max_lines - head_count)
        selected = "\n".join(
            [*lines[:head_count], *(lines[-tail_count:] if tail_count else [])]
        )
        if len(selected) <= max_chars:
            return selected
        head_chars = max(1, (max_chars + 1) // 2)
        tail_chars = max_chars - head_chars
        if not tail_chars:
            return selected[:head_chars].rstrip()
        return (
            selected[:head_chars].rstrip()
            + "\n"
            + selected[-tail_chars:].lstrip()
        )
    selected = "\n".join(lines[:max_lines])
    return selected[:max_chars].rstrip()


def _retention_summary(
    strategy: ToolRetentionStrategy,
    *,
    retained_lines: int,
    max_chars: int,
    anchor_line: int | None,
) -> str:
    direction = {
        ToolRetentionStrategy.HEAD: "first",
        ToolRetentionStrategy.TAIL: "last",
        ToolRetentionStrategy.HEAD_TAIL: "first/last",
    }[strategy]
    anchor = f" from source line {anchor_line}" if anchor_line is not None else ""
    return (
        f"Showing {direction} {retained_lines} retained lines{anchor} "
        f"and up to {max_chars} chars."
    )
