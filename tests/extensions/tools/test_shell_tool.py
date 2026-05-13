"""Tests for ShellTool CWD tracking, stale-directory recovery, and
cross-platform shell execution behaviour."""

import os
import subprocess
import tempfile
from unittest import mock

from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.infrastructure.platform import ShellType


def test_execute_local_resets_cwd_when_directory_deleted():
    """When tracked CWD is deleted externally, next command resets to project root."""
    tool = ShellTool()

    # Create a temp directory and cd into it
    tmpdir = tempfile.mkdtemp(prefix="rcoder-shell-test-")
    tool._cwd = tmpdir
    assert os.path.isdir(tmpdir)

    # Delete the directory behind the tool's back
    os.rmdir(tmpdir)
    assert not os.path.isdir(tmpdir)

    # Next command should detect stale CWD and reset
    result = tool._execute_local("echo hello")

    assert "working directory no longer exists" in result
    assert "reset to the project root" in result
    assert tmpdir in result
    assert tool._cwd is None


def test_execute_local_succeeds_when_cwd_valid():
    """Normal execution works fine when CWD is valid."""
    tool = ShellTool()

    result = tool._execute_local("echo hello")

    assert "hello" in result
    assert "working directory no longer exists" not in result


def test_execute_local_uses_os_getcwd_when_cwd_is_none():
    """When _cwd is None, fallback to os.getcwd() works normally."""
    tool = ShellTool()
    tool._cwd = None

    result = tool._execute_local("echo hello")

    assert "hello" in result


# ── _run_powershell: && handling ──────────────────────────────────────────


def test_run_powershell_core_preserves_and_operator():
    """PowerShell Core (pwsh 7+) supports && natively — do not replace."""
    tool = ShellTool()
    command = "cd /tmp && echo done"

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info"
    ) as mock_platform:
        mock_info = mock.MagicMock()
        mock_info.get_preferred_shell.return_value = ShellType.POWERSHELL_CORE
        mock_info.get_shell_executable.return_value = ["pwsh", "-NoProfile", "-Command"]
        mock_platform.return_value = mock_info

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="done\n", stderr=""
            )
            tool._cwd = "/tmp"
            tool._run_powershell(command, "/tmp", 30)

    cmd_passed = mock_run.call_args[0][0]
    full_cmd = " ".join(cmd_passed)
    assert "&&" in full_cmd, "&& should be preserved for PowerShell Core"
    # ; should not appear as a replacement
    normalized_part = cmd_passed[-1]
    assert "&&" in normalized_part


def test_run_powershell_legacy_replaces_and_operator():
    """Legacy PowerShell 5.1 does not support && — replace with ;."""
    tool = ShellTool()
    command = "cd /tmp && echo done"

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info"
    ) as mock_platform:
        mock_info = mock.MagicMock()
        mock_info.get_preferred_shell.return_value = ShellType.POWERSHELL
        mock_info.get_shell_executable.return_value = [
            "powershell",
            "-NoProfile",
            "-Command",
        ]
        mock_platform.return_value = mock_info

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="done\n", stderr=""
            )
            tool._cwd = "/tmp"
            tool._run_powershell(command, "/tmp", 30)

    cmd_passed = mock_run.call_args[0][0]
    normalized_part = cmd_passed[-1]
    assert "&&" not in normalized_part, "&& should be replaced in legacy PowerShell"
    assert ";" in normalized_part


# ── _update_cwd: separator logic ──────────────────────────────────────────


def test_update_cwd_splits_on_and_for_git_bash_on_windows():
    """Windows + Git Bash: && should split cd commands."""
    tool = ShellTool()
    tmpdir = tempfile.mkdtemp(prefix="rcoder-cwd-bash-")

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info"
    ) as mock_platform:
        mock_info = mock.MagicMock()
        mock_info.get_preferred_shell.return_value = ShellType.BASH
        mock_platform.return_value = mock_info

        tool._update_cwd(f"cd {tmpdir} && echo done", "/tmp", is_windows=True)

    assert tool._cwd == tmpdir, "cd via && should be tracked on Windows Git Bash"


def test_update_cwd_splits_on_and_for_pwsh_core_on_windows():
    """Windows + PowerShell Core: && should split cd commands."""
    tool = ShellTool()
    tmpdir = tempfile.mkdtemp(prefix="rcoder-cwd-pwsh7-")

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info"
    ) as mock_platform:
        mock_info = mock.MagicMock()
        mock_info.get_preferred_shell.return_value = ShellType.POWERSHELL_CORE
        mock_platform.return_value = mock_info

        tool._update_cwd(f"cd {tmpdir} && echo done", "/tmp", is_windows=True)

    assert tool._cwd == tmpdir, "cd via && should be tracked on pwsh 7+"


def test_update_cwd_splits_on_semicolon_for_legacy_powershell():
    """Windows + legacy PowerShell: only ; (not &&) should split."""
    tool = ShellTool()
    tmpdir = tempfile.mkdtemp(prefix="rcoder-cwd-ps5-")

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info"
    ) as mock_platform:
        mock_info = mock.MagicMock()
        mock_info.get_preferred_shell.return_value = ShellType.POWERSHELL
        mock_platform.return_value = mock_info

        tool._update_cwd(f"cd {tmpdir} ; echo done", "/tmp", is_windows=True)

    assert tool._cwd == tmpdir, "cd via ; should be tracked on legacy PowerShell"


def test_update_cwd_unix_splits_on_and_and_or_and_semicolon():
    """Unix: &&, ||, and ; should all split commands."""
    tool = ShellTool()
    tmpdir = tempfile.mkdtemp(prefix="rcoder-cwd-unix-")

    tool._update_cwd(f"cd {tmpdir} && echo done", "/tmp", is_windows=False)
    assert tool._cwd == tmpdir, "cd via && should be tracked on Unix"

    tmpdir2 = tempfile.mkdtemp(prefix="rcoder-cwd-unix2-")
    tool._update_cwd(f"cd {tmpdir2} || exit 1", "/tmp", is_windows=False)
    assert tool._cwd == tmpdir2, "cd via || should be tracked on Unix"


# ── _execute_local: shell invocation strategy ─────────────────────────────


def test_execute_local_uses_explicit_shell_executable_on_unix():
    """Unix + bash: should use explicit shell path, not shell=True."""
    tool = ShellTool()

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info"
    ) as mock_platform:
        mock_info = mock.MagicMock()
        mock_info.is_windows = False
        mock_info.get_preferred_shell.return_value = ShellType.BASH
        mock_info.get_shell_executable.return_value = ["/bin/bash", "-c"]
        mock_platform.return_value = mock_info

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="ok\n", stderr=""
            )
            result = tool._execute_local("echo ok")

    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("shell") is not True, (
        "Should use explicit shell path, not shell=True"
    )
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "/bin/bash", f"Expected /bin/bash, got {cmd[0]}"
    assert "ok" in result


def test_execute_local_falls_back_to_shell_true_when_no_shell():
    """When no shell is detected, fall back to shell=True."""
    tool = ShellTool()

    with mock.patch(
        "reuleauxcoder.extensions.tools.builtin.shell.get_platform_info"
    ) as mock_platform:
        mock_info = mock.MagicMock()
        mock_info.is_windows = False
        mock_info.get_preferred_shell.return_value = ShellType.UNKNOWN
        mock_info.get_shell_executable.return_value = []
        mock_platform.return_value = mock_info

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="ok\n", stderr=""
            )
            result = tool._execute_local("echo ok")

    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("shell") is True, (
        "Should fall back to shell=True when no shell detected"
    )
    assert "ok" in result
