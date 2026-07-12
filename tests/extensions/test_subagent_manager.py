import threading
import time
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.config.models import Config, ModelProfileConfig
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.extensions.subagent.models import SubagentResult
from reuleauxcoder.extensions.subagent.manager import (
    SubagentJob,
    SubagentManager,
    _create_subagent_llm,
    _filter_subagent_tools,
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


class _Parent:
    def __init__(self) -> None:
        self.agent_id = "parent-agent"
        self.session_generation = 0
        self.current_session_id = "session-1"
        self.injected = []
        self.events = []
        self.messages = []
        self.history_ledger = HistoryLedger(
            session_id=self.current_session_id, agent_id=self.agent_id
        )

    def inject_subagent_job_result(self, job) -> None:
        self.injected.append(job.id)

    def _emit_event(self, event) -> None:
        self.events.append(event)


def test_fast_background_completion_never_loses_job_registration(monkeypatch) -> None:
    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task",
        lambda **kwargs: "done",
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()

    job_id = manager.submit_background(
        parent_agent=parent, task="fast", mode="explore"
    )
    job = manager.wait_job(job_id, timeout=2)

    assert job is not None and job.status == "completed"
    assert job.result == "done"
    assert job.parent_agent_id == "parent-agent"
    assert job.parent_session_id == "session-1"
    assert job.generation == parent.session_generation
    assert parent.injected == []
    assert [event.data["status"] for event in parent.events] == [
        "queued",
        "running",
        "completed",
    ]
    assert [
        event.payload["status"]
        for event in parent.history_ledger.events
        if event.kind == "subagent_job_changed"
    ] == ["queued", "running", "completed"]
    drained = manager.drain_completed_for_parent()
    assert [item.id for item in drained] == [job_id]
    manager.shutdown()


def test_resume_marks_unrecoverable_worker_stale_and_actionable() -> None:
    parent = _Parent()
    parent.history_ledger.append(
        "subagent_job_changed",
        {
            "job_id": "sj_lost",
            "mode": "execute",
            "task": "edit files",
            "status": "running",
            "created_at": 10.0,
            "generation": 0,
            "delivery": "awaited",
        },
        job_id="sj_lost",
    )
    manager = SubagentManager()

    assert manager.restore_from_history(parent, parent.history_ledger.events) == 1
    job = manager.get_job("sj_lost")
    assert job is not None
    assert job.status == "stale"
    assert "not recoverable" in (job.error or "")
    assert manager.drain_completed_for_parent(parent_agent_id=parent.agent_id) == []
    manager.shutdown()


def test_background_exception_becomes_failed_job(monkeypatch) -> None:
    def fail(**kwargs):
        raise RuntimeError("child exploded")

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", fail
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()

    job_id = manager.submit_background(
        parent_agent=parent, task="fail", mode="explore"
    )
    job = manager.wait_job(job_id, timeout=2)

    assert job is not None and job.status == "failed"
    assert job.error == "child exploded"
    assert job.result is None
    drained = manager.drain_completed_for_parent(parent_agent_id=parent.agent_id)
    assert [item.id for item in drained] == [job_id]
    manager.shutdown()


def test_detached_success_does_not_enter_parent_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task",
        lambda **kwargs: "done",
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(
        parent_agent=parent,
        task="optional",
        mode="explore",
        detached=True,
    )
    job = manager.wait_job(job_id, timeout=2)

    assert job is not None and job.status == "completed"
    assert job.delivery == "detached"
    assert manager.drain_completed_for_parent(parent_agent_id=parent.agent_id) == []
    manager.shutdown()


def test_child_messages_route_to_immediate_parent_in_sequence() -> None:
    manager = SubagentManager(parent_agent_id="root")
    manager.register_child_agent(
        "child-a", 1, parent_agent_id="root", job_id="sj_a"
    )
    manager.register_child_agent(
        "child-b", 1, parent_agent_id="root", job_id="sj_b"
    )

    assert manager.send_to_parent("child-b", "second") is True
    assert manager.send_to_parent("child-a", "first") is True
    messages = manager.drain_parent_messages("root")

    assert [item.content for item in messages] == ["second", "first"]
    assert [item.seq for item in messages] == sorted(item.seq for item in messages)
    assert all(len(item.content_hash) == 64 for item in messages)
    assert manager.drain_parent_messages("child-a") == []
    manager.shutdown()


def test_parent_mailbox_recovers_unacknowledged_item_exactly_once() -> None:
    parent = _Parent()
    manager = SubagentManager(parent_agent_id=parent.agent_id)
    manager.bind_root_agent(parent)
    manager.register_child_agent(
        "child-a", 1, parent_agent_id=parent.agent_id, job_id="sj_a"
    )
    assert manager.send_to_parent("child-a", "durable milestone") is True
    queued = manager.drain_parent_messages(parent.agent_id)
    assert len(queued) == 1
    assert manager.drain_parent_messages(parent.agent_id) == []
    assert any(
        event.kind == "subagent_communication_queued"
        for event in parent.history_ledger.events
    )
    item_id = queued[0].item_id
    manager.shutdown()

    recovered = SubagentManager(parent_agent_id=parent.agent_id)
    recovered.restore_from_history(parent, parent.history_ledger.events)
    redelivered = recovered.drain_parent_messages(parent.agent_id)
    assert [item.item_id for item in redelivered] == [item_id]
    assert recovered.acknowledge_parent_message(item_id) is True
    assert any(
        event.kind == "subagent_communication_delivered"
        and event.payload["item_id"] == item_id
        for event in parent.history_ledger.events
    )
    recovered.shutdown()

    final = SubagentManager(parent_agent_id=parent.agent_id)
    final.restore_from_history(parent, parent.history_ledger.events)
    assert final.drain_parent_messages(parent.agent_id) == []
    final.shutdown()


def test_restore_persists_unrecoverable_worker_as_stale() -> None:
    parent = _Parent()
    parent.history_ledger.append(
        "subagent_job_changed",
        {
            "job_id": "sj_lost",
            "mode": "execute",
            "task": "lost work",
            "status": "running",
            "created_at": time.time(),
            "generation": 0,
            "delivery": "awaited",
        },
    )
    manager = SubagentManager(parent_agent_id=parent.agent_id)

    assert manager.restore_from_history(parent, parent.history_ledger.events) == 1

    assert manager.get_job("sj_lost").status == "stale"
    assert parent.history_ledger.events[-1].kind == "subagent_job_changed"
    assert parent.history_ledger.events[-1].payload["status"] == "stale"
    manager.shutdown()


def test_background_execute_waits_for_runtime_managed_verify(monkeypatch) -> None:
    calls = []

    def run(**kwargs):
        calls.append(kwargs["mode"])
        return SubagentResult(
            status="ok",
            summary=f"{kwargs['mode']} complete",
            evidence=[f"{kwargs['mode']} evidence"],
        )

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    parent = _Parent()
    manager = SubagentManager(max_parallel_explore=2)
    execute_id = manager.submit_background(
        parent_agent=parent, task="implement", mode="execute"
    )
    manager.wait_job(execute_id, timeout=2)
    deadline = time.monotonic() + 2
    verify_job = None
    while time.monotonic() < deadline:
        execute = manager.get_job(execute_id)
        verify_job = (
            manager.get_job(execute.verification_job_id)
            if execute and execute.verification_job_id
            else None
        )
        if verify_job and verify_job.status == "completed":
            break
        time.sleep(0.01)

    assert calls == ["execute", "verify"]
    assert verify_job is not None and verify_job.verification_for == execute_id
    drained = manager.drain_completed_for_parent(parent_agent_id=parent.agent_id)
    assert [job.id for job in drained] == [execute_id, verify_job.id]
    manager.shutdown()


def test_failed_automatic_verify_releases_execute_barrier_as_attention(monkeypatch) -> None:
    def run(**kwargs):
        if kwargs["mode"] == "verify":
            return SubagentResult(status="failed", summary="tests failed")
        return SubagentResult(status="ok", summary="implementation complete")

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    parent = _Parent()
    manager = SubagentManager(max_parallel_explore=2)
    execute_id = manager.submit_background(
        parent_agent=parent, task="implement", mode="execute"
    )
    deadline = time.monotonic() + 2
    verify_job = None
    while time.monotonic() < deadline:
        execute = manager.get_job(execute_id)
        verify_job = (
            manager.get_job(execute.verification_job_id)
            if execute and execute.verification_job_id
            else None
        )
        if verify_job and verify_job.status == "failed":
            break
        time.sleep(0.01)

    assert verify_job is not None and verify_job.status == "failed"
    drained = manager.drain_completed_for_parent(parent_agent_id=parent.agent_id)
    assert [job.id for job in drained] == [execute_id, verify_job.id]
    assert manager.has_awaited_jobs(parent.agent_id) is False
    manager.shutdown()


def test_sync_execute_returns_combined_automatic_verification(monkeypatch) -> None:
    calls = []

    def run(**kwargs):
        calls.append(kwargs["mode"])
        return SubagentResult(
            status="ok",
            summary=f"{kwargs['mode']} complete",
            evidence=[f"{kwargs['mode']} evidence"],
        )

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager()

    result = manager.run_sync(
        parent_agent=_Parent(), task="implement", mode="execute"
    )

    assert calls == ["execute", "verify"]
    assert isinstance(result, SubagentResult)
    assert "Automatic verification: verify complete" in result.summary
    assert result.evidence == ["execute evidence", "verify evidence"]
    manager.shutdown()


def test_manager_rejects_cross_parent_reuse(monkeypatch) -> None:
    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task",
        lambda **kwargs: "done",
    )
    manager = SubagentManager(max_parallel_explore=1)
    first = _Parent()
    manager.submit_background(parent_agent=first, task="first", mode="explore")
    second = _Parent()
    second.agent_id = "other-parent"

    try:
        with pytest.raises(ValueError, match="cannot be shared"):
            manager.submit_background(
                parent_agent=second, task="second", mode="explore"
            )
    finally:
        manager.shutdown()


