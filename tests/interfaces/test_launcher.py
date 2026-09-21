import json
from types import SimpleNamespace

import pytest

from reuleauxcoder.interfaces import launcher


@pytest.fixture
def launch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(launcher.sys, "argv", ["rcoder"])
    monkeypatch.setattr(launcher, "_is_terminal", lambda: True)
    monkeypatch.setattr(launcher, "_cli", lambda: calls.append("cli") or 0)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/node")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="v22.9.0\n", stderr=""
        ),
    )
    monkeypatch.setattr(
        launcher.os, "execv", lambda path, args: calls.append((path, args))
    )
    monkeypatch.setattr(launcher, "TUI_ASSETS", tmp_path)
    (tmp_path / "cli.mjs").write_text("", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"node_major": 22}), encoding="utf-8"
    )
    return calls


def test_auto_tui_uses_installed_python_and_preserves_options(launch, monkeypatch):
    monkeypatch.setattr(
        launcher.sys,
        "argv",
        ["rcoder", "--model", "profile", "--theme", "ocean", "--no-mouse"],
    )
    launcher.main()
    assert launch == [
        (
            "/node",
            [
                "/node",
                str(launcher.TUI_ASSETS / "cli.mjs"),
                "--python",
                launcher.sys.executable,
                "--model",
                "profile",
                "--theme",
                "ocean",
                "--no-mouse",
            ],
        )
    ]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("problem", ["missing", "old", "broken", "timeout"])
def test_node_probe_explains_fallback_or_explicit_failure(
    launch, monkeypatch, capsys, explicit, problem
):
    if problem == "missing":
        monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    elif problem == "timeout":

        def timeout(*args, **kwargs):
            raise launcher.subprocess.TimeoutExpired("node", 3)

        monkeypatch.setattr(launcher.subprocess, "run", timeout)
    else:
        monkeypatch.setattr(
            launcher.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0,
                stdout="v20.1.0" if problem == "old" else "unrecognized",
                stderr="",
            ),
        )
    result = launcher.tui_main() if explicit else launcher.main()
    assert result == (1 if explicit else 0)
    assert launch == ([] if explicit else ["cli"])
    captured = capsys.readouterr()
    assert captured.out == ""
    message = captured.err
    assert "Node" in message
    assert "rcoder-cli" in message
    if not explicit:
        assert "falling back to CLI" in message
        assert "rcoder-tui" in message
        assert "no npm install is needed" in message
    if problem == "old":
        assert ">= 22" in message


@pytest.mark.parametrize(
    "argv", [["--prompt", "hello"], ["-phello"], ["--server"], ["--rpc-stdio"]]
)
def test_backend_and_batch_modes_never_probe_node(launch, monkeypatch, argv):
    monkeypatch.setattr(launcher.sys, "argv", ["rcoder", *argv])
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: pytest.fail("Unexpected Node probe")
    )
    assert launcher.main() == 0
    assert launch == ["cli"]


def test_redirected_auto_uses_cli_but_explicit_tui_errors(launch, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "_is_terminal", lambda: False)
    assert launcher.main() == 0
    with pytest.raises(SystemExit) as error:
        launcher.tui_main()
    assert error.value.code == 2
    assert launch == ["cli"]
    assert "requires terminal" in capsys.readouterr().err


def test_missing_bundle_is_an_installation_error(launch, capsys):
    (launcher.TUI_ASSETS / "cli.mjs").unlink()
    assert launcher.main() == 1
    assert launch == []
    assert "Reinstall the published wheel" in capsys.readouterr().err


def test_custom_backend_arguments_are_not_reinterpreted(launch, monkeypatch):
    argv = ["--backend", "ssh", "--", "host", "rcoder", "--rpc-stdio"]
    monkeypatch.setattr(launcher.sys, "argv", ["rcoder-tui", *argv])
    launcher.tui_main()
    assert launch[0][1][2:] == argv


def test_help_does_not_need_node_or_a_terminal(launch, monkeypatch, capsys):
    monkeypatch.setattr(launcher.sys, "argv", ["rcoder", "--help"])
    monkeypatch.setattr(
        launcher.shutil, "which", lambda name: pytest.fail("Unexpected Node probe")
    )
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 0
    assert "rcoder-cli" in capsys.readouterr().out
    assert launch == []
