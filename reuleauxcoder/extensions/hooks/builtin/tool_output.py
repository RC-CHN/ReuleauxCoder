"""Built-in hook that truncates oversized tool output and archives full results."""

from __future__ import annotations

import time
import uuid
import hashlib
import json
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
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext
from reuleauxcoder.infrastructure.fs.paths import get_tool_outputs_dir
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir
from reuleauxcoder.infrastructure.persistence.session_paths import (
    is_safe_session_id,
    session_storage_path,
)
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


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
            Path(sessions_dir).expanduser() if sessions_dir else get_sessions_dir()
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
        line_count, separators = _line_summary(result)
        char_count = len(result)
        if line_count <= self.max_lines and char_count <= self.max_chars:
            return context

        archive_path: Path | None = None
        artifact_ref: str | None = None
        archive_reference: ToolArchiveReference | None = None
        if self.store_full_output:
            archive_path, archive_reference = self._archive_output(
                tool_call.name,
                result,
                context.round_index,
                session_id=context.session_id,
            )
            if context.session_id:
                artifact_ref = archive_reference.path

        strategy = outcome.retention_hint.strategy
        truncated_text = _retain_text(
            result,
            max_lines=self.max_lines,
            max_chars=self.max_chars,
            strategy=strategy,
            separators=separators,
        )
        retained_lines = len(truncated_text.splitlines())

        summary_lines = [
            f"[truncated] Tool output exceeded limits ({line_count} lines, {char_count} chars).",
            _retention_summary(
                strategy,
                retained_lines=retained_lines,
                max_chars=self.max_chars,
                anchor_line=outcome.retention_hint.anchor_line,
            ),
        ]
        if archive_path is not None:
            if artifact_ref and context.session_id:
                summary_lines.append(f"Full output artifact: {artifact_ref}")
                summary_lines.append(
                    "Read it with artifact_read("
                    f"artifact_ref={json.dumps(artifact_ref)})."
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
                retained_lines=retained_lines,
                strategy=strategy.value,
            ),
            archive_reference=archive_reference,
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
        self,
        tool_name: str,
        content: str,
        round_index: int | None,
        *,
        session_id: str | None,
    ) -> tuple[Path, ToolArchiveReference]:
        if session_id:
            if not is_safe_session_id(session_id):
                raise ValueError("invalid session_id")
            workspace = LocalWorkspacePort(self.sessions_dir)
            directory = workspace.resolve(
                session_storage_path(workspace.root, session_id)
            )
            artifact_dir = workspace.resolve(directory / "artifacts" / "tools")
            # Providers can reuse call IDs across rounds. Each publication needs
            # its own identity so older event references never change content.
            path = artifact_dir / f"{uuid.uuid4().hex}.txt"
            artifact_ref = f"tools/{path.name}"
            artifact_dir.mkdir(parents=True, exist_ok=True)
        else:
            day_dir = self.output_dir / time.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            round_part = (
                f"round-{round_index:02d}" if round_index is not None else "round-na"
            )
            filename = f"{round_part}-{tool_name}-{uuid.uuid4().hex[:8]}.txt"
            path = day_dir / filename
            artifact_ref = str(path)
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("wb") as stream:
            for offset in range(0, len(content), 64 * 1024):
                encoded = content[offset : offset + 64 * 1024].encode("utf-8")
                stream.write(encoded)
                digest.update(encoded)
                size_bytes += len(encoded)
        return path, ToolArchiveReference(
            path=artifact_ref,
            checksum_sha256=digest.hexdigest(),
            size_bytes=size_bytes,
        )

    def _should_bypass_truncation(self, tool_name: str, arguments: dict) -> bool:
        return (
            tool_name in {"artifact_read", "history_read", "history_search"}
            or self._is_override_read(tool_name, arguments)
            or self._is_skills_markdown_read(tool_name, arguments)
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


_LINE_SEPARATORS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def _line_summary(text: str) -> tuple[int, tuple[str, ...]]:
    """Count splitlines boundaries without allocating the discarded lines."""
    separators = tuple(char for char in _LINE_SEPARATORS if char in text)
    count = sum(text.count(char) for char in separators)
    if "\r" in separators and "\n" in separators:
        count -= text.count("\r\n")
    if text and text[-1] not in _LINE_SEPARATORS:
        count += 1
    return count, separators


def _line_spans(
    text: str, separators: tuple[str, ...], count: int, *, tail: bool = False
) -> list[tuple[int, int]]:
    """Locate only the retained lines; even a huge single line stays a span."""
    spans = []
    position = len(text) if tail else 0
    boundaries = {
        char: text.rfind(char) if tail else text.find(char) for char in separators
    }
    for _ in range(count):
        if tail:
            if position <= 0:
                break
            end = position
            if text[end - 1] in _LINE_SEPARATORS:
                end -= 1
                if text[end] == "\n" and end and text[end - 1] == "\r":
                    end -= 1
            for char, boundary in boundaries.items():
                if boundary >= end:
                    boundaries[char] = text.rfind(char, 0, end)
            start = max(boundaries.values(), default=-1) + 1
            spans.append((start, end))
            position = start
        else:
            if position >= len(text):
                break
            for char, boundary in boundaries.items():
                if 0 <= boundary < position:
                    boundaries[char] = text.find(char, position)
            end = min(
                (index for index in boundaries.values() if index >= 0),
                default=len(text),
            )
            spans.append((position, end))
            position = end + (2 if text.startswith("\r\n", end) else 1)
    return list(reversed(spans)) if tail else spans


def _slice_joined(
    text: str, spans: list[tuple[int, int]], start: int, stop: int
) -> str:
    """Slice newline-joined spans without materializing the unbounded join."""
    parts = []
    position = 0
    for index, (left, right) in enumerate(spans):
        if index:
            if start <= position < stop:
                parts.append("\n")
            position += 1
        length = right - left
        lower = max(0, start - position)
        upper = min(length, stop - position)
        if lower < upper:
            parts.append(text[left + lower : left + upper])
        position += length
        if position >= stop:
            break
    return "".join(parts)


def _retain_text(
    text: str,
    *,
    max_lines: int,
    max_chars: int,
    strategy: ToolRetentionStrategy,
    separators: tuple[str, ...] | None = None,
) -> str:
    if separators is None:
        _, separators = _line_summary(text)
    if strategy is ToolRetentionStrategy.TAIL:
        spans = _line_spans(text, separators, max_lines, tail=True)
        length = sum(end - start for start, end in spans) + max(0, len(spans) - 1)
        return _slice_joined(text, spans, max(0, length - max_chars), length).lstrip()
    if strategy is ToolRetentionStrategy.HEAD_TAIL:
        head_count = max(1, (max_lines + 1) // 2)
        tail_count = max(0, max_lines - head_count)
        spans = _line_spans(text, separators, head_count) + _line_spans(
            text, separators, tail_count, tail=True
        )
        length = sum(end - start for start, end in spans) + max(0, len(spans) - 1)
        if length <= max_chars:
            return _slice_joined(text, spans, 0, length)
        head_chars = max(1, (max_chars + 1) // 2)
        tail_chars = max_chars - head_chars
        head = _slice_joined(text, spans, 0, head_chars).rstrip()
        if not tail_chars:
            return head
        return (
            head
            + "\n"
            + _slice_joined(text, spans, length - tail_chars, length).lstrip()
        )
    spans = _line_spans(text, separators, max_lines)
    return _slice_joined(text, spans, 0, max_chars).rstrip()


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
