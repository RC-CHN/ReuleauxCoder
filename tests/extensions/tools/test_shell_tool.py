"""Tests for ShellTool CWD tracking and stale-directory recovery."""

import os
import tempfile

from reuleauxcoder.extensions.tools.builtin.shell import ShellTool


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
