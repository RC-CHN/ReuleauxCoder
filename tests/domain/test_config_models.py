from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig, Config, MCPServerConfig, ModeConfig, ModelProfileConfig


def test_mcp_server_config_roundtrip() -> None:
    config = MCPServerConfig(
        name="demo",
        command="npx",
        args=["-y", "server"],
        env={"FOO": "bar"},
        cwd="/tmp",
        enabled=False,
    )
    restored = MCPServerConfig.from_dict("demo", config.to_dict())
    assert restored == config


def test_model_profile_config_from_dict_uses_defaults() -> None:
    profile = ModelProfileConfig.from_dict("main", {})
    assert profile.name == "main"
    assert profile.model == "gpt-4o"
    assert profile.api_key == ""
    assert profile.max_tokens == 4096
    assert profile.temperature == 0.0
    assert profile.preserve_reasoning_content is True
    assert profile.backfill_reasoning_content_for_tool_calls is False


def test_mode_config_from_dict_normalizes_invalid_fields() -> None:
    mode = ModeConfig.from_dict(
        "coder",
        {
            "description": None,
            "tools": ["shell", 123],
            "prompt_append": None,
            "allowed_subagent_modes": "explore",
        },
    )
    assert mode.name == "coder"
    assert mode.description == ""
    assert mode.tools == ["shell", "123"]
    assert mode.prompt_append == ""
    assert mode.allowed_subagent_modes == []


def test_config_validate_collects_multiple_errors() -> None:
    config = Config(
        api_key="",
        max_tokens=0,
        temperature=3.0,
        tool_output_max_chars=0,
        tool_output_max_lines=0,
        active_model_profile="missing",
        active_main_model_profile="missing-main",
        active_sub_model_profile="missing-sub",
        active_mode="missing-mode",
        model_profiles={
            "bad": ModelProfileConfig(
                name="bad",
                model="gpt",
                api_key="",
                max_tokens=0,
                temperature=5.0,
                max_context_tokens=0,
            )
        },
        modes={"coder": ModeConfig(name="coder")},
        approval=ApprovalConfig(
            default_mode="invalid",  # type: ignore[arg-type]
            rules=[ApprovalRuleConfig(action="invalid")],  # type: ignore[arg-type]
        ),
    )

    errors = config.validate()

    assert "api_key is required" in errors
    assert "max_tokens must be positive" in errors
    assert "temperature must be between 0 and 2" in errors
    assert "tool_output_max_chars must be positive" in errors
    assert "tool_output_max_lines must be positive" in errors
    assert "active_model_profile must exist in model_profiles" in errors
    assert "active_main_model_profile must exist in model_profiles" in errors
    assert "active_sub_model_profile must exist in model_profiles" in errors
    assert "active_mode must exist in modes" in errors
    assert "model_profiles[bad].api_key is required" in errors
    assert "model_profiles[bad].max_tokens must be positive" in errors
    assert "model_profiles[bad].max_context_tokens must be positive" in errors
    assert "model_profiles[bad].temperature must be between 0 and 2" in errors
    assert "approval.default_mode must be one of allow, warn, require_approval, deny" in errors
    assert "approval.rules[0].action must be one of allow, warn, require_approval, deny" in errors


def test_config_is_valid_for_minimal_valid_configuration() -> None:
    config = Config(
        api_key="key",
        approval=ApprovalConfig(default_mode="allow"),
    )
    assert config.is_valid() is True


def test_config_supports_llm_debug_trace_flag() -> None:
    config = Config(api_key="key", llm_debug_trace=True)
    assert config.llm_debug_trace is True
