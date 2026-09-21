from types import SimpleNamespace
import shutil

from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.rpc.codec import encode, decode
from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.shell import ShellCatalog, WslDistribution
from reuleauxcoder.extensions.command.builtin.shell import (
    ListShells,
    SelectShell,
    _handle_list,
    _handle_select,
    _parse_list,
    _parse_select,
    command_panel_spec,
)
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.infrastructure import shells


def test_shell_slash_parsing_preserves_windows_paths_and_distribution_names():
    assert _parse_list("/shell", None) == ListShells()
    assert _parse_list("/shell wsl Ubuntu 24.04", None) == ListShells("Ubuntu 24.04")
    assert _parse_select(
        '/shell use "C:\\Program Files\\Git\\bin\\bash.exe"', None
    ) == SelectShell(r"C:\Program Files\Git\bin\bash.exe")
    assert _parse_select("/shell auto", None) == SelectShell("auto")
    assert _parse_select("/shell use", None) is None


def test_picker_roundtrip_and_selection_update_model_schema_without_new_discovery(
    monkeypatch,
):
    option = shells._option(
        "fish",
        "/test/fish",
        "WSL / Ubuntu",
        distribution="Ubuntu",
        launcher=r"C:\Windows\System32\wsl.exe",
    )
    calls = []

    def discover(distribution=None):
        calls.append(distribution)
        return ShellCatalog((option,), (WslDistribution("Ubuntu", option.launcher),))

    monkeypatch.setattr(shells, "discover_shells", discover)
    monkeypatch.setattr(shutil, "which", lambda value: value)
    tool = ShellTool()
    ctx = SimpleNamespace(agent=SimpleNamespace(tools=[tool]), effect=CommandEffect())
    loop = AgentLoop(
        SimpleNamespace(get_active_tools=lambda: [tool]),
        prompt_fn=lambda: "",
        shell_name="bash",
    )
    before = loop._tool_schemas()[0]["function"]["description"]
    _handle_list(ListShells(), ctx)
    view = ctx.effect.views[0].view_model
    assert decode(encode(view)) == view
    panel = command_panel_spec().build(view, "Shell")
    assert option.path in panel.items[1].description
    assert panel.items[1].action.command == SelectShell(option.id)
    assert panel.items[2].action.command == ListShells("Ubuntu")
    ctx.effect = CommandEffect()
    _handle_select(SelectShell(option.id), ctx)
    assert calls == [None], (
        "choosing an already listed shell does not rerun WSL discovery"
    )
    assert tool.backend.process.shells.selected == option
    after = loop._tool_schemas()[0]["function"]["description"]
    assert (
        after != before
        and "Fish" in after
        and "/test/fish" in after
        and "WSL / Ubuntu" in after
    )
    assert "WSL / Ubuntu" in tool.shell_environment
    ctx.effect = CommandEffect()
    _handle_select(SelectShell("auto"), ctx)
    assert tool.backend.process.shells.selected is None
    assert loop._tool_schemas()[0]["function"]["description"] == before


def test_remote_backend_does_not_offer_host_shells():
    ctx = SimpleNamespace(
        agent=SimpleNamespace(
            tools=[
                SimpleNamespace(name="shell", backend=SimpleNamespace(process=object()))
            ]
        ),
        effect=CommandEffect(),
    )
    _handle_list(ListShells(), ctx)
    assert not ctx.effect.views
    assert "Remote peers" in ctx.effect.notifications[0].message


def test_discovery_timeout_is_an_action_error_and_does_not_change_selection(
    monkeypatch,
):
    import subprocess

    tool = ShellTool()
    ctx = SimpleNamespace(agent=SimpleNamespace(tools=[tool]), effect=CommandEffect())

    def timeout(distribution=None):
        raise subprocess.TimeoutExpired("wsl.exe", 8)

    monkeypatch.setattr(shells, "discover_shells", timeout)
    _handle_list(ListShells("Ubuntu"), ctx)
    assert ctx.effect.notifications[0].level == "error"
    assert tool.backend.process.shells.selected is None