def test_generation_change_prevents_old_result_injection(monkeypatch) -> None:
    release = threading.Event()

    def run(**kwargs):
        release.wait(timeout=2)
        return "old result"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(
        parent_agent=parent, task="old", mode="explore"
    )

    manager.advance_generation(cancel_pending=False)
    release.set()
    job = manager.wait_job(job_id, timeout=2)

    assert job is not None and job.status == "stale"
    assert parent.injected == []
    assert manager.drain_completed_for_parent() == []
    manager.shutdown()


def test_generation_change_rejects_late_child_messages() -> None:
    manager = SubagentManager(parent_agent_id="root")
    manager.register_child_agent(
        "old-child", 1, parent_agent_id="root", job_id="sj_old"
    )

    manager.advance_generation(cancel_pending=False)

    assert manager.send_to_parent("old-child", "late result") is False
    assert manager.drain_parent_messages("root") == []
    manager.shutdown()


def test_cancel_job_has_explicit_terminal_state(monkeypatch) -> None:
    def run(**kwargs):
        cancel_event = kwargs["cancel_event"]
        cancel_event.wait(timeout=2)
        return "[Sub-agent finished status=cancelled]"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(
        parent_agent=parent, task="cancel", mode="explore"
    )

    assert manager.cancel_job(job_id) is True
    job = manager.wait_job(job_id, timeout=2)

    assert job is not None and job.status == "cancelled"
    assert job.cancel_requested is True
    assert parent.injected == []
    manager.shutdown()


