"""Read-only RPC/CLI works even when Agent startup is impossible."""
import json
import os
import subprocess
import sys

import pytest

from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.app.rpc.configuration import bind_configuration
from reuleauxcoder.app.rpc.configuration_client import ConfigurationClient
from reuleauxcoder.infrastructure.rpc.peer import RpcError, RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import StreamTransport


def isolated_environment(home):
    return {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}


def test_runtime_and_relay_expose_only_readonly_configuration(runtime, tmp_path):
    service = ConfigurationService.for_workspace(tmp_path, home=tmp_path / "home")
    bind_configuration(runtime.server.peer, service)
    client = runtime.client.configuration
    assert client.describe()["operations"] == ["describe", "inspect", "check"]
    assert client.describe()["api_version"] == 2
    assert not client.inspect()["valid"]
    content = "app:\n  api_key: private-key\n"
    result = client.check(documents=[{"scope": "workspace", "content": content}], checks=["static"])
    assert result["valid"] and result["buffer_check"]
    assert "private-key" not in json.dumps(result)
    for operation in ("prepare", "apply", "revert", "recover", "history", "validate", "editor_documents"):
        with pytest.raises(RpcError) as caught:
            runtime.client.peer.request("config." + operation, {})
        assert caught.value.code == -32601
    bind_configuration(runtime.server.peer, service, allow_checks=False)
    assert client.inspect()["valid"] is False
    with pytest.raises(RpcError) as caught:
        client.check()
    assert caught.value.data["code"] == "local_only"


def test_stdio_checks_buffers_without_writing_or_starting_agent(tmp_path):
    root, home = tmp_path / "项目 with spaces", tmp_path / "home"
    path = root / ".rcoder/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("app: [broken", encoding="utf-8")
    with (tmp_path / "stderr.log").open("w+") as errors:
        process = subprocess.Popen(
            [sys.executable, "-m", "reuleauxcoder", "config", "rpc", "--workspace", str(root)],
            env=isolated_environment(home), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
        )
        peer = RpcPeer(StreamTransport(process.stdout, process.stdin))
        client = ConfigurationClient(peer)
        peer.start()
        try:
            assert client.initialize()["mode"] == "configuration"
            initial = client.inspect()
            assert not initial["valid"] and initial["diagnostics"][0]["code"] == "invalid_yaml"
            content = "# preserved\napp:\n  api_key: repair-secret\n"
            result = client.check(documents=[{"scope": "workspace", "content": content}])
            assert result["valid"] and all(check["status"] == "passed" for check in result["checks"])
            assert path.read_text() == "app: [broken"
            path.write_text(content, encoding="utf-8")
            assert client.inspect()["valid"]
            assert "repair-secret" not in json.dumps([initial, result, client.inspect()])
        finally:
            peer.close()
            try:
                assert process.wait(timeout=10) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
        errors.seek(0)
        assert "Traceback" not in errors.read()


def test_static_cli_is_independent_of_agent_provider_and_frontend_imports(tmp_path):
    script = """
import importlib.abc, sys
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('reuleauxcoder.domain.agent.agent', 'reuleauxcoder.services.llm', 'reuleauxcoder.extensions.mcp.manager', 'reuleauxcoder.interfaces.entrypoint', 'openai', 'rich', 'prompt_toolkit')):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Boundary())
from reuleauxcoder.interfaces.configuration import main
raise SystemExit(main(['check', '--check', 'static', '--workspace', sys.argv[1]]))
"""
    completed = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
        env=isolated_environment(tmp_path / "home"), capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["valid"] is False
    assert not (tmp_path / ".rcoder").exists()


def test_cli_raw_yaml_stdin_and_revision_conflict(tmp_path):
    def command(*args, input=None):
        return subprocess.run(
            [sys.executable, "-m", "reuleauxcoder", "config", *args, "--workspace", str(tmp_path)],
            input=input, env=isolated_environment(tmp_path / "home"), text=True, encoding="utf-8",
            capture_output=True, timeout=15,
        )
    revision = json.loads(command("inspect").stdout)["revision"]
    checked = command("check", "--content", "-", "--scope", "workspace", input="app:\n  api_key: cli-secret\n")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["buffer_check"]
    assert "cli-secret" not in checked.stdout
    assert not (tmp_path / ".rcoder").exists()
    path = tmp_path / ".rcoder/config.yaml"
    path.parent.mkdir()
    path.write_text("app: [broken", encoding="utf-8")
    assert command("check").returncode == 1
    conflict = command("check", "--revision", revision)
    assert conflict.returncode == 2
    assert json.loads(conflict.stdout)["error"]["code"] == "conflict"
