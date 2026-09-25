import subprocess
import sys
from types import SimpleNamespace

from reuleauxcoder.app.commands.capabilities import UIProfile, UICapability
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.rpc.client import RuntimeClient
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.infrastructure.rpc.peer import RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import StreamTransport


def test_stdio_backend_handshake_commands_and_clean_eof(tmp_path):
    config = tmp_path / "config.yaml"
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
main()
"""
    with (tmp_path / "stderr.log").open("w+") as errors:
        process = subprocess.Popen(
            [sys.executable, "-c", script, "--rpc-stdio", "-c", str(config)],
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
            assert inspected["sources"][-1]["path"] == str(config)
            assert "test-key" not in str(inspected)
            assert client.state.model == "test-model"
            client.submit("/help")
            client.wait_idle()
            client.submit(ActionRequest("thinking.set_effort", {"level": "high"}))
            client.wait_idle()
            assert bus.history_snapshot()
            client.close()  # stdin EOF shuts the owning backend process down.
            assert process.wait(timeout=15) == 0
            errors.seek(0)
            assert "Traceback" not in errors.read()
        finally:
            client.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
