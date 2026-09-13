import shutil
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from reuleauxcoder.domain.workspace import DEFAULT_SEARCH_LIMITS
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


def test_git_filters_ignored_files_and_can_include_them_explicitly(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git unavailable")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("generated/\n!generated/keep.py\n")
    (tmp_path / "main.py").write_text("needle\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "out.py").write_text("needle\n")
    port = LocalWorkspacePort(tmp_path)

    normal = port.search_text("needle", ".", include="*.py")
    expanded = port.search_text(
        "needle", ".", include="*.py", include_ignored=True, exclude_dirs=(".git",)
    )
    assert [Path(m.path).name for m in normal.matches] == ["main.py"]
    assert sorted(Path(m.path).name for m in expanded.matches) == ["main.py", "out.py"]


def test_excluded_directories_do_not_consume_search_budget(tmp_path):
    excluded = tmp_path / "a-vendor"
    excluded.mkdir()
    for i in range(10):
        (excluded / str(i)).write_text("needle")
    (tmp_path / "z.py").write_text("needle")
    result = LocalWorkspacePort(tmp_path).search_text(
        "needle", ".", exclude_dirs=("a-vendor",), max_files=1
    )
    assert [Path(m.path).name for m in result.matches] == ["z.py"]
    assert not result.truncated


def test_binary_and_oversized_files_are_not_searched(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"\0needle")
    (tmp_path / "b.map").write_text("needle" * 100)
    (tmp_path / "c.txt").write_text("needle")
    result = LocalWorkspacePort(tmp_path).search_text(
        "needle", ".", limits=replace(DEFAULT_SEARCH_LIMITS, max_file_bytes=100)
    )
    assert [Path(m.path).name for m in result.matches] == ["c.txt"]
    assert result.reasons == ("file_size",)
    assert result.scanned_bytes == 13


def test_long_matching_lines_and_total_output_are_bounded(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text(("x" * 1000 + "needle\n") * 100)
    result = LocalWorkspacePort(tmp_path).search_text(
        "needle",
        target,
        limits=replace(DEFAULT_SEARCH_LIMITS, max_line_chars=20, max_output_chars=500),
    )
    assert result.matches
    assert all(m.line == "x" * 20 and m.truncated for m in result.matches)
    assert result.reasons == ("line_chars", "output_chars")
    assert (
        sum(len(f"{m.path}:{m.line_number}: {m.line}\n") for m in result.matches) <= 500
    )
    assert result.scanned_bytes < target.stat().st_size


def test_scan_budget_and_cancellation_return_partial_facts(tmp_path):
    target = tmp_path / "text"
    target.write_bytes(b"needle\n" * 100)
    port = LocalWorkspacePort(tmp_path)
    result = port.search_text(
        "needle", target, limits=replace(DEFAULT_SEARCH_LIMITS, max_scan_bytes=14)
    )
    assert len(result.matches) == 2
    assert result.reasons == ("scan_bytes",)
    signal = threading.Event()
    signal.set()
    cancelled = port.search_text("needle", target, cancellation=signal)
    assert cancelled.matches == ()
    assert cancelled.reasons == ("cancelled",)


def test_pathological_regex_times_out_and_literal_mode_escapes_nothing(tmp_path):
    target = tmp_path / "text"
    target.write_text("a" * 100_000 + "!\n")
    result = LocalWorkspacePort(tmp_path).search_text(r"(a+)+$", target)
    assert result.reasons == ("regex_timeout",)
    target.write_text("a+b\naaab\n")
    literal = LocalWorkspacePort(tmp_path).search_text("a+b", target, literal=True)
    assert [m.line for m in literal.matches] == ["a+b"]
