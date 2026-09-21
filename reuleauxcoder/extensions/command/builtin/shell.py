"""Pick an execution shell without changing the host's default shell."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.panels import (
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.commands.shared import UI_TARGETS, slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.app.commands.shell_views import ShellsView
from reuleauxcoder.infrastructure.shells import LocalShellSelection


@dataclass(frozen=True, slots=True)
class ListShells:
    distribution: str | None = None


@dataclass(frozen=True, slots=True)
class SelectShell:
    selector: str


def _parse_list(text, _context):
    if text.strip() in {"/shell", "/shell list", "/shell refresh"}:
        return ListShells()
    match = match_template(text, "/shell wsl {distribution+}")
    return ListShells(match["distribution"].strip()) if match else None


def _parse_select(text, _context):
    if text.strip() == "/shell auto":
        return SelectShell("auto")
    match = match_template(text, "/shell use {selector+}")
    if match:
        selector = match["selector"].strip()
        if len(selector) > 1 and selector[0] == selector[-1] and selector[0] in "\"'":
            selector = selector[1:-1]
        return SelectShell(selector)
    return None


def _selection(ctx) -> LocalShellSelection | None:
    tool = next((tool for tool in ctx.agent.tools if tool.name == "shell"), None)
    selection = getattr(
        getattr(getattr(tool, "backend", None), "process", None), "shells", None
    )
    if isinstance(selection, LocalShellSelection):
        return selection
    ctx.effect.error(
        "Shell selection is unavailable for this execution backend. Remote peers keep their own native shell."
    )
    return None


def _show(ctx, selection, distribution=None):
    view = ShellsView(
        selection.catalog(distribution), selection.current(), selection.selected is None
    )
    ctx.effect.open_view(view, title="Execution Shell", reuse_key="shells")
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_list(command, ctx):
    selection = _selection(ctx)
    if selection is None:
        return ctx.effect.finish(control="continue")
    try:
        return _show(ctx, selection, command.distribution)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        ctx.effect.error(f"Could not discover shells: {error}")
        return ctx.effect.finish(control="continue")


def _handle_select(command, ctx):
    selection = _selection(ctx)
    if selection is None:
        return ctx.effect.finish(control="continue")
    try:
        current = selection.select(command.selector)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        ctx.effect.error(str(error))
        return ctx.effect.finish(control="continue")
    ctx.effect.success(
        f"Shell: {current.summary if current else 'automatic (unavailable)'}. Applies to new commands in this runtime."
    )
    # No second WSL probe after selection; keep this action immediate and leave
    # the next explicit /shell request responsible for refreshing discovery.
    return ctx.effect.finish(
        control="continue",
        state_changes={"execution_shell": current.summary if current else None},
    )


def command_panel_spec() -> CommandPanelSpec:
    def build(model, title):
        assert isinstance(model, ShellsView)
        items = [
            PanelItem(
                "Automatic",
                "Use the runtime's default shell",
                ActionRequest("shell.select", SelectShell("auto")),
                current=model.automatic,
            )
        ]
        items.extend(
            PanelItem(
                option.name,
                f"{option.path} · {option.environment}",
                ActionRequest("shell.select", SelectShell(option.id)),
                current=not model.automatic
                and model.current is not None
                and model.current.id == option.id,
            )
            for option in model.catalog.options
        )
        items.extend(
            PanelItem(
                f"WSL / {distro.name}",
                f"{distro.launcher} · Discover shells in this distribution",
                ActionRequest("shell.list", ListShells(distro.name)),
            )
            for distro in model.catalog.distributions
        )
        if model.catalog.distribution:
            items.append(
                PanelItem(
                    "Windows shells",
                    "Back to native shells and WSL distributions",
                    ActionRequest("shell.list", ListShells()),
                )
            )
        body = f"Current: {model.current.summary if model.current else 'unavailable'}\nNew commands only · This runtime · Existing processes keep their shell"
        if model.catalog.diagnostics:
            body += "\n" + "\n".join(model.catalog.diagnostics)
        return PanelDefinition(
            model.view_type, title, tuple(items), body=body, filterable=True
        )

    return CommandPanelSpec("shells", ShellsView, build)


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="shell.list",
                preview=True,
                command_type=ListShells,
                feature_id="shell",
                description="List shell names, paths and WSL distributions",
                ui_targets=UI_TARGETS,
                triggers=(
                    slash_trigger("/shell"),
                    slash_trigger("/shell wsl <distribution>"),
                ),
                parser=_parse_list,
                handler=_handle_list,
            ),
            ActionSpec(
                action_id="shell.select",
                command_type=SelectShell,
                audit="runtime_config_changed",
                feature_id="shell",
                description="[session] Choose the shell for new commands (or restore auto)",
                ui_targets=UI_TARGETS,
                triggers=(
                    slash_trigger("/shell use <id|name|path>"),
                    slash_trigger("/shell auto"),
                ),
                parser=_parse_select,
                handler=_handle_select,
            ),
        ]
    )
