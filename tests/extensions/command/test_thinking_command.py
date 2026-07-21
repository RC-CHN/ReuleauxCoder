"""Tests for the /thinking command."""

from __future__ import annotations

from types import SimpleNamespace
from reuleauxcoder.app.commands.models import CommandEffect

from reuleauxcoder.app.commands.shared import EmptyCommand
from reuleauxcoder.extensions.command.builtin.thinking import (
    SetEffortCommand,
    ToggleInlineCommand,
    _handle_effort_set,
    _handle_effort_show,
    _handle_inline,
    _handle_show,
    _parse_effort_set,
    _parse_effort_show,
    _parse_inline,
    _parse_show,
)


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


class FakeLLM:
    def __init__(self):
        self.reasoning_effort: str | None = "medium"
        self.reasoning_effort_values: dict | None = None
        self.reasoning_effort_param: str = "reasoning_effort"
        self._reconfig_calls: list[dict] = []

    def reconfigure(self, **kwargs):
        self._reconfig_calls.append(dict(kwargs))
        for k, v in kwargs.items():
            if v is not None:
                setattr(self, k, v)


# ---------------------------------------------------------------------------
# Fake Agent
# ---------------------------------------------------------------------------


class FakeAgent:
    def __init__(self):
        self.last_reasoning_content: str | None = None
        self.reasoning_display_mode: str = "quiet"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_ctx(
    *,
    agent: FakeAgent | None = None,
    llm: FakeLLM | None = None,
) -> SimpleNamespace:
    ag = agent or FakeAgent()
    llm_runtime = llm or FakeLLM()
    ag.llm = llm_runtime
    from reuleauxcoder.domain.config.models import (
        ApprovalConfig,
        Config,
        ModelProfileConfig,
    )

    config = Config(
        api_key="key",
        approval=ApprovalConfig(),
        active_main_model_profile="gpt5",
        model_profiles={
            "gpt5": ModelProfileConfig(
                name="gpt5",
                model="gpt-5",
                api_key="key",
                reasoning_effort="high",
                reasoning_effort_values={
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                },
            ),
        },
    )
    effect = CommandEffect()
    return SimpleNamespace(config=config, agent=ag, effect=effect)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestParseThinking:
    def test_show(self):
        assert isinstance(_parse_show("/thinking", None), EmptyCommand)

    def test_inline(self):
        result = _parse_inline("/thinking inline", None)
        assert isinstance(result, ToggleInlineCommand)

    def test_effort_show(self):
        assert isinstance(_parse_effort_show("/thinking effort", None), EmptyCommand)

    def test_effort_set_valid(self):
        result = _parse_effort_set("/thinking effort high", None)
        assert isinstance(result, SetEffortCommand)
        assert result.level == "high"

    def test_effort_set_valid_case_insensitive(self):
        result = _parse_effort_set("/thinking effort HIGH", None)
        assert isinstance(result, SetEffortCommand)
        assert result.level == "high"

    def test_effort_set_invalid_value(self):
        assert _parse_effort_set("/thinking effort max", None) is None

    def test_effort_set_no_value(self):
        assert _parse_effort_set("/thinking effort", None) is None


# ---------------------------------------------------------------------------
# Handler tests — /thinking show
# ---------------------------------------------------------------------------


class TestHandleShow:
    def test_no_reasoning_content(self):
        agent = FakeAgent()
        agent.last_reasoning_content = None
        ctx = _build_ctx(agent=agent)
        result = _handle_show(None, ctx)
        assert result.control == "continue"
        assert any(
            e.level == "info" and "No reasoning content" in e.message
            for e in ctx.effect.notifications
        )

    def test_has_reasoning_content(self):
        agent = FakeAgent()
        agent.last_reasoning_content = "Let me think about this..."
        ctx = _build_ctx(agent=agent)
        result = _handle_show(None, ctx)
        assert result.control == "continue"
        assert any(
            e.level == "info"
            and e.payload is not None
            and e.message == "Let me think about this..."
            for e in ctx.effect.notifications
        )


