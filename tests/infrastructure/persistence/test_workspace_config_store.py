from pathlib import Path

from reuleauxcoder.infrastructure.persistence.workspace_config_store import (
    WorkspaceConfigStore,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config


def test_save_mcp_enabled_writes_only_workspace_override(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_yaml_config(path, {"app": {"model": "test"}})
    store = WorkspaceConfigStore(path)

    saved_path = store.save_mcp_server_enabled("docs", False)

    assert saved_path == path
    assert load_yaml_config(path) == {
        "app": {"model": "test"},
        "mcp": {"servers": {"docs": {"enabled": False}}},
    }


def test_save_mcp_enabled_preserves_existing_workspace_server_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    save_yaml_config(
        path,
        {"mcp": {"servers": {"docs": {"command": "local", "enabled": True}}}},
    )
    store = WorkspaceConfigStore(path)

    store.save_mcp_server_enabled("docs", False)

    assert load_yaml_config(path)["mcp"]["servers"]["docs"] == {
        "command": "local",
        "enabled": False,
    }
