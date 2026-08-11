from types import SimpleNamespace
from reuleauxcoder.app.commands.models import CommandEffect

from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.command.builtin.system import (
    _handle_config,
    _handle_debug,
    _handle_exit,
    _parse_config,
    _parse_debug,
    _handle_status_perf,
    _parse_status_perf,
)
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


def test_parse_debug_on_off() -> None:
    toggle = _parse_debug("/debug", None)
    enabled = _parse_debug("/debug on", None)
    disabled = _parse_debug("/debug off", None)
    assert toggle is not None and toggle.enabled is None
    assert enabled is not None and enabled.enabled is True
    assert disabled is not None and disabled.enabled is False
    assert _parse_debug("/debug maybe", None) is None


def test_status_perf_emits_bounded_typed_view() -> None:
    assert _parse_status_perf("/STATUS PERF", None) is not None
    assert _parse_status_perf("/debug performance", None) is not None
    monitor = RuntimePerformanceMonitor()
    monitor.record(
        "hook",
        "before_llm_request:project_context",
        12.5,
        attributes={"hook_name": "project_context"},
    )
    effect = CommandEffect()
    ctx = SimpleNamespace(
        agent=SimpleNamespace(performance_monitor=monitor),
        effect=effect,
    )

    result = _handle_status_perf(None, ctx)

    view = result.views[-1].view_model
    assert view.view_type == "runtime_performance"
    assert view.retained_count == 1
    assert view.categories[0].category == "hook"
    assert view.slowest[0].detail == "hook_name=project_context"


def test_status_perf_exposes_ui_queue_pressure() -> None:
    monitor = RuntimePerformanceMonitor()
    monitor.record(
        "ui_queue",
        "drain",
        1.5,
        attributes={
            "batch_size": 32,
            "depth": 0,
            "high_watermark": 64,
            "coalesced": 100,
            "transient_dropped": 3,
            "must_deliver_waits": 2,
            "must_deliver_timeouts": 1,
            "closed_dropped": 4,
        },
    )
    effect = CommandEffect()
    ctx = SimpleNamespace(
        agent=SimpleNamespace(performance_monitor=monitor),
        effect=effect,
    )

    view = _handle_status_perf(None, ctx).views[-1].view_model

    assert view.categories[0].category == "ui_queue"
    detail = view.recent[0].detail
    assert "high_watermark=64" in detail
    assert "coalesced=100" in detail
    assert "transient_dropped=3" in detail
    assert "must_deliver_waits=2" in detail
    assert "must_deliver_timeouts=1" in detail
    assert "closed_dropped=4" in detail


def test_status_perf_exposes_secret_free_lsp_phase_details() -> None:
    monitor = RuntimePerformanceMonitor()
    monitor.record(
        "lsp",
        "request",
        4.5,
        attributes={
            "language": "python",
            "root_hash": "abc123def456",
            "transport_generation": 2,
            "launcher": "pyright-langserver",
            "request_kind": "definition",
            "document_version": 3,
        },
    )
    effect = CommandEffect()
    ctx = SimpleNamespace(
        agent=SimpleNamespace(performance_monitor=monitor),
        effect=effect,
    )

    detail = _handle_status_perf(None, ctx).views[-1].view_model.recent[0].detail

    assert detail == (
        "language=python · root_hash=abc123def456 · transport_generation=2 · "
        "launcher=pyright-langserver · request_kind=definition · "
        "document_version=3"
    )


def test_handle_debug_toggles_runtime_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    effect = CommandEffect()
    llm = SimpleNamespace(debug_trace=False)
    config = SimpleNamespace(llm_debug_trace=False)
    ctx = SimpleNamespace(
        config=config,
        agent=SimpleNamespace(llm=llm),
        effect=effect,
    )

    result = _handle_debug(SimpleNamespace(enabled=True), ctx)
    assert ctx.config.llm_debug_trace is False
    assert ctx.agent.llm.debug_trace is True
    assert result.state == {"llm_debug_trace": True}

    result = _handle_debug(SimpleNamespace(enabled=False), ctx)
    assert ctx.config.llm_debug_trace is False
    assert ctx.agent.llm.debug_trace is False
    assert result.state == {"llm_debug_trace": False}

    result = _handle_debug(SimpleNamespace(enabled=None), ctx)
    assert ctx.agent.llm.debug_trace is True
    assert result.state == {"llm_debug_trace": True}
    assert "session event ledger remains bounded" in (
        result.notifications[-1].message.lower()
    )


def test_config_command_emits_typed_effective_view() -> None:
    assert _parse_config("/config", None) is not None
    effect = CommandEffect()
    config = Config()
    agent = SimpleNamespace(
        active_main_model_profile=None,
        active_sub_model_profile=None,
        active_mode=None,
        llm=SimpleNamespace(model="demo"),
    )

    result = _handle_config(
        None, SimpleNamespace(config=config, agent=agent, effect=effect)
    )

    view = result.views[-1]
    assert view.view_type == "effective_config"
    assert view.view_model.view_type == "effective_config"
    assert result.state["rows"]


def test_exit_respects_disabled_auto_save(tmp_path) -> None:
    config = Config(session_auto_save=False)
    agent = SimpleNamespace(
        messages=[{"role": "user", "content": "do not persist"}],
        llm=SimpleNamespace(model="demo"),
        state=SimpleNamespace(total_prompt_tokens=0, total_completion_tokens=0),
        active_mode=None,
    )
    ctx = SimpleNamespace(
        config=config,
        agent=agent,
        effect=CommandEffect(),
        sessions_dir=tmp_path,
    )

    result = _handle_exit(SimpleNamespace(current_session_id=None), ctx)

    assert result.control == "exit"
    assert not list(tmp_path.iterdir())


def test_exit_routes_auto_save_through_lifecycle(tmp_path) -> None:
    saved = []
    config = Config(session_auto_save=True)
    agent = SimpleNamespace(
        messages=[{"role": "user", "content": "persist"}],
        llm=SimpleNamespace(model="demo"),
        state=SimpleNamespace(total_prompt_tokens=0, total_completion_tokens=0),
        active_mode=None,
        lifecycle=SimpleNamespace(session_saved=saved.append),
        session_approval_rules=[],
        active_main_model_profile=None,
        active_sub_model_profile=None,
    )
    ctx = SimpleNamespace(
        config=config,
        agent=agent,
        effect=CommandEffect(),
        sessions_dir=tmp_path,
    )

    _handle_exit(SimpleNamespace(current_session_id=None), ctx)

    assert len(saved) == 1
    assert SessionStore(tmp_path).load(saved[0]) is not None
