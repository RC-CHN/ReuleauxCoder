"""Goal commands, interactions and panel, shared by CLI and TUI."""

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.panels import (
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
)
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.commands.shared import EmptyCommand, UI_TARGETS, slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.app.commands.view_models import GoalViewModel
from reuleauxcoder.app.interaction_contracts import InputTextRequest


@dataclass(frozen=True)
class ObjectiveCommand:
    objective: str | None = None


@dataclass(frozen=True)
class BudgetCommand:
    token_budget: int | None = None


def _show(command, ctx):
    view = GoalViewModel(
        ctx.agent.goal_controller.state, ctx.config.goal_default_token_budget
    )
    ctx.effect.open_view(view, title="Goal", reuse_key="goal")
    return ctx.effect.finish(control="continue")


def _text(ctx, title, prompt, initial=""):
    response = ctx.ui_interactor.input_text(
        InputTextRequest(title, prompt, initial_value=initial)
    )
    return None if response.cancelled else response.value


def _change(ctx, operation):
    try:
        operation()
        goal = ctx.agent.goal_controller.state
        if goal:
            budget = f"{goal.token_budget:,}" if goal.token_budget else "No limit"
            ctx.effect.info(
                f"Goal {goal.status} · Tokens {goal.tokens_used:,} / {budget}"
            )
        else:
            ctx.effect.info("Goal cleared")
    except ValueError as error:
        ctx.effect.warning(str(error))
    return ctx.effect.finish(control="continue")


def _create(command, ctx):
    objective = command.objective
    if objective is None:
        objective = _text(
            ctx,
            "Create goal",
            "Describe the full objective and how to verify completion",
        )
    if objective is None:
        return ctx.effect.finish(control="continue")

    def create():
        ctx.agent.goal_controller.create(
            objective, ctx.config.goal_default_token_budget
        )
        ctx.agent.clear_stop_request()

    return _change(ctx, create)


def _edit(command, ctx):
    goal = ctx.agent.goal_controller.state
    if goal is None:
        ctx.effect.warning("No goal is set")
        return ctx.effect.finish(control="continue")
    objective = command.objective
    if objective is None:
        objective = _text(
            ctx,
            "Edit goal",
            "Update the objective; status and usage are preserved",
            goal.objective,
        )
    if objective is None:
        return ctx.effect.finish(control="continue")
    return _change(ctx, lambda: ctx.agent.goal_controller.update(objective=objective))


def _budget(command, ctx):
    return _change(
        ctx,
        lambda: ctx.agent.goal_controller.update(
            token_budget=command.token_budget, change_budget=True
        ),
    )


def _budget_prompt(command, ctx):
    goal = ctx.agent.goal_controller.state
    if goal is None:
        ctx.effect.warning("No goal is set")
        return ctx.effect.finish(control="continue")
    value = _text(
        ctx,
        "Goal token budget",
        "Cumulative token limit; enter a positive integer or none. Changing the limit does not resume a stopped goal.",
        str(goal.token_budget) if goal.token_budget else "none",
    )
    if value is None:
        return ctx.effect.finish(control="continue")
    try:
        budget = None if value.strip().lower() == "none" else int(value)
    except ValueError:
        ctx.effect.warning("Enter a positive integer or none")
        return ctx.effect.finish(control="continue")
    return _budget(BudgetCommand(budget), ctx)


def _status(status):
    def handler(command, ctx):
        def update():
            ctx.agent.goal_controller.update(status=status)
            if status == "active":
                ctx.agent.clear_stop_request()

        return _change(ctx, update)

    return handler


def _clear(command, ctx):
    return _change(ctx, ctx.agent.goal_controller.clear)


def _parse_objective(prefix):
    def parse(text, _):
        if text.strip() == prefix:
            return ObjectiveCommand()
        captures = match_template(text, prefix + " {objective+}")
        return ObjectiveCommand(captures["objective"]) if captures else None

    return parse


