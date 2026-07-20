"""Round-trip coverage for session runtime state model restore."""

from types import SimpleNamespace

from reuleauxcoder.app.runtime.session_state import (
    apply_session_runtime_state,
    build_session_runtime_state,
)


class _FakeLLM:
    def __init__(self, model: str = "base-model") -> None:
        self.model = model
        self.debug_trace = False
        self.reconfigured_with = None

    def reconfigure(self, **kwargs) -> None:
        self.reconfigured_with = kwargs
        self.model = kwargs.get("model", self.model)


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
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        model="base-model",
        model_profiles={"sonnet": _profile("sonnet")},
        active_main_model_profile=None,
        active_model_profile=None,
        active_sub_model_profile=None,
        active_mode=None,
        llm_debug_trace=False,
        approval=SimpleNamespace(default_mode="warn", rules=[]),
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
        context=SimpleNamespace(reconfigure=lambda limit: None),
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
