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
                    "main": {"model": "gpt-main", "api_key": "main-key", "temperature": 0.1},
                    "sub": {"model": "gpt-sub", "api_key": "sub-key", "temperature": 0.2},
                },
            },
            "modes": {
                "active": "coder",
                "profiles": {
                    "coder": {"description": "Code mode", "tools": ["bash", "read_file"]}
                },
            },
            "approval": {
                "default_mode": "warn",
                "rules": [{"tool_name": "bash", "action": "deny"}],
            },
            "skills": {"enabled": True, "scan_project": False, "disabled": ["demo"]},
        }
    )

    assert config.model == "gpt-main"
    assert config.api_key == "main-key"
    assert config.active_model_profile == "main"
    assert config.active_main_model_profile == "main"
    assert config.active_sub_model_profile == "sub"
    assert config.active_mode == "coder"
    assert config.modes["coder"].tools == ["bash", "read_file"]
    assert config.approval.default_mode == "warn"
    assert config.approval.rules[0].tool_name == "bash"
    assert config.skills.scan_project is False
    assert config.skills.disabled == ["demo"]


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
