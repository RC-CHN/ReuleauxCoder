from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
    DEFAULT_REASONING_EFFORT_VALUES,
    MCPServerConfig,
    ModeConfig,
    ModelProfileConfig,
    RemoteExecConfig,
    ResponsesConfig,
    UIConfig,
)


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
    assert profile.provider == "openai-compatible"
    assert profile.request_mode is None
    assert profile.responses == ResponsesConfig()
    assert profile.responses.cache.mode == "implicit"
    assert profile.max_tokens == 4096
    assert profile.temperature == 0.0
    assert profile.preserve_reasoning_content is True
    assert profile.backfill_reasoning_content_for_tool_calls is False


def test_model_profile_config_roundtrips_native_provider() -> None:
    profile = ModelProfileConfig.from_dict(
        "native",
        {
            "model": "claude-test",
            "api_key": "key",
            "provider": "anthropic",
        },
    )

    assert profile.provider == "anthropic"
    assert profile.to_dict()["provider"] == "anthropic"


def test_model_profile_config_roundtrips_responses_mode() -> None:
    profile = ModelProfileConfig.from_dict(
        "responses",
        {
            "model": "gpt-5.6-luna",
            "api_key": "key",
            "request_mode": "responses",
            "responses": {
                "state": "local",
                "cache": {"mode": "implicit"},
            },
        },
    )

    assert profile.request_mode == "responses"
    assert profile.responses.state == "local"
    assert profile.responses.cache.mode == "implicit"
    assert profile.to_dict()["responses"] == {
        "state": "local",
        "cache": {"mode": "implicit"},
    }


def test_config_rejects_incompatible_request_mode() -> None:
    config = Config(
        api_key="key",
        provider="anthropic",
        request_mode="responses",
    )

    assert "anthropic provider requires request_mode messages" in config.validate()


def test_model_profile_config_default_reasoning_effort_param() -> None:
    profile = ModelProfileConfig.from_dict("main", {})
    assert profile.reasoning_effort_param == "reasoning_effort"
    assert profile.reasoning_effort_values is None


def test_model_profile_config_custom_reasoning_effort_param() -> None:
    profile = ModelProfileConfig.from_dict(
        "deepseek",
        {
            "model": "deepseek-chat",
            "api_key": "sk-xx",
            "reasoning_effort": "high",
            "reasoning_effort_param": "thinking_level",
            "reasoning_effort_values": {"low": "high", "medium": "high", "high": "max"},
        },
    )
    assert profile.reasoning_effort == "high"
    assert profile.reasoning_effort_param == "thinking_level"
    assert profile.reasoning_effort_values == {
        "low": "high",
        "medium": "high",
        "high": "max",
    }


def test_model_profile_config_reasoning_effort_values_roundtrip() -> None:
    profile = ModelProfileConfig(
        name="test",
        model="m",
        api_key="k",
        reasoning_effort="high",
        reasoning_effort_values={"low": 1, "medium": 5, "high": 10},
        reasoning_effort_param="think",
    )
    restored = ModelProfileConfig.from_dict("test", profile.to_dict())
    assert restored.reasoning_effort_values == {"low": 1, "medium": 5, "high": 10}
    assert restored.reasoning_effort_param == "think"


def test_default_reasoning_effort_values() -> None:
    assert DEFAULT_REASONING_EFFORT_VALUES == {
        "low": "low",
        "medium": "medium",
        "high": "high",
    }


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
        provider="unsupported",
        max_tokens=0,
        temperature=3.0,
        tool_output_max_chars=0,
        tool_output_max_lines=0,
        ui=UIConfig(
            verbosity="verbose",  # type: ignore[arg-type]
            max_preview_chars=0,
            max_preview_lines=0,
        ),
        active_model_profile="missing",
        active_main_model_profile="missing-main",
        active_sub_model_profile="missing-sub",
        active_mode="missing-mode",
        model_profiles={
            "bad": ModelProfileConfig(
                name="bad",
                model="gpt",
                api_key="",
                provider="unsupported",
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
    assert "provider must be openai-compatible or anthropic" in errors
    assert (
        "model_profiles[bad].provider must be openai-compatible or anthropic"
        in errors
    )
    assert "tool_output_max_chars must be positive" in errors
    assert "tool_output_max_lines must be positive" in errors
    assert "ui.max_preview_chars must be positive" in errors
    assert "ui.max_preview_lines must be positive" in errors
    assert "ui.verbosity must be compact, standard, or debug" in errors
    assert "active_model_profile must exist in model_profiles" in errors
    assert "active_main_model_profile must exist in model_profiles" in errors
    assert "active_sub_model_profile must exist in model_profiles" in errors
    assert "active_mode must exist in modes" in errors
    assert "model_profiles[bad].api_key is required" in errors
    assert "model_profiles[bad].max_tokens must be positive" in errors
    assert "model_profiles[bad].max_context_tokens must be positive" in errors
    assert "model_profiles[bad].temperature must be between 0 and 2" in errors
    assert (
        "approval.default_mode must be one of allow, warn, require_approval, deny"
        in errors
    )
    assert (
        "approval.rules[0].action must be one of allow, warn, require_approval, deny"
        in errors
    )


def test_config_is_valid_for_minimal_valid_configuration() -> None:
    config = Config(
        api_key="key",
        approval=ApprovalConfig(default_mode="allow"),
    )
    assert config.is_valid() is True


def test_auto_review_config_rejects_missing_or_unknown_profile() -> None:
    missing = Config(
        api_key="key",
        approval=ApprovalConfig(reviewer="auto_review"),
    )
    unknown = Config(
        api_key="key",
        approval=ApprovalConfig(
            reviewer="auto_review", auto_review_model_profile="guardian"
        ),
    )

    assert (
        "approval.auto_review_model_profile is required for auto_review"
        in missing.validate()
    )
    assert (
        "approval.auto_review_model_profile must exist in model_profiles"
        in unknown.validate()
    )


def test_config_supports_llm_debug_trace_flag() -> None:
    config = Config(api_key="key", llm_debug_trace=True)
    assert config.llm_debug_trace is True


def test_web_private_networks_are_allowed_by_default() -> None:
    config = Config(api_key="key")
    assert config.web_allow_private_networks is True


def test_remote_exec_config_defaults() -> None:
    config = Config(api_key="key")
    assert isinstance(config.remote_exec, RemoteExecConfig)
    assert config.remote_exec.enabled is False
    assert config.remote_exec.host_mode is False
    assert config.remote_exec.relay_bind == "127.0.0.1:8765"