# ---------------------------------------------------------------------------
# Handler tests — /thinking inline
# ---------------------------------------------------------------------------


class TestHandleInline:
    def test_toggle_quiet_to_inline(self):
        agent = FakeAgent()
        agent.reasoning_display_mode = "quiet"
        ctx = _build_ctx(agent=agent)
        _handle_inline(ToggleInlineCommand(), ctx)
        assert agent.reasoning_display_mode == "inline"
        assert any(
            e.level == "info" and "inline" in e.message
            for e in ctx.effect.notifications
        )

    def test_toggle_inline_to_quiet(self):
        agent = FakeAgent()
        agent.reasoning_display_mode = "inline"
        ctx = _build_ctx(agent=agent)
        _handle_inline(ToggleInlineCommand(), ctx)
        assert agent.reasoning_display_mode == "quiet"
        assert any(
            e.level == "info" and "quiet" in e.message for e in ctx.effect.notifications
        )


# ---------------------------------------------------------------------------
# Handler tests — /thinking effort show
# ---------------------------------------------------------------------------


class TestHandleEffortShow:
    def test_default(self):
        llm = FakeLLM()
        llm.reasoning_effort = "medium"
        ctx = _build_ctx(llm=llm)
        result = _handle_effort_show(None, ctx)
        assert result.control == "continue"
        assert len(ctx.effect.views) == 1
        view = ctx.effect.views[0]
        assert view.view_type == "thinking_effort"
        assert view.action == "open"
        model = view.view_model
        assert model.current == "medium"
        assert model.profile_default == "high"
        assert [level.label for level in model.levels] == ["low", "medium", "high"]

    def test_custom_mapping(self):
        llm = FakeLLM()
        llm.reasoning_effort = "high"
        llm.reasoning_effort_values = {"low": "high", "medium": "high", "high": "max"}
        llm.reasoning_effort_param = "thinking_level"
        ctx = _build_ctx(llm=llm)
        result = _handle_effort_show(None, ctx)
        assert result.control == "continue"
        assert len(ctx.effect.views) == 1
        model = ctx.effect.views[0].view_model
        assert model.param == "thinking_level"
        api_values = {level.label: level.api_value for level in model.levels}
        assert api_values == {"low": "high", "medium": "high", "high": "max"}


# ---------------------------------------------------------------------------
# Handler tests — /thinking effort set
# ---------------------------------------------------------------------------


class TestHandleEffortSet:
    def test_set_valid(self):
        llm = FakeLLM()
        llm.reasoning_effort = "medium"
        ctx = _build_ctx(llm=llm)
        _handle_effort_set(SetEffortCommand(level="high"), ctx)
        assert llm.reasoning_effort == "high"
        assert any(
            e.level == "success" and "high" in e.message and "medium" in e.message
            for e in ctx.effect.notifications
        )

    def test_set_not_in_mapping(self):
        llm = FakeLLM()
        llm.reasoning_effort = "medium"
        llm.reasoning_effort_values = {"high": "max"}
        ctx = _build_ctx(llm=llm)
        _handle_effort_set(SetEffortCommand(level="low"), ctx)
        assert llm.reasoning_effort == "medium"  # unchanged
        assert any(
            e.level == "error" and "not available" in e.message
            for e in ctx.effect.notifications
        )

    def test_set_with_custom_mapping(self):
        llm = FakeLLM()
        llm.reasoning_effort = "low"
        llm.reasoning_effort_values = {"low": 1, "medium": 5, "high": 10}
        llm.reasoning_effort_param = "think"
        ctx = _build_ctx(llm=llm)
        _handle_effort_set(SetEffortCommand(level="high"), ctx)
        assert llm.reasoning_effort == "high"
        assert any(
            e.level == "success" and "10" in e.message and "think" in e.message
            for e in ctx.effect.notifications
        )
