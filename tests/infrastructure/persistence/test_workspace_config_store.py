from pathlib import Path

from reuleauxcoder.infrastructure.persistence.workspace_config_store import (
    WorkspaceConfigStore,
)
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
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


def test_save_approval_config_preserves_pattern_rules(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    store = WorkspaceConfigStore(path)

    store.save_approval_config(
        ApprovalConfig(
            rules=[
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="src/**",
                    action="allow",
                )
            ]
        )
    )

    assert load_yaml_config(path)["approval"]["rules"] == [
        {
            "tool_name": "edit_file",
            "pattern": "src/**",
            "action": "allow",
        }
    ]
