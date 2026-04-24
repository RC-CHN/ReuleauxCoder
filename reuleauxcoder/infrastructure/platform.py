"""Platform detection and shell utilities for cross-platform support."""

from __future__ import annotations

import platform
import shutil
import sys
from enum import Enum
from pathlib import Path


class ShellType(Enum):
    """Supported shell types."""

    BASH = "bash"
    POWERSHELL = "powershell"
    POWERSHELL_CORE = "pwsh"
    CMD = "cmd"
    UNKNOWN = "unknown"


class PlatformInfo:
    """Platform detection and shell configuration."""

    def __init__(self):
        self._system = platform.system().lower()
        self._is_windows = self._system == "windows"
        self._is_linux = self._system == "linux"
        self._is_darwin = self._system == "darwin"
        self._shell: ShellType | None = None
        self._shell_path: str | None = None

    @property
    def is_windows(self) -> bool:
        return self._is_windows

    @property
    def is_linux(self) -> bool:
        return self._is_linux

    @property
    def is_darwin(self) -> bool:
        return self._is_darwin

    @property
    def system(self) -> str:
        return self._system

    def get_preferred_shell(self) -> ShellType:
        """Get the preferred shell for this platform."""
        if self._shell is not None:
            return self._shell

        if self._is_windows:
            # Prefer PowerShell Core, then PowerShell, then CMD
            if shutil.which("pwsh"):
                self._shell = ShellType.POWERSHELL_CORE
                self._shell_path = shutil.which("pwsh")
            elif shutil.which("powershell"):
                self._shell = ShellType.POWERSHELL
                self._shell_path = shutil.which("powershell")
            elif shutil.which("cmd"):
                self._shell = ShellType.CMD
                self._shell_path = shutil.which("cmd")
            else:
                self._shell = ShellType.UNKNOWN
                self._shell_path = None
        else:
            # Unix-like systems
            if shutil.which("bash"):
                self._shell = ShellType.BASH
                self._shell_path = shutil.which("bash")
            else:
                self._shell = ShellType.UNKNOWN
                self._shell_path = None

        return self._shell

    def get_shell_path(self) -> str | None:
        """Get the path to the preferred shell executable."""
        if self._shell_path is None:
            self.get_preferred_shell()
        return self._shell_path

    def get_shell_executable(self) -> list[str]:
        """Get shell command as a list for subprocess."""
        shell = self.get_preferred_shell()
        path = self.get_shell_path()

        if path is None:
            return []

        if shell in (ShellType.POWERSHELL, ShellType.POWERSHELL_CORE):
            return [path, "-NoProfile", "-Command"]
        elif shell == ShellType.CMD:
            return [path, "/c"]
        elif shell == ShellType.BASH:
            return [path, "-c"]
        else:
            return [path, "-c"]

    def get_bin_paths(self) -> list[Path]:
        """Get common binary paths for this platform."""
        paths: list[Path] = []

        if self._is_windows:
            # Windows common paths
            localappdata = Path.home() / "AppData" / "Local"
            roaming = Path.home() / "AppData" / "Roaming"

            paths.extend(
                [
                    localappdata / "Programs",
                    localappdata / "Microsoft" / "WindowsApps",
                    Path.home() / ".local" / "bin",
                    roaming / "Python" / "Python310" / "Scripts",
                    roaming / "Python" / "Python311" / "Scripts",
                    roaming / "Python" / "Python312" / "Scripts",
                    roaming / "Python" / "Python313" / "Scripts",
                    Path("C:") / "Program Files" / "nodejs",
                    Path("C:") / "Program Files" / "PowerShell" / "7",
                ]
            )
        else:
            # Unix paths
            paths.extend(
                [
                    Path("/usr/local/bin"),
                    Path("/usr/bin"),
                    Path.home() / ".local" / "bin",
                    Path("/opt/homebrew/bin"),
                ]
            )

        return paths

    def format_path_for_display(self, path: Path | str) -> str:
        """Format path for display (use forward slashes for consistency)."""
        p = Path(path)
        return str(p.as_posix())


# Global platform info singleton
_platform_info: PlatformInfo | None = None


def get_platform_info() -> PlatformInfo:
    """Get the global platform info instance."""
    global _platform_info
    if _platform_info is None:
        _platform_info = PlatformInfo()
    return _platform_info


def is_windows() -> bool:
    """Check if running on Windows."""
    return get_platform_info().is_windows


def get_shell_type() -> ShellType:
    """Get the preferred shell type."""
    return get_platform_info().get_preferred_shell()


def get_shell_command() -> list[str]:
    """Get shell command prefix for subprocess."""
    return get_platform_info().get_shell_executable()
