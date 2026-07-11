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
    assert rows["lsp.typescript_mode"].value == "auto"
    assert "models.active: legacy alias" in view.diagnostics[0]


def test_effective_config_view_never_exposes_credentials() -> None:
    config = Config(api_key="super-secret", base_url="https://example.test")

    payload = build_effective_config_view(config).to_payload()

    assert "super-secret" not in str(payload)


def test_effective_config_includes_runtime_scope_diagnostics() -> None:
    peer = SimpleNamespace(
        peer_id="peer-1",
        capabilities=["fs.read", "process.start"],
        meta={"protocol_version": 2},
    )
    job = SimpleNamespace(
        id="sj_1",
        status="running",
        generation=3,
        parent_agent_id="agent-1",
    )
    agent = SimpleNamespace(
        active_main_model_profile=None,
        active_sub_model_profile=None,
        active_mode=None,
        llm=SimpleNamespace(model="runtime-model"),
        extension_manager=SimpleNamespace(
            describe_graph=lambda: ("core.hooks [50]",),
            describe_scopes=lambda: ("runner:runner -> core.hooks",),
        ),
        lsp_manager=SimpleNamespace(
            describe_scopes=lambda: ("python:/workspace",)
        ),
        relay_server=SimpleNamespace(
            registry=SimpleNamespace(list_online=lambda: [peer])
        ),
        _subagent_manager=SimpleNamespace(list_jobs=lambda: [job]),
    )

    view = build_effective_config_view(Config(), agent)

    assert view.extension_graph == ("core.hooks [50]",)
    assert view.extension_scopes == ("runner:runner -> core.hooks",)
    assert view.lsp_scopes == ("python:/workspace",)
    assert view.peer_capabilities == ("peer-1: v2 fs.read,process.start",)
    assert view.active_jobs == ("sj_1:running:g3:agent-1",)
