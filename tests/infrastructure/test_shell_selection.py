from __future__ import annotations

from dataclasses import replace
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from reuleauxcoder.infrastructure import shells
from reuleauxcoder.infrastructure.platform import PlatformInfo, ShellType
from reuleauxcoder.domain.shell import ShellCatalog
from reuleauxcoder.infrastructure.process.local import LocalProcessPort
from reuleauxcoder.extensions.tools.backend import LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool


@pytest.fixture
def windows(monkeypatch):
    info = PlatformInfo()
    info._system = "windows"
    monkeypatch.setattr(shells, "get_platform_info", lambda: info)
    monkeypatch.setattr(shells.os, "get_exec_path", lambda: [])
    available = {
        "bash": r"C:\Windows\System32\bash.exe",
        "pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe",
        "cmd": r"C:\Windows\System32\cmd.exe",
        "wsl.exe": r"C:\Windows\System32\wsl.exe",
    }
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            available.get(name) or (name if name in available.values() else None)
        ),
    )
    return info, available


def test_windows_catalog_names_paths_and_lazy_wsl_discovery(windows, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 3 and kwargs["stdin"] == subprocess.DEVNULL
        return SimpleNamespace(
            returncode=0,
            stdout="Ubuntu 24.04\r\n开发机\r\n".encode("utf-16-le"),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    catalog = shells.discover_shells()
    assert [item.kind for item in catalog.options] == ["pwsh", "cmd"]
    assert catalog.options[0].path == r"C:\Program Files\PowerShell\7\pwsh.exe"
    assert [item.name for item in catalog.distributions] == ["Ubuntu 24.04", "开发机"]
    assert len(calls) == 1 and calls[0][1:] == ["--list", "--quiet"], (
        "listing must not start/probe distributions"
    )
    info, _ = windows
    assert info.get_preferred_shell() is ShellType.POWERSHELL_CORE, (
        "legacy WSL bash must not win automatic selection"
    )


def test_windows_wsl_failure_keeps_native_options(windows, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"WSL unavailable"
        ),
    )
    result = shells.discover_shells()
    assert result.options and not result.distributions
    assert "WSL unavailable" in result.diagnostics[0]


def test_one_wsl_distribution_is_probed_without_interpolating_its_name(
    windows, monkeypatch
):
    calls = []
    distro = "Ubuntu & workspace"

    def run(argv, **kwargs):
        calls.append(argv)
        data = (
            (distro + "\n").encode("utf-16")
            if "--list" in argv
            else b"bash\t/bin/bash\nfish\t/usr/bin/fish\ninvalid\t/tmp/fake\n"
        )
        return SimpleNamespace(returncode=0, stdout=data, stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    catalog = shells.discover_shells(distro)
    assert [item.kind for item in catalog.options] == ["bash", "fish"]
    assert calls[1][1:5] == ["--distribution", distro, "--exec", "/bin/sh"]
    assert distro not in calls[1][-1]
    command = "printf '%s' 'hello & 中文';\nprintf '$HOME'"
    option = catalog.options[0]
    argv = shells.shell_argv(option, command, cwd=r"C:\work dir\项目", tty=False)
    assert argv == (
        option.launcher,
        "--distribution",
        distro,
        "--cd",
        r"C:\work dir\项目",
        "--exec",
        "/bin/bash",
        "-c",
        command,
    )
    assert (
        shells.shell_argv(
            option, command, cwd=rf"\\wsl.localhost\{distro}\home\me", tty=True
        )[4]
        == "/home/me"
    )
    with pytest.raises(ValueError, match="different WSL"):
        shells.shell_argv(option, command, cwd=r"\\wsl$\Other\home", tty=False)
    with pytest.raises(ValueError, match="not installed"):
        shells.discover_shells("missing")


@pytest.mark.parametrize(
    "kind", ["bash", "sh", "zsh", "fish", "pwsh", "powershell", "cmd"]
)
def test_shell_arguments_preserve_scripts_and_use_family_flags(kind):
    option = shells._option(kind, f"/shells/{kind}", "test")
    command = 'echo "a b" && next\n$variable'
    pipe = shells.shell_argv(option, command, cwd="/tmp", tty=False)
    tty = shells.shell_argv(option, command, cwd="/tmp", tty=True)
    assert pipe[-1] == tty[-1] == command
    if kind in {"pwsh", "powershell"}:
        assert "-NonInteractive" in pipe and "-NonInteractive" not in tty
        assert "-NoProfile" in pipe and pipe[-2] == "-Command"
    elif kind == "cmd":
        assert pipe[-2] == "/c"
    else:
        assert pipe[-2] == "-c"


def test_refresh_invalidates_removed_choices_and_ambiguous_names_do_not_switch(
    monkeypatch,
):
    first = shells._option("bash", "/one/bash", "linux")
    second = shells._option("bash", "/two/bash", "linux")
    catalog = ShellCatalog((first, second))
    monkeypatch.setattr(shells, "discover_shells", lambda distribution=None: catalog)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    selection = shells.LocalShellSelection()
    selection.catalog()
    with pytest.raises(ValueError, match="ambiguous"):
        selection.select("bash")
    assert selection.selected is None
    assert selection.select(second.id) == second
    catalog = ShellCatalog((first,))
    selection.catalog()
    with pytest.raises(ValueError, match="unavailable"):
        selection.select(second.id)
    assert selection.selected == second, (
        "failed selection preserves the previous preference"
    )


@pytest.mark.skipif(
    os.name == "nt" or not shutil.which("sh"), reason="requires a native POSIX shell"
)
def test_selection_drives_real_processes_and_clones_without_global_mutation(tmp_path):
    backend = LocalToolBackend()
    tool = ShellTool(backend)
    options = backend.process.shells.catalog().options
    chosen = next(item for item in options if item.kind == "sh")
    previous = PlatformInfo().get_shell_path()
    backend.process.shells.select(chosen.id)
    clone = tool.clone_for_scope("child")
    assert clone.backend.process.shells.selected == chosen
    assert clone.backend.process.shells is not backend.process.shells
    assert chosen.path in tool.description and chosen.path in tool.shell_environment
    try:
        result = backend.process.run(
            "printf 'selected-shell-ok'", cwd=str(tmp_path), timeout=10
        )
        assert result.exit_code == 0 and result.stdout == "selected-shell-ok"
        backend.process.shells.select("auto")
        assert clone.backend.process.shells.selected == chosen
        assert PlatformInfo().get_shell_path() == previous
        # An explicitly selected executable disappearing must not fall back.
        backend.process.shells = shells.LocalShellSelection(
            replace(chosen, path=str(tmp_path / "missing"))
        )
        with pytest.raises(FileNotFoundError):
            backend.process.start("echo wrong", cwd=str(tmp_path), runtime_timeout=1)
    finally:
        backend.process.shutdown()
        clone.backend.process.shutdown()


def test_selected_wsl_launch_reaches_process_adapter(windows, monkeypatch, tmp_path):
    port = LocalProcessPort()
    selected = shells._option(
        "bash",
        "/bin/bash",
        "WSL / Ubuntu",
        distribution="Ubuntu",
        launcher=r"C:\Windows\System32\wsl.exe",
    )
    port.shells = shells.LocalShellSelection(selected)
    captured = []

    def spawn(argv, **kwargs):
        captured.append((argv, kwargs))
        raise FileNotFoundError("fixture stops before launching")

    monkeypatch.setattr(port, "_spawn_pipe", spawn)
    with pytest.raises(FileNotFoundError):
        port.start("echo 'same text'", cwd=str(tmp_path), runtime_timeout=1)
    assert captured[0][0][0] == selected.launcher
    assert captured[0][0][-3:] == ("/bin/bash", "-c", "echo 'same text'")
