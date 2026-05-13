"""Tests for list_file built-in tool."""

import stat
from pathlib import Path

import pytest

from reuleauxcoder.extensions.tools.builtin.list_file import (
    ListFileTool,
    _sanitize_name,
    _format_mode,
)


class TestSanitizeName:
    def test_passes_plain_names(self):
        assert _sanitize_name("test.py") == "test.py"
        assert _sanitize_name("README.md") == "README.md"

    def test_escapes_backtick(self):
        assert _sanitize_name("test`file`.py") == r"test\`file\`.py"

    def test_escapes_asterisk(self):
        assert _sanitize_name("*important*.md") == r"\*important\*.md"

    def test_escapes_underscore(self):
        assert _sanitize_name("my_file.txt") == r"my\_file.txt"

    def test_escapes_brackets(self):
        assert _sanitize_name("[docs].md") == r"\[docs\].md"

    def test_escapes_pipe(self):
        assert _sanitize_name("a|b.txt") == r"a\|b.txt"

    def test_escapes_angle_brackets(self):
        assert _sanitize_name("<tag>.xml") == r"\<tag\>.xml"


class TestFormatMode:
    def test_directory(self):
        mode = stat.S_IFDIR | 0o755
        assert _format_mode(mode) == "drwxr-xr-x"

    def test_regular_file(self):
        mode = stat.S_IFREG | 0o644
        assert _format_mode(mode) == "-rw-r--r--"


class TestListFileExecute:
    @pytest.fixture
    def tool(self):
        return ListFileTool()

    def test_default_listing(self, tool, tmp_path: Path):
        (tmp_path / "README.md").write_text("hello")
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / ".hidden").write_text("secret")

        result = tool.execute(path=str(tmp_path))
        lines = result.split("\n")
        # header line
        assert lines[0] == f"{tmp_path}/:"
        # .hidden before visible (dirs first, then name — .hidden is a file)
        names = [l.split()[-1] for l in lines[1:]]
        assert ".hidden" in names
        assert "README.md" in names
        assert "main.py" in names

    def test_all_false_hides_dotfiles(self, tool, tmp_path: Path):
        (tmp_path / "README.md").write_text("")
        (tmp_path / ".hidden").write_text("")

        result = tool.execute(path=str(tmp_path), all=False)
        assert "README.md" in result
        assert ".hidden" not in result

    def test_long_false(self, tool, tmp_path: Path):
        (tmp_path / "main.py").write_text("x")

        result = tool.execute(path=str(tmp_path), long=False)
        # No header in non-long mode
        assert str(tmp_path) + ":" not in result.rsplit("\n", 1)[0]
        assert "main.py" in result

    def test_pattern_filter(self, tool, tmp_path: Path):
        (tmp_path / "main.py").write_text("")
        (tmp_path / "README.md").write_text("")

        result = tool.execute(path=str(tmp_path), pattern="*.py")
        assert "main.py" in result
        assert "README.md" not in result

    def test_single_file(self, tool, tmp_path: Path):
        f = tmp_path / "main.py"
        f.write_text("print('hi')")

        result = tool.execute(path=str(f), long=False)
        assert result == "main.py"

    def test_sanitize_in_output(self, tool, tmp_path: Path):
        (tmp_path / "tricky`name`.py").write_text("")

        result = tool.execute(path=str(tmp_path), long=False)
        # Should contain escaped backtick, not raw backtick
        assert r"\`" in result
