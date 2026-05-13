"""Tests for platform detection and shell resolution."""

import shutil
from unittest import mock

from reuleauxcoder.infrastructure.platform import (
    PlatformInfo,
    ShellType,
    get_platform_info,
)


def test_unix_falls_back_to_sh_when_bash_missing():
    """On Unix, when bash is not found, fall back to POSIX sh."""

    def _which_side_effect(cmd):
        if cmd == "sh":
            return "/usr/bin/sh"
        return None

    with mock.patch.object(shutil, "which", side_effect=_which_side_effect):
        info = PlatformInfo()
        # Override system detection to simulate Unix
        info._system = "linux"
        info._is_windows = False
        info._is_linux = True
        info._is_darwin = False

        shell = info.get_preferred_shell()
        assert shell == ShellType.BASH  # classified as BASH (POSIX-compatible)
        assert info.get_shell_path() == "/usr/bin/sh"
        assert info.get_shell_executable() == ["/usr/bin/sh", "-c"]


def test_unix_unknown_when_nothing_found():
    """On Unix, when neither bash nor sh is available, return UNKNOWN."""
    with mock.patch.object(shutil, "which", return_value=None):
        info = PlatformInfo()
        info._system = "linux"
        info._is_windows = False
        info._is_linux = True
        info._is_darwin = False

        shell = info.get_preferred_shell()
        assert shell == ShellType.UNKNOWN
        assert info.get_shell_path() is None
        assert info.get_shell_executable() == []


def test_windows_still_prefers_git_bash_over_sh():
    """On Windows, sh should NOT take priority over bash/pwsh/powershell."""

    def _which_side_effect(cmd):
        mapping = {
            "bash": None,
            "pwsh": None,
            "powershell": "/windows/system32/WindowsPowerShell/v1.0/powershell.exe",
        }
        return mapping.get(cmd)

    with mock.patch.object(shutil, "which", side_effect=_which_side_effect):
        info = PlatformInfo()
        info._system = "windows"
        info._is_windows = True
        info._is_linux = False
        info._is_darwin = False

        shell = info.get_preferred_shell()
        # sh isn't even checked on Windows — falls through to powershell
        assert shell == ShellType.POWERSHELL


def test_platform_info_is_singleton():
    """get_platform_info returns the same instance."""
    a = get_platform_info()
    b = get_platform_info()
    assert a is b
