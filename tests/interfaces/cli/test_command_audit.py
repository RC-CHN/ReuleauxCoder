from types import SimpleNamespace

from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.interfaces.cli.commands import _record_command_control_event


def test_mutating_command_state_is_ledgered() -> None:
    ledger = HistoryLedger(session_id="session", agent_id="agent")
    persisted = []
    agent = SimpleNamespace(
        history_ledger=ledger,
        agent_id="agent",
        _current_turn_id=None,
        persist_runtime_snapshot=lambda: persisted.append(True),
    )
    effect = CommandEffect().finish(
        state_changes={"active_mode": "coder"}
    )

    _record_command_control_event(agent, "mode.switch", effect)

    assert ledger.events[-1].kind == "runtime_config_changed"
    assert ledger.events[-1].payload["state_changes"] == {
        "active_mode": "coder"
    }
    assert persisted == [True]


def test_read_only_command_does_not_pollute_control_ledger() -> None:
    ledger = HistoryLedger()
    agent = SimpleNamespace(history_ledger=ledger)
    _record_command_control_event(agent, "system.help", CommandEffect())
    assert ledger.events == ()
