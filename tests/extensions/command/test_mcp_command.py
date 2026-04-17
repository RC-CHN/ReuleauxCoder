from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.extensions.command.builtin.mcp import (
    ToggleMCPServerCommand,
    _handle_show_mcp_servers,
    _handle_toggle_mcp_server,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind, UIEventLevel


class FakeTool:
    def __init__(self, backend: str) -> None:
        self._backend = backend

    def backend_id(self) -> str:
        return self._backend


def _build_ctx(backend: str) -> SimpleNamespace:
    config = Config(api_key="key", approval=ApprovalConfig())
    agent = SimpleNamespace(tools=[FakeTool(backend)])
    ui_bus = UIEventBus()
    return SimpleNamespace(config=config, agent=agent, ui_bus=ui_bus)


def test_show_mcp_rejects_non_local_runtime() -> None:
    ctx = _build_ctx("remote")

    result = _handle_show_mcp_servers(SimpleNamespace(), ctx)

    assert result.payload == {"markdown": "MCP local-only."}
    assert any(
        event.level == UIEventLevel.ERROR
        and event.kind == UIEventKind.MCP
        and "only available in local runtime" in event.message
        for event in ctx.ui_bus._history
    )


def test_toggle_mcp_rejects_non_local_runtime() -> None:
    ctx = _build_ctx("remote")

    result = _handle_toggle_mcp_server(ToggleMCPServerCommand(server_name="demo", enabled=True), ctx)

    assert result.payload == {"markdown": "MCP local-only."}
    assert any(
        event.level == UIEventLevel.ERROR and event.kind == UIEventKind.MCP
        for event in ctx.ui_bus._history
    )


def test_toggle_mcp_local_runtime_emits_success_and_refreshes(monkeypatch) -> None:
    ctx = _build_ctx("local")

    class FakeResult:
        error = None
        message = "Enabled MCP server 'demo'"
        already_in_desired_state = False
        warning = None
        server_name = "demo"
        enabled = True
        saved_path = "/tmp/config.yaml"

    class FakeView:
        def to_payload(self) -> dict:
            return {"servers": [{"name": "demo", "enabled": True, "runtime_connected": False}]}

    monkeypatch.setattr(
        "reuleauxcoder.extensions.command.builtin.mcp.toggle_mcp_server",
        lambda server_name, enabled, agent, config: FakeResult(),
    )
    monkeypatch.setattr(
        "reuleauxcoder.extensions.command.builtin.mcp.build_mcp_servers_view",
        lambda config, agent: FakeView(),
    )

    result = _handle_toggle_mcp_server(ToggleMCPServerCommand(server_name="demo", enabled=True), ctx)

    assert result.payload["servers"][0]["name"] == "demo"
    assert any(
        event.level == UIEventLevel.SUCCESS
        and event.kind == UIEventKind.MCP
        and event.data.get("saved_path") == "/tmp/config.yaml"
        for event in ctx.ui_bus._history
    )
    assert any(
        event.kind == UIEventKind.VIEW and event.data.get("action") == "refresh"
        for event in ctx.ui_bus._history
    )
