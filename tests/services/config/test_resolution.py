"""Persisted values, fresh startup and session profile selection must agree."""

import pytest
import yaml

from reuleauxcoder.app.runtime.model_profiles import apply_main_model_profile
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.config.validation import resolve_layers
from reuleauxcoder.services.llm.factory import build_llm_from_settings


def test_file_defaults_reach_startup_and_session_client_identically(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "app": {
            "api_key": "synthetic", "model": "inherited", "temperature": 0.4,
            "request_mode": "responses", "reasoning_effort": "high",
            "responses": {"cache": {"mode": "explicit"}}, "support_modal": ["text", "image"],
            "preserve_reasoning_content": False,
        },
        "models": {"active_main": "main", "profiles": {
            "main": {"model": "main-model", "reasoning_effort_values": {"high": "high"}},
            "other": {"request_mode": None, "reasoning_effort": None, "temperature": 0},
        }},
        "context": {"reserved_output_tokens": 333, "fixed_prompt_tokens": 444,
                    "tool_schema_tokens": 555, "safety_margin_tokens": 666},
    }))
    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", path)
    monkeypatch.setattr(ConfigLoader, "WORKSPACE_CONFIG_PATH", tmp_path / "workspace.yaml")
    config = ConfigLoader().load()
    client = build_llm_from_settings(config)
    try:
        agent = Agent(client, [], config=config)
        before = (client.model, client.temperature, client.request_mode, client.reasoning_effort,
                  client.responses_cache_mode, client.support_modal, client.preserve_reasoning_content)
        apply_main_model_profile(config, agent, "main", config.model_profiles["main"])
        assert before == (client.model, client.temperature, client.request_mode, client.reasoning_effort,
                          client.responses_cache_mode, client.support_modal, client.preserve_reasoning_content)
        assert before == ("main-model", 0.4, "responses", "high", "explicit", ("text", "image"), False)
        assert agent.context._budget.reserved_output == 333
        assert agent.context._budget.fixed_prompt_tokens == 444
        assert agent.context._budget.tool_schema_tokens == 555
        assert agent.context._budget.safety_margin == 666
        apply_main_model_profile(config, agent, "other", config.model_profiles["other"])
        assert client.model == "inherited"
        assert client.request_mode == "chat-completions"
        assert client.reasoning_effort is None and client.temperature == 0
    finally:
        client.close()


@pytest.mark.parametrize("section", [
    {"app": {"reasoning_replay_mode": "typo"}},
    {"context": {"reserved_output_tokens": -1}},
    {"lsp": {"servers": {"not-a-language": {"cmd": "server"}}}},
    {"remote_exec": {"relay_bind": "not an address"}},
    {"remote_exec": {"relay_bind": "localhost:70000"}},
    {"remote_exec": {"heartbeat_interval_sec": -1}},
    {"remote_exec": {"heartbeat_interval_sec": 30, "heartbeat_timeout_sec": 20}},
    {"models": {"profiles": {"unsafe": {"api_key": "test", "reasoning_effort_param": "messages"}}}},
])
def test_bad_semantic_values_are_rejected(section):
    data = {"app": {"api_key": "test"}}
    data = ConfigLoader()._merge_dicts(data, section)
    _, issues = resolve_layers([("workspace", data)])
    assert any(issue.severity == "error" for issue in issues)


def test_schema_reports_limits_and_model_authority():
    from reuleauxcoder.services.config.definition import config_schema

    schema = config_schema()["properties"]
    assert schema["models"]["properties"]["active"]["deprecated"]
    assert not schema["models"]["properties"]["active_main"].get("deprecated")
    assert schema["context"]["properties"]["safety_margin_tokens"]["minimum"] == 0
    assert schema["approval"]["x-rcoder"]["model_write"] == "user_required"
    model = schema["models"]["properties"]["profiles"]["additionalProperties"]
    assert model["x-rcoder"]["model_write"] == "conditional"
    assert model["properties"]["api_key"]["x-rcoder"]["model_write"] == "user_required"
    server = schema["mcp"]["properties"]["servers"]["additionalProperties"]
    assert server["properties"]["command"]["x-rcoder"]["reason"] == "process_launch"
    assert schema["skills"]["properties"]["disabled"]["default"] == []
