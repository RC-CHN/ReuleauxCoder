"""Built-in hook that truncates oversized tool output and archives full results."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.infrastructure.fs.paths import get_tool_outputs_dir


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
        priority: int = 0,
    ):
        super().__init__(name="tool_output_truncation", priority=priority, extension_name="core")
        self.max_chars = max_chars
        self.max_lines = max_lines
        self.store_full_output = store_full_output
        self.output_dir = get_tool_outputs_dir(store_dir)

    @classmethod
    def create_from_config(cls, config: "Config") -> "ToolOutputTruncationHook":
        """Create hook instance from config."""
        return cls(
            max_chars=config.tool_output_max_chars,
            max_lines=config.tool_output_max_lines,
            store_full_output=config.tool_output_store_full,
            store_dir=config.tool_output_store_dir,
            priority=0,
        )

    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        tool_call = context.tool_call
        if tool_call is None:
            return context

        if self._should_bypass_truncation(tool_call.name, tool_call.arguments):
            return context

        result = context.result
        line_count = len(result.splitlines())
        char_count = len(result)
        if line_count <= self.max_lines and char_count <= self.max_chars:
            return context

        archive_path: Path | None = None
        if self.store_full_output:
            archive_path = self._archive_output(tool_call.name, result, context.round_index)

        truncated_lines = result.splitlines()[: self.max_lines]
        truncated_text = "\n".join(truncated_lines)
        if len(truncated_text) > self.max_chars:
            truncated_text = truncated_text[: self.max_chars].rstrip()

        summary_lines = [
            f"[truncated] Tool output exceeded limits ({line_count} lines, {char_count} chars).",
            f"Showing first {min(line_count, self.max_lines)} lines and up to {self.max_chars} chars.",
        ]
        if archive_path is not None:
            summary_lines.append(f"Full output saved to: {archive_path}")
            summary_lines.append(
                "To recover the full archived output, call read_file on that path with override=true."
            )

        context.result = (
            "\n".join(summary_lines)
            + "\n\n--- BEGIN TRUNCATED OUTPUT ---\n"
            + truncated_text
            + "\n--- END TRUNCATED OUTPUT ---"
        )
        return context

    def _archive_output(self, tool_name: str, content: str, round_index: int | None) -> Path:
        day_dir = self.output_dir / time.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        round_part = f"round-{round_index:02d}" if round_index is not None else "round-na"
        filename = f"{round_part}-{tool_name}-{uuid.uuid4().hex[:8]}.txt"
        path = day_dir / filename
        path.write_text(content)
        return path

    def _should_bypass_truncation(self, tool_name: str, arguments: dict) -> bool:
        return self._is_override_read(tool_name, arguments) or self._is_skills_markdown_read(
            tool_name, arguments
        )

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