def test_cancelling_queued_future_does_not_deadlock_callback(monkeypatch) -> None:
    release = threading.Event()

    def run(**kwargs):
        release.wait(timeout=2)
        return "done"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    first = manager.submit_background(
        parent_agent=parent, task="blocking", mode="explore"
    )
    second = manager.submit_background(
        parent_agent=parent, task="queued", mode="explore"
    )

    assert manager.cancel_job(second) is True
    second_job = manager.wait_job(second, timeout=1)
    assert second_job is not None and second_job.status == "cancelled"

    release.set()
    manager.wait_job(first, timeout=2)
    manager.shutdown()


def test_shutdown_rejects_new_jobs() -> None:
    manager = SubagentManager()
    manager.shutdown()

    try:
        manager.submit_background(parent_agent=_Parent(), task="late", mode="explore")
    except RuntimeError as error:
        assert "shut down" in str(error)
    else:
        raise AssertionError("submission after shutdown must fail")


def test_shutdown_cancels_running_job_and_prune_releases_terminal_state(
    monkeypatch,
) -> None:
    def run(**kwargs):
        kwargs["cancel_event"].wait(timeout=2)
        return "[Sub-agent finished status=cancelled]"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager(max_parallel_explore=1)
    job_id = manager.submit_background(
        parent_agent=_Parent(), task="running", mode="explore"
    )

    manager.shutdown(wait=True)

    job = manager.get_job(job_id)
    assert job is not None and job.status == "cancelled"
    assert manager.prune(keep=0) == 1
    assert manager.get_job(job_id) is None


def test_subagent_tools_are_distinct_instances() -> None:
    from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool

    tool = ReadFileTool()
    parent = SimpleNamespace(tools=[tool])

    (child_tool,) = _filter_subagent_tools(parent, "explore")

    assert child_tool is not tool
    assert child_tool.backend is not tool.backend


def test_subagent_tool_backend_context_is_isolated() -> None:
    from reuleauxcoder.extensions.tools.backend import (
        ExecutionContext,
        LocalToolBackend,
    )
    from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool

    context = ExecutionContext(cwd="/tmp", workspace_root="/tmp")
    backend = LocalToolBackend(context)
    tool = ReadFileTool(backend)
    parent = SimpleNamespace(tools=[tool])

    (child_tool,) = _filter_subagent_tools(parent, "explore")
    child_tool.backend.context.cwd = "/child"

    assert child_tool.backend is not backend
    assert child_tool.backend.context is not context
    assert context.cwd == "/tmp"
    assert child_tool.backend.workspace is not backend.workspace
    assert child_tool.backend.process is not backend.process


def test_subagent_shell_cwd_state_is_not_shared() -> None:
    from reuleauxcoder.extensions.tools.backend import (
        ExecutionContext,
        LocalToolBackend,
    )
    from reuleauxcoder.extensions.tools.builtin.shell import ShellTool

    parent_tool = ShellTool(
        LocalToolBackend(ExecutionContext(cwd="/tmp", workspace_root="/tmp"))
    )
    parent_tool._cwd = "/tmp/parent"
    parent = SimpleNamespace(tools=[parent_tool])

    (child_tool,) = _filter_subagent_tools(parent, "execute")
    child_tool._cwd = "/tmp/child"

    assert parent_tool._cwd == "/tmp/parent"
    assert child_tool.backend.context is not parent_tool.backend.context


def test_manager_honors_custom_max_timeout(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def run(**kwargs):
        captured["timeout"] = kwargs["timeout_seconds"]
        return "done"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager(max_timeout_seconds=7)

    manager.run_sync(
        parent_agent=_Parent(),
        task="bounded",
        mode="execute",
        timeout_seconds=999,
    )

    assert captured["timeout"] == 7
    manager.shutdown()
