"""Real RPC and recovery CLI entry points share transaction and error semantics."""

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


def test_normal_runtime_configuration_client_uses_shared_service(runtime, tmp_path):
    root, home = tmp_path / "project", tmp_path / "home"
    path = root / ".rcoder/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("app:\n  api_key: test-secret\n", encoding="utf-8")
    service = ConfigurationService.for_workspace(root, home=home)
    bind_configuration(runtime.server.peer, service)
    client = runtime.client.configuration
    assert client.describe()["api_version"] == 1
    before = client.inspect()
    candidate = client.prepare(
        base_revision=before["revision"],
        changes=[{"path": "/ui/verbosity", "value": "debug"}],
    )
    assert client.validate(change_id=candidate["id"])["valid"]
    applied = client.apply(candidate["id"])
    assert applied["status"] == "applied" and applied["activation"] == "next_start"
    assert "test-secret" not in json.dumps(client.history())
    assert runtime.agent.llm.model == "test-model"
    revert = client.revert(candidate["id"])
    client.apply(revert["id"])
    assert client.inspect()["next_start"]["ui"]["verbosity"] == "compact"
    with pytest.raises(RpcError) as caught:
        client.prepare(actor="model", changes=[])
    assert caught.value.data["code"] == "invalid_operation"


def test_relay_configuration_cannot_change_host_files(runtime, tmp_path):
    service = ConfigurationService.for_workspace(tmp_path, home=tmp_path)
    bind_configuration(runtime.server.peer, service, writable=False)
    assert runtime.client.configuration.describe()["api_version"] == 1
    with pytest.raises(RpcError) as caught:
        runtime.client.configuration.prepare(
            changes=[{"path": "/ui/verbosity", "value": "debug"}]
        )
    assert caught.value.data["code"] == "local_only"


def test_management_stdio_can_repair_broken_configuration_without_agent(tmp_path):
    root, home = tmp_path / "项目 with spaces", tmp_path / "home"
    path = root / ".rcoder/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("app: [broken", encoding="utf-8")
    with (tmp_path / "stderr.log").open("w+") as errors:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "reuleauxcoder",
                "config",
                "rpc",
                "--workspace",
                str(root),
            ],
            env=isolated_environment(home),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        peer = RpcPeer(StreamTransport(process.stdout, process.stdin))
        client = ConfigurationClient(peer)
        peer.start()
        try:
            assert client.initialize()["mode"] == "configuration"
            initial = client.inspect()
            assert not initial["valid"]
            assert initial["diagnostics"][0]["code"] == "invalid_yaml"
            candidate = client.prepare(
                document={"app": {"api_key": "repair-secret"}},
                base_revision=initial["revision"],
            )
            applied = client.apply(candidate["id"], allow_unverified_model=True)
            assert applied["status"] == "applied"
            assert client.inspect()["valid"]
            assert "repair-secret" not in json.dumps(
                [initial, candidate, applied, client.history()]
            )
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


def test_recovery_cli_is_independent_of_agent_provider_and_frontend_imports(tmp_path):
    path = tmp_path / ".rcoder/config.yaml"
    path.parent.mkdir()
    path.write_text("app: [broken", encoding="utf-8")
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
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=isolated_environment(tmp_path / "home"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["valid"] is False
    assert path.read_text() == "app: [broken"


def test_cli_candidates_survive_between_commands_and_exit_codes_distinguish_errors(
    tmp_path,
):
    env = isolated_environment(tmp_path / "home")

    def command(operation, *args, input=None):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "reuleauxcoder",
                "config",
                operation,
                "--workspace",
                str(tmp_path),
                *args,
            ],
            input=input,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=15,
        )

    broken = command("check", "--check", "static")
    assert broken.returncode == 1
    prepared = command(
        "prepare",
        "--document",
        "-",
        input=json.dumps({"app": {"api_key": "cli-secret"}}),
    )
    assert prepared.returncode == 0, prepared.stderr
    change_id = json.loads(prepared.stdout)["id"]
    rejected = command("apply", change_id)
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["error"]["code"] == "validation_required"
    applied = command("apply", change_id, "--allow-unverified-model")
    assert applied.returncode == 0, applied.stderr
    checked = command("check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "cli-secret" not in prepared.stdout + applied.stdout + checked.stdout
