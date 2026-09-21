"""Discover and launch explicit native/WSL shells without rewriting commands."""

from __future__ import annotations

import hashlib
import ntpath
import os
from pathlib import Path
import shutil
import subprocess
import threading

from reuleauxcoder.domain.shell import ShellCatalog, ShellOption, WslDistribution
from reuleauxcoder.infrastructure.platform import get_platform_info

_NAMES = {
    "bash": "Bash",
    "sh": "POSIX sh",
    "zsh": "Zsh",
    "fish": "Fish",
    "dash": "Dash",
    "ksh": "Ksh",
    "pwsh": "PowerShell",
    "powershell": "Windows PowerShell",
    "cmd": "Command Prompt",
}
_UNIX = ("bash", "sh", "zsh", "fish", "dash", "ksh", "pwsh")
_WINDOWS = ("bash", "pwsh", "powershell", "cmd")


def _option(
    kind: str, path: str, environment: str, *, distribution=None, launcher=None
) -> ShellOption:
    identity = "\0".join((kind, path, distribution or "", launcher or ""))
    return ShellOption(
        id="shell_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
        name=_NAMES[kind],
        path=path,
        kind=kind,
        environment=environment,
        distribution=distribution,
        launcher=launcher,
    )


def is_legacy_wsl_bash(path: str) -> bool:
    """System32 bash.exe launches WSL; it is not native Git Bash."""
    normalized = path.replace("\\", "/").lower()
    return normalized.endswith(("/system32/bash.exe", "/sysnative/bash.exe"))


def _decode(data: bytes) -> str:
    # wsl.exe list output is UTF-16LE on many Windows versions, even when piped.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-16-le" if b"\0" in data else "utf-8", errors="replace")


def _run(argv: list[str], *, timeout: float = 3) -> str:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        message = _decode(result.stderr or result.stdout).strip()[:500]
        raise OSError(message or f"Shell discovery exited with {result.returncode}")
    return _decode(result.stdout)


def discover_shells(distribution: str | None = None) -> ShellCatalog:
    info = get_platform_info()
    if distribution is not None:
        if not info.is_windows:
            raise ValueError("Choose WSL distributions from the Windows runtime.")
        launcher = shutil.which("wsl.exe")
        if not launcher:
            raise ValueError("wsl.exe is unavailable.")
        installed = _run([launcher, "--list", "--quiet"]).splitlines()
        if distribution not in [name.strip() for name in installed]:
            raise ValueError(f"WSL distribution is not installed: {distribution}")
        # Explicit selection starts only this distribution. Probe non-interactively,
        # without evaluating a login profile or interpolating its name into a script.
        probe = 'for s in bash sh zsh fish dash ksh pwsh; do p=$(command -v "$s") || continue; [ -f "$p" ] && [ -x "$p" ] && printf "%s\\t%s\\n" "$s" "$p"; done'
        output = _run(
            [
                launcher,
                "--distribution",
                distribution,
                "--exec",
                "/bin/sh",
                "-c",
                probe,
            ],
            timeout=8,
        )
        options = []
        for line in output.splitlines():
            kind, separator, path = line.partition("\t")
            if separator and kind in _UNIX and path.startswith("/"):
                options.append(
                    _option(
                        kind,
                        path,
                        f"WSL / {distribution}",
                        distribution=distribution,
                        launcher=launcher,
                    )
                )
        return ShellCatalog(tuple(options), distribution=distribution)

    environment = "Windows" if info.is_windows else info.system
    distro = os.environ.get("WSL_DISTRO_NAME") if not info.is_windows else None
    if distro:
        environment = f"WSL / {distro}"
    options: list[ShellOption] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, candidate: str) -> None:
        path = shutil.which(candidate)
        if not path or (info.is_windows and is_legacy_wsl_bash(path)):
            return
        path = ntpath.abspath(path) if info.is_windows else os.path.abspath(path)
        key = (kind, path.casefold() if info.is_windows else os.path.realpath(path))
        if key not in seen:
            seen.add(key)
            options.append(_option(kind, path, environment, distribution=distro))

    kinds = _WINDOWS if info.is_windows else _UNIX
    for kind in kinds:
        add(kind, kind)
        # Show distinct installations rather than only the first PATH hit.
        for directory in os.get_exec_path():
            join = ntpath.join if info.is_windows else os.path.join
            add(kind, join(directory, kind + (".exe" if info.is_windows else "")))
    if info.is_windows:
        for base in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                for relative, kind in (
                    ("Git/bin/bash.exe", "bash"),
                    ("Programs/Git/bin/bash.exe", "bash"),
                    ("PowerShell/7/pwsh.exe", "pwsh"),
                ):
                    add(kind, ntpath.join(base, *relative.split("/")))
    else:
        try:
            configured = Path("/etc/shells").read_text().splitlines()
        except OSError:
            configured = []
        for path in [os.environ.get("SHELL", ""), *configured]:
            path = path.strip()
            kind = Path(path).name
            if kind in kinds:
                add(kind, path)
    distributions: tuple[WslDistribution, ...] = ()
    diagnostics: tuple[str, ...] = ()
    launcher = shutil.which("wsl.exe") if info.is_windows else None
    if launcher:
        try:
            names = dict.fromkeys(
                name.strip()
                for name in _run([launcher, "--list", "--quiet"]).splitlines()
                if name.strip()
            )
            distributions = tuple(WslDistribution(name, launcher) for name in names)
        except (OSError, subprocess.TimeoutExpired) as error:
            diagnostics = (f"WSL discovery unavailable: {error}",)
    return ShellCatalog(tuple(options), distributions, diagnostics)


