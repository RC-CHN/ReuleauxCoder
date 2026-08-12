from __future__ import annotations

from unittest.mock import MagicMock

from tests import process_helpers


def test_posix_process_probe_uses_signal_zero(monkeypatch) -> None:
    kill = MagicMock()
    monkeypatch.setattr(process_helpers.os, "name", "posix")
    monkeypatch.setattr(process_helpers.os, "kill", kill)

    assert process_helpers.process_is_alive(42)

    kill.assert_called_once_with(42, 0)


def test_windows_process_probe_never_uses_os_kill(monkeypatch) -> None:
    kill = MagicMock()
    windows_probe = MagicMock(return_value=True)
    monkeypatch.setattr(process_helpers.os, "name", "nt")
    monkeypatch.setattr(process_helpers.os, "kill", kill)
    monkeypatch.setattr(process_helpers, "_windows_process_is_alive", windows_probe)

    assert process_helpers.process_is_alive(42)

    windows_probe.assert_called_once_with(42)
    kill.assert_not_called()
