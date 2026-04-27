import threading
import time
from types import SimpleNamespace

from reuleauxcoder.domain.config.models import Config, ModelProfileConfig
from reuleauxcoder.extensions.subagent.manager import (
    SubagentJob,
    SubagentManager,
    _create_subagent_llm,
)


class _FakeParentLLM:
    def __init__(self) -> None:
        self.model = "parent-model"
        self.debug_trace = True


def test_create_subagent_llm_uses_full_profile_runtime_settings() -> None:
    sub_profile = ModelProfileConfig(
        name="sub-profile",
        model="deepseek-v4-pro",
        api_key="sub-key",
        base_url="https://api.deepseek.com",
        max_tokens=8192,
        temperature=0.0,
        max_context_tokens=128000,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=False,
        reasoning_effort="high",
        thinking_enabled=True,
        reasoning_replay_mode="tool_calls",
        reasoning_replay_placeholder="[PLACE_HOLDER]",
    )
    config = Config(
        model_profiles={"sub-profile": sub_profile},
        active_main_model_profile="sub-profile",
        active_model_profile="sub-profile",
        active_sub_model_profile="sub-profile",
    )
    parent_agent = SimpleNamespace(
        runtime_config=config,
        llm=_FakeParentLLM(),
    )

    llm, profile_name = _create_subagent_llm(parent_agent, None)

    assert profile_name == "sub-profile"
    assert llm.model == "deepseek-v4-pro"
    assert llm.api_key == "sub-key"
    assert llm.base_url == "https://api.deepseek.com"
    assert llm.max_tokens == 8192
    assert llm.temperature == 0.0
    assert llm.preserve_reasoning_content is True
    assert llm.backfill_reasoning_content_for_tool_calls is False
    assert llm.reasoning_effort == "high"
    assert llm.thinking_enabled is True
    assert llm.reasoning_replay_mode == "tool_calls"
    assert llm.reasoning_replay_placeholder == "[PLACE_HOLDER]"
    assert llm.debug_trace is True


# ---------------------------------------------------------------------------
# drain_completed_for_parent  –  parent_state_lock  sync tests
# ---------------------------------------------------------------------------


def test_drain_with_parent_state_lock_skips_concurrently_injected_job() -> None:
    """The locked re-check catches a job injected between fast-path and locked check."""
    manager = SubagentManager()
    now = time.time()
    job = SubagentJob(
        id="sj_race",
        mode="explore",
        task="test task",
        status="completed",
        created_at=now,
        result="done",
        injected_to_parent=False,
    )
    manager._jobs["sj_race"] = job

    parent_lock = threading.Lock()
    # Hold the lock so drain blocks inside the locked section before re-check.
    parent_lock.acquire()

    drained_result: list[list[SubagentJob]] = []

    def _drain() -> None:
        drained_result.append(
            manager.drain_completed_for_parent(parent_state_lock=parent_lock)
        )

    t = threading.Thread(target=_drain)
    t.start()

    # Let the drain thread pass the fast-path check and block on parent_lock.
    time.sleep(0.15)

    # Simulate the done-callback injecting the job concurrently.
    job.injected_to_parent = True

    parent_lock.release()
    t.join(timeout=2)

    assert not t.is_alive(), "drain thread should have finished"
    assert len(drained_result) == 1
    assert drained_result[0] == [], (
        "job should be skipped because it was injected concurrently"
    )


def test_drain_completed_for_parent_works_without_parent_lock() -> None:
    """Backward-compatible: drain still returns completed jobs when no lock is given."""
    manager = SubagentManager()
    now = time.time()
    job = SubagentJob(
        id="sj_no_lock",
        mode="explore",
        task="test task",
        status="completed",
        created_at=now,
        result="done",
        injected_to_parent=False,
    )
    manager._jobs["sj_no_lock"] = job

    result = manager.drain_completed_for_parent()

    assert len(result) == 1
    assert result[0].id == "sj_no_lock"
    assert job.injected_to_parent is True


def test_drain_with_parent_state_lock_drains_job_not_injected() -> None:
    """With parent_state_lock, a job that was *not* injected concurrently is drained."""
    manager = SubagentManager()
    now = time.time()
    job = SubagentJob(
        id="sj_safe",
        mode="explore",
        task="test task",
        status="completed",
        created_at=now,
        result="done",
        injected_to_parent=False,
    )
    manager._jobs["sj_safe"] = job

    parent_lock = threading.Lock()
    result = manager.drain_completed_for_parent(parent_state_lock=parent_lock)

    assert len(result) == 1
    assert result[0].id == "sj_safe"
    assert job.injected_to_parent is True


def test_drain_without_lock_skips_already_injected_job() -> None:
    """Jobs already marked injected_to_parent are skipped even without parent lock."""
    manager = SubagentManager()
    now = time.time()
    job = SubagentJob(
        id="sj_done",
        mode="explore",
        task="test task",
        status="completed",
        created_at=now,
        result="done",
        injected_to_parent=True,
    )
    manager._jobs["sj_done"] = job

    result = manager.drain_completed_for_parent()

    assert result == []
