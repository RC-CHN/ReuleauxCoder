"""Manual context compaction: choose a strategy instead of entering internal fields."""

from dataclasses import dataclass

from reuleauxcoder.app.commands.capabilities import UICapability
from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.panels import (
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
)
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.commands.shared import UI_TARGETS, slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.app.commands.view_models import CompactViewModel
from reuleauxcoder.app.interaction_contracts import (
    ChoiceItem,
    ChooseOneRequest,
    ConfirmRequest,
)

_STRATEGIES = (
    (
        "summarize",
        "Summarize older conversation · recommended",
        "Create a summary of older context and retain recent conversation.",
    ),
    (
        "snip",
        "Trim tool output",
        "Shorten older tool results to reduce their context footprint.",
    ),
    (
        "collapse",
        "Deep compaction…",
        "Use the recovery strategy to rebuild a smaller context; requires confirmation.",
    ),
)


@dataclass(frozen=True, slots=True)
class CompactContextCommand:
    force_strategy: str | None = None


def _parse_compact(user_input, parse_ctx):
    if match_template(user_input, "/compact") is not None:
        return CompactContextCommand()
    captures = match_template(
        user_input, "/compact force {strategy}", case_insensitive=True
    )
    if captures is not None:
        return CompactContextCommand(captures["strategy"].lower())
    return None


def _panel(model, title):
    return PanelDefinition(
        view_type=model.view_type,
        title=f"{title} · estimated {model.estimated_tokens:,} / {model.input_limit:,} tokens",
        items=tuple(
            PanelItem(
                label,
                description,
                ActionRequest("system.compact", CompactContextCommand(strategy)),
            )
            for strategy, label, description in _STRATEGIES
        ),
        show_auxiliary_actions=False,
    )


def _handle_compact(command, ctx):
    context = ctx.agent.context
    strategy = command.force_strategy
    if strategy is None:
        model = CompactViewModel(
            context.predict_request_tokens(ctx.agent.messages),
            context.request_input_limit,
        )
        panel = _panel(model, "Compact context")
        if UICapability.MENUS in ctx.ui_profile.capabilities:
            ctx.effect.open_view(model, title="Compact context", reuse_key="compact")
            return ctx.effect.finish(control="continue")
        response = ctx.ui_interactor.choose_one(
            ChooseOneRequest(
                title=panel.title,
                items=[
                    ChoiceItem(strategy, label, description)
                    for strategy, label, description in _STRATEGIES
                ],
                initial_id="summarize",
            )
        )
        if response.cancelled or response.selected_id is None:
            return ctx.effect.finish(control="continue")
        strategy = response.selected_id
    if strategy not in {item[0] for item in _STRATEGIES}:
        ctx.effect.warning(
            "Invalid compact strategy. Use /compact to choose, or /compact force <snip|summarize|collapse>."
        )
        return ctx.effect.finish(control="continue")
    if strategy == "collapse":
        response = ctx.ui_interactor.confirm(
            ConfirmRequest(
                "Deep compaction",
                "Rebuild the active model context using the recovery strategy? More older detail may be replaced by a summary. This does not delete the session history.",
                severity="warning",
            )
        )
        if response.cancelled or not response.confirmed:
            return ctx.effect.finish(control="continue")
    before = context.predict_request_tokens(ctx.agent.messages)
    changed = ctx.agent.force_compress_context(strategy, ctx.agent.llm)
    after = context.predict_request_tokens(ctx.agent.messages)
    if changed:
        ctx.effect.success(
            f"Context compacted: estimated {before:,} → {after:,} tokens."
        )
    else:
        ctx.effect.info(
            f"No eligible context to compact with {strategy}; estimated {before:,} tokens. Recent conversation was retained."
        )
    return ctx.effect.finish(control="continue")


def command_panel_spec():
    return CommandPanelSpec("compact", CompactViewModel, _panel)


def register_actions(registry):
    registry.register(
        ActionSpec(
            action_id="system.compact",
            command_type=CompactContextCommand,
            feature_id="system",
            description="[session] Compact the current conversation context",
            ui_targets=UI_TARGETS,
            triggers=(
                slash_trigger("/compact"),
                slash_trigger("/compact force <snip|summarize|collapse>"),
            ),
            parser=_parse_compact,
            handler=_handle_compact,
            preview=True,
            interactive=True,
        )
    )
