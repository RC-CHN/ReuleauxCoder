from pathlib import Path

from reuleauxcoder.services.config.loader import ConfigLoader


def test_load_yaml_returns_empty_dict_for_missing_file(tmp_path: Path) -> None:
    loader = ConfigLoader()
    assert loader._load_yaml(tmp_path / "missing.yaml") == {}


def test_load_yaml_returns_empty_dict_for_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("foo: [unterminated", encoding="utf-8")

    loader = ConfigLoader()
    assert loader._load_yaml(path) == {}


def test_merge_dicts_recursively_merges_nested_dicts() -> None:
    loader = ConfigLoader()
    merged = loader._merge_dicts(
        {"app": {"model": "a", "temperature": 0.0}},
        {"app": {"temperature": 0.5}},
    )
    assert merged == {"app": {"model": "a", "temperature": 0.5}}


def test_merge_dicts_merges_profile_maps_by_name() -> None:
    loader = ConfigLoader()
    merged = loader._merge_dicts(
        {
            "models": {
                "active": "main",
                "profiles": {
                    "main": {"model": "gpt-4o", "api_key": "k1"},
                    "sub": {"model": "gpt-4o-mini", "api_key": "k2"},
                },
            }
        },
        {
            "models": {
                "active": "sub",
                "profiles": {
                    "main": {"temperature": 0.2},
                    "extra": {"model": "x", "api_key": "k3"},
                },
            }
        },
    )

    assert merged["models"]["active"] == "sub"
    assert merged["models"]["profiles"]["main"] == {
        "model": "gpt-4o",
        "api_key": "k1",
        "temperature": 0.2,
    }
    assert "sub" in merged["models"]["profiles"]
    assert "extra" in merged["models"]["profiles"]


def test_parse_config_selects_active_profiles_and_modes() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "models": {
                "active_main": "main",
                "active_sub": "sub",
                "profiles": {
                    "main": {
                        "model": "gpt-main",
                        "api_key": "main-key",
                        "temperature": 0.1,
                        "preserve_reasoning_content": True,
                        "backfill_reasoning_content_for_tool_calls": True,
                    },
                    "sub": {"model": "gpt-sub", "api_key": "sub-key", "temperature": 0.2},
                },
            },
            "modes": {
                "active": "coder",
                "profiles": {
                    "coder": {"description": "Code mode", "tools": ["shell", "read_file"]}
                },
            },
            "approval": {
                "default_mode": "warn",
                "rules": [{"tool_name": "shell", "action": "deny"}],
            },
            "skills": {"enabled": True, "scan_project": False, "disabled": ["demo"]},
            "prompt": {"system_append": "Always answer in Chinese."},
        }
    )

    assert config.model == "gpt-main"
    assert config.api_key == "main-key"
    assert config.active_model_profile == "main"
    assert config.active_main_model_profile == "main"
    assert config.active_sub_model_profile == "sub"
    assert config.active_mode == "coder"
    assert config.modes["coder"].tools == ["shell", "read_file"]
    assert config.approval.default_mode == "warn"
    assert config.approval.rules[0].tool_name == "shell"
    assert config.skills.scan_project is False
    assert config.skills.disabled == ["demo"]
    assert config.prompt.system_append == "Always answer in Chinese."
    assert config.preserve_reasoning_content is True
    assert config.backfill_reasoning_content_for_tool_calls is True


def test_parse_config_reads_remote_exec_settings() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "app": {"api_key": "key"},
            "models": {"profiles": {"main": {"model": "gpt-main", "api_key": "main-key"}}},
            "modes": {"profiles": {"coder": {}}},
            "remote_exec": {
                "enabled": True,
                "host_mode": True,
                "relay_bind": "0.0.0.0:9999",
                "bootstrap_token_ttl_sec": 111,
                "peer_token_ttl_sec": 222,
                "heartbeat_interval_sec": 7,
                "heartbeat_timeout_sec": 21,
                "default_tool_timeout_sec": 44,
                "shell_timeout_sec": 155,
            },
        }
    )

    assert config.remote_exec.enabled is True
    assert config.remote_exec.host_mode is True
    assert config.remote_exec.relay_bind == "0.0.0.0:9999"
    assert config.remote_exec.bootstrap_token_ttl_sec == 111
    assert config.remote_exec.peer_token_ttl_sec == 222
    assert config.remote_exec.heartbeat_interval_sec == 7
    assert config.remote_exec.heartbeat_timeout_sec == 21
    assert config.remote_exec.default_tool_timeout_sec == 44
    assert config.remote_exec.shell_timeout_sec == 155


def test_parse_config_falls_back_when_active_profile_missing() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "models": {
                "active_main": "missing",
                "profiles": {
                    "first": {"model": "gpt-first", "api_key": "key-1"},
                },
            },
            "modes": {"profiles": {"coder": {}}},
        }
    )

    assert config.active_main_model_profile == "first"
    assert config.active_sub_model_profile == "first"
    assert config.active_model_profile == "first"
    assert config.model == "gpt-first"
