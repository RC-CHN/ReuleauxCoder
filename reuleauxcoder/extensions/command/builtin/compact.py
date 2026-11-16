"""Manual prefix compaction through the same runtime policy as automatic work."""

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.shared import UI_TARGETS, slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.domain.context.summary import CompactionError


@dataclass(frozen=True, slots=True)
class CompactContextCommand:
    pass


def _parse_compact(user_input, parse_ctx):
    if match_template(user_input, "/compact") is not None:
        return CompactContextCommand()
    return None


def _handle_compact(command, ctx):
    context = ctx.agent.context
    before = context.predict_request_tokens(ctx.agent.messages)
    try:
        changed = ctx.agent.force_compress_context("summarize", ctx.agent.llm)
    except CompactionError as error:
        ctx.effect.warning(str(error))
        return ctx.effect.finish(control="continue")
    if changed:
        checkpoint = context.checkpoints[-1]
        ctx.effect.success(
            f"Context compacted: estimated {checkpoint.tokens_before:,} → {checkpoint.tokens_after:,} tokens."
        )
    else:
        ctx.effect.info(
            f"No complete prefix is available to compact; estimated {before:,} tokens. Recent conversation was retained."
        )
    return ctx.effect.finish(control="continue")


def register_actions(registry):
    registry.register(
        ActionSpec(
            action_id="system.compact",
            command_type=CompactContextCommand,
            feature_id="system",
            description="[session] Summarize older context and retain recent history",
            ui_targets=UI_TARGETS,
            triggers=(slash_trigger("/compact"),),
            parser=_parse_compact,
            handler=_handle_compact,
        )
    )
