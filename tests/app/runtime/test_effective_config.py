from types import SimpleNamespace

from reuleauxcoder.app.runtime.effective_config import build_effective_config_view
from reuleauxcoder.domain.config.models import Config, ConfigDiagnostic


def test_effective_config_view_marks_session_overrides_and_sources() -> None:
    config = Config(
        active_main_model_profile="main",
        active_sub_model_profile="sub",
        active_mode="coder",
        effective_sources={
            "models.active_main": "workspace",
            "models.active_sub": "global",
            "modes.active": "global",
        },
        diagnostics=[
            ConfigDiagnostic(
                code="legacy_config_alias",
                path="models.active",
                message="legacy alias",
                severity="info",
                source="global",
            )
        ],
    )
    agent = SimpleNamespace(
        active_main_model_profile="session-main",
        active_sub_model_profile="sub",
        active_mode="debugger",
        llm=SimpleNamespace(model="runtime-model"),
    )

    view = build_effective_config_view(config, agent)
    rows = {row.path: row for row in view.rows}

    assert rows["models.active_main"].source == "session"
    assert rows["models.active_sub"].source == "global"
    assert rows["modes.active"].source == "session"
    assert rows["lsp.enabled"].source == "default"
    assert "models.active: legacy alias" in view.diagnostics[0]


def test_effective_config_view_never_exposes_credentials() -> None:
    config = Config(api_key="super-secret", base_url="https://example.test")

    payload = build_effective_config_view(config).to_payload()

    assert "super-secret" not in str(payload)
