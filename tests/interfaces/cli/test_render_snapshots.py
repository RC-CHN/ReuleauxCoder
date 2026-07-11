from __future__ import annotations

import pytest
from rich.console import Console

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.events import UIEvent
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry


@pytest.mark.parametrize(
    ("width", "expected"),
    [
        (
            80,
            "› shell(command='python -m pytest tests/unit --maxfail=1', timeout=120, …)\n"
            "  540 passed in 120.63s\n",
        ),
        (
            120,
            "› shell(command='python -m pytest tests/unit --maxfail=1', timeout=120, …)\n"
            "  540 passed in 120.63s\n",
        ),
    ],
)
def test_compact_tool_lifecycle_width_snapshot(width: int, expected: str) -> None:
    console = Console(
        record=True, width=width, color_system=None, force_terminal=False
    )
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]), console_override=console
    )
    arguments = {
        "command": "python -m pytest tests/unit --maxfail=1",
        "timeout": 120,
        "description": "run focused regression tests",
    }

    renderer.on_event(
        AgentEvent.tool_call_start("shell", arguments, tool_call_id="call-1")
    )
    renderer.on_event(
        AgentEvent.tool_call_end(
            "shell", "540 passed in 120.63s", tool_call_id="call-1"
        )
    )

    assert console.export_text() == expected


def test_compact_notification_snapshot() -> None:
    console = Console(
        record=True, width=80, color_system=None, force_terminal=False
    )
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]), console_override=console
    )

    renderer.on_ui_event(UIEvent.info("Loaded session"))
    renderer.on_ui_event(UIEvent.success("Saved session"))
    renderer.on_ui_event(UIEvent.debug("internal detail"))

    assert console.export_text() == "Loaded session\n✓ Saved session\n"