def shell_argv(
    option: ShellOption, command: str, *, cwd: str, tty: bool
) -> tuple[str, ...]:
    if option.kind in {"pwsh", "powershell"}:
        arguments = [
            option.path,
            "-NoLogo",
            "-NoProfile",
            *([] if tty else ["-NonInteractive"]),
            "-Command",
            command,
        ]
    elif option.kind == "cmd":
        arguments = [option.path, "/c", command]
    else:
        arguments = [option.path, "-c", command]
    if option.launcher:
        # Let WSL translate Windows paths, respecting its automount configuration.
        # A UNC path into another distro must never silently use this distro's cwd.
        normalized = cwd.replace("/", "\\")
        if normalized.lower().startswith(("\\\\wsl$\\", "\\\\wsl.localhost\\")):
            parts = normalized.split("\\")
            if (
                len(parts) < 4
                or parts[3].casefold() != (option.distribution or "").casefold()
            ):
                raise ValueError(
                    "The working directory belongs to a different WSL distribution."
                )
            cwd = "/" + "/".join(parts[4:])
        arguments = [
            option.launcher,
            "--distribution",
            option.distribution,
            "--cd",
            cwd,
            "--exec",
            *arguments,
        ]
    return tuple(arguments)


class LocalShellSelection:
    """One backend's in-memory preference; existing processes are untouched."""

    def __init__(self, selected: ShellOption | None = None):
        self._selected = selected
        self._options: dict[str, ShellOption] = {}
        self._lock = threading.RLock()

    @property
    def selected(self) -> ShellOption | None:
        with self._lock:
            return self._selected

    def current(self) -> ShellOption | None:
        if selected := self.selected:
            return selected
        info = get_platform_info()
        path = info.get_shell_path()
        if not path:
            return None
        kind = info.get_preferred_shell().value
        if kind == "bash" and Path(path).name == "sh":
            kind = "sh"
        distro = os.environ.get("WSL_DISTRO_NAME") if not info.is_windows else None
        return _option(
            kind,
            path,
            f"WSL / {distro}" if distro else info.system,
            distribution=distro,
        )

    def catalog(self, distribution: str | None = None) -> ShellCatalog:
        catalog = discover_shells(distribution)
        with self._lock:
            self._options = {
                key: option
                for key, option in self._options.items()
                if (option.distribution if option.launcher else None) != distribution
            }
            self._options.update((option.id, option) for option in catalog.options)
        return catalog

    def select(self, selector: str) -> ShellOption | None:
        if selector == "auto":
            with self._lock:
                self._selected = None
            return self.current()
        with self._lock:
            needs_catalog = not self._options
        if needs_catalog:
            self.catalog()
        with self._lock:
            matches = [
                option
                for option in self._options.values()
                if selector in (option.id, option.path)
                or selector.casefold()
                in (option.kind.casefold(), option.name.casefold())
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Shell is unavailable or ambiguous. Use /shell and select its exact entry."
                )
            executable = matches[0].launcher or matches[0].path
            if not shutil.which(executable):
                raise ValueError(
                    f"Shell executable is no longer available: {executable}"
                )
            self._selected = matches[0]
            return self._selected