def _parse_budget(text, _):
    captures = match_template(text, "/goal budget {tokens}")
    if not captures:
        return None
    try:
        return BudgetCommand(
            None if captures["tokens"].lower() == "none" else int(captures["tokens"])
        )
    except ValueError:
        return None


def command_panel_spec():
    def build(model, title):
        goal = model.goal
        items = []
        if goal:
            budget = (
                f"{goal.tokens_used:,} / {goal.token_budget:,}"
                if goal.token_budget
                else f"{goal.tokens_used:,} · No limit"
            )
            items.extend(
                [
                    PanelItem(goal.status.replace("_", " ").title(), goal.objective),
                    PanelItem(
                        "Tokens",
                        budget
                        + (
                            f" · {goal.estimated_requests} estimated requests"
                            if goal.estimated_requests
                            else ""
                        ),
                    ),
                    PanelItem("Elapsed", f"{int(goal.time_used_seconds)} seconds"),
                ]
            )
        actions = [
            (
                "Create goal",
                "create",
                "Describe an objective · Default budget: "
                + (
                    f"{model.default_token_budget:,} tokens"
                    if model.default_token_budget is not None
                    else "No limit"
                ),
            )
        ]
        if goal and goal.status != "complete":
            actions = [("Edit objective", "edit", "Preserve status, budget and usage")]
            actions += (
                [
                    (
                        "Pause after turn",
                        "pause",
                        "Let the current turn finish; stop automatic continuation",
                    )
                ]
                if goal.status == "active"
                else [
                    ("Resume", "resume", "Continue this goal with its remaining budget")
                ]
            )
        if goal and goal.status != "complete":
            actions += [
                (
                    "Token budget",
                    "budget_prompt",
                    "Change the cumulative limit or remove it",
                ),
            ]
        if goal:
            actions += [
                (
                    "Clear goal",
                    "clear",
                    "Remove this goal; current execution is not interrupted",
                ),
            ]
        items.extend(
            PanelItem(
                label,
                description,
                ActionRequest(
                    f"goal.{action}",
                    ObjectiveCommand()
                    if action in {"create", "edit"}
                    else EmptyCommand(),
                ),
            )
            for label, action, description in actions
        )
        return PanelDefinition("goal", title, tuple(items), keep_open_on_submit=True)

    return CommandPanelSpec("goal", GoalViewModel, build)


def register_actions(registry):
    specs = [
        ("show", "/goal", "View goal and controls", EmptyCommand, _show, None),
        (
            "create",
            "/goal create",
            "Create a persistent goal",
            ObjectiveCommand,
            _create,
            _parse_objective("/goal create"),
        ),
        (
            "edit",
            "/goal edit",
            "Edit the goal objective",
            ObjectiveCommand,
            _edit,
            _parse_objective("/goal edit"),
        ),
        (
            "pause",
            "/goal pause",
            "Pause automatic continuation after the current turn",
            EmptyCommand,
            _status("paused"),
            None,
        ),
        (
            "resume",
            "/goal resume",
            "Resume the goal",
            EmptyCommand,
            _status("active"),
            None,
        ),
        ("clear", "/goal clear", "Clear the goal", EmptyCommand, _clear, None),
        (
            "budget_prompt",
            "/goal budget",
            "Change the token budget interactively",
            EmptyCommand,
            _budget_prompt,
            None,
        ),
        (
            "budget",
            "/goal budget <tokens|none>",
            "Set a cumulative token limit; none removes it",
            BudgetCommand,
            _budget,
            _parse_budget,
        ),
    ]
    for name, trigger, description, command_type, handler, parser in specs:
        registry.register(
            ActionSpec(
                action_id=f"goal.{name}",
                feature_id="goal",
                description=description,
                command_type=command_type,
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger(trigger),),
                parser=parser
                or (
                    lambda text, _, trigger=trigger: (
                        EmptyCommand() if text.strip() == trigger else None
                    )
                ),
                handler=handler,
                preview=name == "show",
                during_turn=DuringTurnPolicy.DEFER_UNTIL_IDLE
                if name in {"create", "resume"}
                else DuringTurnPolicy.IMMEDIATE,
            )
        )
