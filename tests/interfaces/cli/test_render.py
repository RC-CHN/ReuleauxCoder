from __future__ import annotations

import pytest

from reuleauxcoder.interfaces.cli.render import CLIRenderer


@pytest.fixture
def renderer() -> CLIRenderer:
    return CLIRenderer()


# ── Pre-truncated (wrapped by ToolOutputTruncationHook) ──────────────


_TRUNCATED_LARGE = """[truncated] Tool output exceeded limits (121 lines, 3764 chars).
Showing first 120 lines and up to 12000 chars.
To recover the full archived output, call read_file on that path with override=true.

--- BEGIN TRUNCATED OUTPUT ---
line000
line001
line002
line003
line004
line005
line006
line007
line008
--- END TRUNCATED OUTPUT ---"""


def test_compact_preserves_header(renderer: CLIRenderer) -> None:
    result = renderer._compact_tool_output("read_file", _TRUNCATED_LARGE)
    lines = result.splitlines()
    assert lines[0].startswith("[truncated]")
    assert "Showing first 120 lines" in lines[1]
    assert "--- BEGIN TRUNCATED OUTPUT ---" in result


def test_compact_shows_first_three_body_lines(renderer: CLIRenderer) -> None:
    result = renderer._compact_tool_output("read_file", _TRUNCATED_LARGE)
    assert "line000" in result
    assert "line001" in result
    assert "line002" in result


def test_compact_shows_last_three_body_lines(renderer: CLIRenderer) -> None:
    result = renderer._compact_tool_output("read_file", _TRUNCATED_LARGE)
    assert "line006" in result
    assert "line007" in result
    assert "line008" in result


def test_compact_hides_middle_body_lines(renderer: CLIRenderer) -> None:
    result = renderer._compact_tool_output("read_file", _TRUNCATED_LARGE)
    assert "line003" not in result
    assert "line004" not in result
    assert "line005" not in result


def test_compact_correct_omitted_count(renderer: CLIRenderer) -> None:
    # 9 body lines, show 3+3=6 → omit 3
    result = renderer._compact_tool_output("read_file", _TRUNCATED_LARGE)
    assert "... (3 more lines) ..." in result


def test_compact_preserves_footer(renderer: CLIRenderer) -> None:
    result = renderer._compact_tool_output("read_file", _TRUNCATED_LARGE)
    assert "--- END TRUNCATED OUTPUT ---" in result


_TRUNCATED_SMALL = """[truncated] small output.
--- BEGIN TRUNCATED OUTPUT ---
a
b
c
d
e
f
--- END TRUNCATED OUTPUT ---"""


def test_compact_small_truncated_unchanged(renderer: CLIRenderer) -> None:
    # 6 body lines exactly → no compact, show full
    result = renderer._compact_tool_output("read_file", _TRUNCATED_SMALL)
    assert result == _TRUNCATED_SMALL


_TRUNCATED_MALFORMED = """[truncated] missing markers, just this text."""


def test_compact_malformed_truncated_passthrough(renderer: CLIRenderer) -> None:
    result = renderer._compact_tool_output("read_file", _TRUNCATED_MALFORMED)
    assert result == _TRUNCATED_MALFORMED


# ── Non-truncated (original compact path) ────────────────────────────


def test_compact_non_truncated_read_file(renderer: CLIRenderer) -> None:
    text = "\n".join(f"line_{i}" for i in range(10))
    result = renderer._compact_tool_output("read_file", text)
    assert "line_0" in result
    assert "line_4" in result
    assert "line_5" not in result
    assert "... (5 more lines hidden)" in result


def test_compact_non_truncated_other_tool(renderer: CLIRenderer) -> None:
    text = "\n".join(f"line_{i}" for i in range(30))
    result = renderer._compact_tool_output("shell", text)
    assert "line_0" in result
    assert "line_19" in result
    assert "line_20" not in result
    assert "... (10 more lines hidden)" in result


def test_compact_non_truncated_below_limit(renderer: CLIRenderer) -> None:
    text = "a\nb\nc\nd\ne"  # 5 lines, exactly the read_file limit
    result = renderer._compact_tool_output("read_file", text)
    assert "more lines" not in result
    assert result == text
