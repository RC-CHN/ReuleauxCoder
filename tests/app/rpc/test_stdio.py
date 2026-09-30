import subprocess
import sys
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.capabilities import UIProfile, UICapability
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.rpc.client import RuntimeClient
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.infrastructure.rpc.peer import RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import StreamTransport
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


def _startup_command(workspace, *args):
    config = workspace / "config.yaml"
    config.write_text("""app:
  api_key: test-key
  model: test-model
session:
  auto_save: false
lsp:
  enabled: false
skills:
  enabled: false
""")
    # Isolate global config, while exercising the real CLI bootstrap and runner.
    script = """from pathlib import Path
from reuleauxcoder.services.config.loader import ConfigLoader
ConfigLoader.GLOBAL_CONFIG_PATH = Path('no-global-config.yaml')
from reuleauxcoder.interfaces.cli.main import main
raise SystemExit(main())
"""
    return [sys.executable, "-c", script, "-c", str(config), *args]


@pytest.mark.parametrize("damaged", [None, "manifest-missing", "manifest-corrupt", "replay-missing", "replay-corrupt"])
def test_stdio_backend_handshake_commands_and_clean_eof(tmp_path, damaged):
    tmp_path = tmp_path / "安全随手拍"
    tmp_path.mkdir()
    old_files = {}
    old_id = None
    if damaged:
        sessions = tmp_path / ".rcoder/sessions"
        old_id = SessionStore(sessions).save(messages=[{"role": "user", "content": "previous task"}], model="test-model")
        artifact, failure = damaged.split("-")
        path = sessions / old_id / f"{artifact}.json"
        if failure == "missing":
            path.unlink()
        else:
            path.write_text('{"private content":', encoding="utf-8")
        old_files = {p: p.read_bytes() for p in (sessions / old_id).rglob("*") if p.is_file()}
    command = _startup_command(tmp_path, "--rpc-stdio")
    with (tmp_path / "stderr.log").open("w+") as errors:
        process = subprocess.Popen(
            command,
            cwd=tmp_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        peer = RpcPeer(StreamTransport(process.stdout, process.stdin))
        bus = UIEventBus()
        client = RuntimeClient(
            peer, bus, SimpleNamespace(cancel=lambda request_id: None)
        )
        peer.start()
        try:
            info = client.initialize(UIProfile("tui", "TUI", frozenset(UICapability)))
            assert info["version"] == 1
            assert info["configuration_api"] == 2
            assert client.configuration.describe()["api_version"] == 2
            inspected = client.configuration.inspect()
            assert inspected["valid"]
            assert inspected["sources"][-1]["path"] == str(tmp_path / "config.yaml")
            assert "test-key" not in str(inspected)
            assert client.state.model == "test-model"
            if damaged:
                assert client.state.session_id != old_id
                assert any("original files were kept" in event.message for event in info["startup_events"])
                assert "private content" not in str(info["startup_events"])
            client.submit("/help")
            client.wait_idle()
            client.submit(ActionRequest("thinking.set_effort", {"level": "high"}))
            client.wait_idle()
            assert bus.history_snapshot()
            client.close()  # stdin EOF shuts the owning backend process down.
            assert process.wait(timeout=15) == 0
            errors.seek(0)
            assert "Traceback" not in errors.read()
            assert all(path.read_bytes() == data for path, data in old_files.items())
        finally:
            client.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


@pytest.mark.parametrize("rpc_stdio", [False, True], ids=["cli", "stdio"])
@pytest.mark.parametrize("damaged", [False, True], ids=["missing", "corrupt"])
def test_explicit_resume_failure_is_actionable_and_preserves_files(tmp_path, rpc_stdio, damaged):
    sessions = tmp_path / ".rcoder/sessions"
    session_id = "missing-session"
    old_files = {}
    if damaged:
        session_id = SessionStore(sessions).save(
            messages=[{"role": "user", "content": "private session content"}],
            model="test-model",
        )
        (sessions / session_id / "manifest.json").write_text('{"private content":', encoding="utf-8")
        old_files = {p: p.read_bytes() for p in sessions.rglob("*") if p.is_file()}
    flags = ["--rpc-stdio"] if rpc_stdio else []
    result = subprocess.run(
        _startup_command(tmp_path, *flags, "--resume", session_id),
        cwd=tmp_path,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Session restore failed" in result.stderr
    assert "Run again without --resume" in result.stderr
    assert "Traceback" not in result.stderr
    assert "private content" not in result.stderr
    assert {p: p.read_bytes() for p in sessions.rglob("*") if p.is_file()} == old_files
