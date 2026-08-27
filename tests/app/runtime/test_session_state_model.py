"""Round-trip coverage for session runtime state model restore."""

from types import SimpleNamespace

from reuleauxcoder.app.runtime.session_state import (
    apply_session_runtime_state,
    build_session_runtime_state,
)
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    ContextConfig,
    ContextStrategyOverrides,
)


class _FakeLLM:
    def __init__(self, model: str = "base-model") -> None:
        self.model = model
        self.debug_trace = False
        self.reconfigured_with = None

    def reconfigure(self, **kwargs) -> None:
        self.reconfigured_with = kwargs
        self.model = kwargs.get("model", self.model)


class _FakeContext:
    def __init__(self) -> None:
        self.max_tokens = 0
        self.strategy_settings = None

    def reconfigure(self, limit: int, **settings) -> None:
        self.max_tokens = limit
        self.strategy_settings = settings


def _profile(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        model=f"model-{name}",
        api_key="sk-test",
        base_url=None,
        temperature=0.5,
        max_tokens=1024,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=False,
        reasoning_effort=None,
        reasoning_effort_values=None,
        reasoning_effort_param="reasoning_effort",
        thinking_enabled=None,
        reasoning_replay_mode=None,
        reasoning_replay_placeholder=None,
        max_context_tokens=100_000,
        context=ContextStrategyOverrides(auto_snip=False),
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        model="base-model",
        skills=SimpleNamespace(disabled=[]),
        model_profiles={"sonnet": _profile("sonnet")},
        active_main_model_profile=None,
        active_model_profile=None,
        active_sub_model_profile=None,
        active_mode=None,
        llm_debug_trace=False,
        approval=SimpleNamespace(default_mode="warn", rules=[]),
        context=ContextConfig(auto_snip=True, auto_summarize=False),
        max_context_tokens=128_000,
    )


def _agent(model: str = "base-model") -> SimpleNamespace:
    plan_state = SimpleNamespace(to_dict=lambda: {})
    progress_state = SimpleNamespace(to_dict=lambda: {})
    hook_registry = SimpleNamespace(hooks_at=lambda point: ())
    return SimpleNamespace(
        llm=_FakeLLM(model),
        state=SimpleNamespace(
            messages=[], total_prompt_tokens=0, total_completion_tokens=0
        ),
        active_main_model_profile=None,
        active_sub_model_profile=None,
        active_mode=None,
        available_modes={},
        session_approval_rules=[],
        hook_registry=hook_registry,
        context=_FakeContext(),
        plan_controller=SimpleNamespace(
            state=plan_state,
            progress=progress_state,
            restore=lambda plan, progress: None,
        ),
        session_fingerprint=None,
    )


def test_runtime_state_round_trip_restores_switched_profile() -> None:
    config = _config()

    source = _agent(model="model-sonnet")
    source.active_main_model_profile = "sonnet"
    state = build_session_runtime_state(config, source)
    assert state.active_main_model_profile == "sonnet"

    restored = _agent()
    session = SimpleNamespace(
        runtime_state=state,
        messages=[],
        total_prompt_tokens=0,
        total_completion_tokens=0,
        active_mode=None,
        checkpoints=(),
    )
    apply_session_runtime_state(session, config, restored)

    assert restored.llm.model == "model-sonnet"
    assert restored.llm.reconfigured_with is not None
    assert restored.llm.reconfigured_with["api_key"] == "sk-test"
    assert restored.active_main_model_profile == "sonnet"
    assert restored.context.max_tokens == 100_000
    assert restored.context.strategy_settings == {
        "auto_snip": False,
        "auto_summarize": False,
        "auto_collapse": True,
    }


def test_runtime_state_round_trip_restores_skills_disabled() -> None:
    config = _config()

    class _FakeSkillsService:
        def __init__(self) -> None:
            self.disabled_names = ()
            self.restored_with = None

        def restore_disabled_names(self, names) -> bool:
            self.restored_with = list(names)
            self.disabled_names = tuple(sorted(names))
            return True

        def build_catalog(self) -> str:
            return "catalog-without-disabled"

    source = _agent()
    service = _FakeSkillsService()
    service.disabled_names = ("deep-review",)
    source.skills_service = service

    state = build_session_runtime_state(config, source)
    assert state.skills_disabled == ["deep-review"]

    config.skills.disabled = ["other-skill"]
    restored = _agent()
    restored_service = _FakeSkillsService()
    restored.skills_service = restored_service
    restored.skills_catalog = "stale-catalog"
    session = SimpleNamespace(
        runtime_state=state,
        messages=[],
        total_prompt_tokens=0,
        total_completion_tokens=0,
        active_mode=None,
        checkpoints=(),
    )
    apply_session_runtime_state(session, config, restored)

    assert config.skills.disabled == ["deep-review"]
    assert restored_service.restored_with == ["deep-review"]
    assert restored_service.disabled_names == ("deep-review",)
    assert restored.skills_catalog == "catalog-without-disabled"


def test_runtime_state_round_trip_preserves_approval_pattern() -> None:
    config = _config()
    config.approval = ApprovalConfig(default_mode="require_approval")
    source = _agent()
    source.session_approval_rules = [
        ApprovalRuleConfig(
            tool_name="edit_file",
            pattern="src/app.py",
            scope_key="session-workspace",
            action="allow",
        )
    ]

    state = build_session_runtime_state(config, source)
    restored = _agent()
    session = SimpleNamespace(
        runtime_state=state,
        messages=[],
        total_prompt_tokens=0,
        total_completion_tokens=0,
        active_mode=None,
        checkpoints=(),
    )

    apply_session_runtime_state(session, config, restored)

    assert len(restored.session_approval_rules) == 1
    assert restored.session_approval_rules[0].tool_name == "edit_file"
    assert restored.session_approval_rules[0].pattern == "src/app.py"
    assert restored.session_approval_rules[0].scope_key == "session-workspace"
