from reuleauxcoder.compat.config_migration import (
    migrate_bash_to_shell,
    migrate_legacy_config,
)


def test_models_active_is_consumed_only_by_migration_boundary() -> None:
    source = {
        "models": {
            "active": "main",
            "profiles": {"main": {"model": "demo"}},
        }
    }

    migrated, changed = migrate_legacy_config(source)

    assert changed is True
    assert migrated["models"]["active_main"] == "main"
    assert "active" not in migrated["models"]
    assert source["models"]["active"] == "main"


def test_explicit_active_main_wins_while_legacy_alias_is_removed() -> None:
    migrated, changed = migrate_legacy_config(
        {
            "models": {
                "active": "old",
                "active_main": "main",
                "profiles": {
                    "old": {"model": "old"},
                    "main": {"model": "main"},
                },
            }
        }
    )

    assert changed is True
    assert migrated["models"]["active_main"] == "main"
    assert "active" not in migrated["models"]


def test_migrate_bash_to_shell_replaces_mode_tools() -> None:
    data = {
        "modes": {
            "profiles": {
                "coder": {"tools": ["read_file", "bash", "write_file"]},
                "debugger": {"tools": ["bash", "grep"]},
            }
        }
    }
    migrated, changed = migrate_bash_to_shell(data)
    assert changed is True
    assert migrated["modes"]["profiles"]["coder"]["tools"] == [
        "read_file",
        "shell",
        "write_file",
    ]
    assert migrated["modes"]["profiles"]["debugger"]["tools"] == ["shell", "grep"]


def test_migrate_bash_to_shell_replaces_approval_rules() -> None:
    data = {
        "approval": {
            "rules": [
                {"tool_name": "bash", "action": "require_approval"},
                {"tool_name": "write_file", "action": "require_approval"},
            ]
        }
    }
    migrated, changed = migrate_bash_to_shell(data)
    assert changed is True
    assert migrated["approval"]["rules"][0]["tool_name"] == "shell"
    assert migrated["approval"]["rules"][1]["tool_name"] == "write_file"


def test_migrate_bash_to_shell_no_change_when_no_bash() -> None:
    data = {
        "modes": {
            "profiles": {
                "coder": {"tools": ["read_file", "shell"]},
            }
        },
        "approval": {
            "rules": [
                {"tool_name": "shell", "action": "require_approval"},
            ]
        },
    }
    migrated, changed = migrate_bash_to_shell(data)
    assert changed is False
    assert migrated == data


def test_migrate_bash_to_shell_leaves_original_intact() -> None:
    data = {
        "modes": {
            "profiles": {
                "coder": {"tools": ["bash"]},
            }
        }
    }
    migrated, _ = migrate_bash_to_shell(data)
    assert data["modes"]["profiles"]["coder"]["tools"] == ["bash"]
    assert migrated["modes"]["profiles"]["coder"]["tools"] == ["shell"]
