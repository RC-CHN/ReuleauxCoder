from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.domain.config.models import Config, MCPServerConfig
from reuleauxcoder.extensions.mcp.runtime import (
    build_mcp_servers_view,
    toggle_mcp_server,
)


class _Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.saved: list[tuple[str, bool]] = []

    def save_mcp_server_enabled(self, server_name: str, enabled: bool) -> Path:
        self.saved.append((server_name, enabled))
        return self.path


class _Manager:
    def __init__(
        self,
        *,
        connected: set[str] | None = None,
        active: set[str] | None = None,
        initial_state: str = "sealed",
        connect_result: bool = True,
        disconnect_result: bool = True,
    ) -> None:
        self.connected_servers = connected or set()
        self.active_servers = active or set()
        self.initial_state = initial_state
        self.tools = []
        self.connect_result = connect_result
        self.disconnect_result = disconnect_result
        self.connect_calls: list[str] = []
        self.disconnect_calls: list[str] = []

    def connect_server(self, server: MCPServerConfig) -> bool:
        self.connect_calls.append(server.name)
        return self.connect_result

    def disconnect_server(self, server_name: str) -> bool:
        self.disconnect_calls.append(server_name)
        return self.disconnect_result


def _agent(manager: _Manager):
    return SimpleNamespace(mcp_manager=manager, tools=[])


def test_enabling_persists_user_intent_even_when_runtime_connection_fails(
    tmp_path: Path,
) -> None:
    server = MCPServerConfig(name="demo", command="fake", enabled=False)
    config = Config(mcp_servers=[server])
    manager = _Manager(connect_result=False)
    store = _Store(tmp_path / "config.yaml")

    result = toggle_mcp_server(
        "demo", enabled=True, agent=_agent(manager), config=config, store=store
    )

    assert server.enabled is True
    assert store.saved == [("demo", True)]
    assert result.config_saved is True
    assert result.runtime_applied is False
    assert result.warning is not None and "next startup" in result.warning


def test_runtime_retry_does_not_rewrite_unchanged_enabled_preference(
    tmp_path: Path,
) -> None:
    server = MCPServerConfig(name="demo", command="fake", enabled=True)
    config = Config(mcp_servers=[server])
    manager = _Manager(connect_result=False)
    store = _Store(tmp_path / "config.yaml")

    result = toggle_mcp_server(
        "demo", enabled=True, agent=_agent(manager), config=config, store=store
    )

    assert manager.connect_calls == ["demo"]
    assert store.saved == []
    assert result.config_saved is False
    assert result.runtime_applied is False
    assert server.enabled is True


def test_disabling_during_background_discovery_is_persisted(
    tmp_path: Path,
) -> None:
    server = MCPServerConfig(name="demo", command="fake", enabled=True)
    config = Config(mcp_servers=[server])
    manager = _Manager(initial_state="connecting")
    store = _Store(tmp_path / "config.yaml")

    result = toggle_mcp_server(
        "demo", enabled=False, agent=_agent(manager), config=config, store=store
    )

    assert server.enabled is False
    assert manager.disconnect_calls == ["demo"]
    assert store.saved == [("demo", False)]
    assert result.runtime_applied is True


def test_status_distinguishes_preference_connection_and_active_catalog() -> None:
    config = Config(
        mcp_servers=[
            MCPServerConfig(name="active", command="fake", enabled=True),
            MCPServerConfig(name="late", command="fake", enabled=True),
            MCPServerConfig(name="off", command="fake", enabled=False),
        ]
    )
    manager = _Manager(connected={"active", "late"}, active={"active"})

    view = build_mcp_servers_view(config, _agent(manager))

    states = {server.name: server.runtime_state for server in view.servers}
    assert states == {"active": "active", "late": "connected", "off": "disabled"}
