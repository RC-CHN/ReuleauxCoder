import threading
import time
from types import SimpleNamespace
from collections import deque

import pytest

from reuleauxcoder.domain.config.models import Config, ModelProfileConfig
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.extensions.subagent.models import SubagentResult
from reuleauxcoder.extensions.subagent.manager import (
    SubagentCapacityError,
    SubagentJob,
    SubagentManager,
    _filter_subagent_tools,
    _subagent_llm_kwargs,
)


class _FakeParentLLM:
    def __init__(self) -> None:
        self.model = "parent-model"
        self.debug_trace = True


def test_subagent_worker_spec_uses_full_profile_runtime_settings() -> None:
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

    settings = _subagent_llm_kwargs(parent_agent, None)

    assert settings["model"] == "deepseek-v4-pro"
    assert settings["api_key"] == "sub-key"
    assert settings["base_url"] == "https://api.deepseek.com"
    assert settings["max_tokens"] == 8192
    assert settings["temperature"] == 0.0
    assert settings["preserve_reasoning_content"] is True
    assert settings["backfill_reasoning_content_for_tool_calls"] is False
    assert settings["reasoning_effort"] == "high"
    assert settings["thinking_enabled"] is True
    assert settings["reasoning_replay_mode"] == "tool_calls"
    assert settings["reasoning_replay_placeholder"] == "[PLACE_HOLDER]"
    assert settings["debug_trace"] is True


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

    job_id = manager.submit_background(parent_agent=parent, task="fast", mode="explore")
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

    job_id = manager.submit_background(parent_agent=parent, task="fail", mode="explore")
    job = manager.wait_job(job_id, timeout=2)

    assert job is not None and job.status == "failed"
    assert job.error == "child exploded"
    assert job.result is None
    drained = manager.drain_completed_for_parent(parent_agent_id=parent.agent_id)
    assert [item.id for item in drained] == [job_id]
    manager.shutdown()


def test_unknown_non_ok_worker_status_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task",
        lambda **_kwargs: SubagentResult(
            status="budget_exhausted",
            summary="token budget exhausted",
        ),
    )
    parent = _Parent()
    manager = SubagentManager()

    job_id = manager.submit_background(
        parent_agent=parent,
        task="bounded task",
        mode="explore",
        auto_verify=False,
    )
    terminal = manager.wait_job(job_id, timeout=2)

    assert terminal is not None and terminal.status == "failed"
    assert terminal.error == "token budget exhausted"
    manager.shutdown()


def test_child_messages_route_to_immediate_parent_in_sequence() -> None:
    manager = SubagentManager(parent_agent_id="root")
    manager.register_child_agent("child-a", 1, parent_agent_id="root", job_id="sj_a")
    manager.register_child_agent("child-b", 1, parent_agent_id="root", job_id="sj_b")

    assert manager.send_to_parent("child-b", "second") is True
    assert (
        manager.send_to_parent("child-a", "first", kind="reply", reply_to="sd_request")
        is True
    )
    messages = manager.drain_parent_messages("root")

    assert [item.content for item in messages] == ["second", "first"]
    assert [item.seq for item in messages] == sorted(item.seq for item in messages)
    assert all(len(item.content_hash) == 64 for item in messages)
    assert messages[1].reply_to == "sd_request"
    assert manager.drain_parent_messages("child-a") == []
    manager.shutdown()


def test_human_guidance_precedes_unconsumed_parent_guidance() -> None:
    parent = _Parent()
    manager = SubagentManager(parent_agent_id=parent.agent_id)
    manager.bind_root_agent(parent)
    job = SubagentJob(
        id="sj_guidance_order",
        mode="explore",
        task="choose API",
        status="parking",
        created_at=time.time(),
        parent_agent_id=parent.agent_id,
        generation=manager.generation,
    )
    manager._jobs[job.id] = job
    manager._message_queues[job.id] = deque()

    parent_directive = manager.queue_message(
        job.id,
        "parent preference",
        source="parent",
    )
    human_directive = manager.queue_message(
        job.id,
        "human decision",
        source="human",
    )
    drained = manager.drain_messages(job.id)

    assert parent_directive is not None and human_directive is not None
    assert [item.content for item in drained] == [
        "human decision",
        "parent preference",
    ]
    assert [item.source for item in drained] == ["human", "parent"]
    manager.shutdown()


def test_wait_subscription_cannot_lose_new_mailbox_activity() -> None:
    parent = _Parent()
    manager = SubagentManager(parent_agent_id=parent.agent_id)
    manager.bind_root_agent(parent)
    manager.register_child_agent(
        "child-wait",
        1,
        parent_agent_id=parent.agent_id,
        job_id="sj_wait",
    )
    observed = []

    waiter = threading.Thread(
        target=lambda: observed.append(
            manager.wait_for_parent_activity(parent.agent_id, timeout=1)
        )
    )
    waiter.start()
    time.sleep(0.02)
    assert manager.send_to_parent("child-wait", "new activity")
    waiter.join(timeout=1)

    assert observed == [True]
    manager.shutdown()


def test_multiple_completions_drain_in_stable_activity_sequence(monkeypatch) -> None:
    gates = {"first": threading.Event(), "second": threading.Event()}

    def run(**kwargs):
        gates[kwargs["task"]].wait(timeout=2)
        return kwargs["task"]

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    parent = _Parent()
    manager = SubagentManager(max_parallel_explore=2)
    first = manager.submit_background(
        parent_agent=parent, task="first", mode="explore", auto_verify=False
    )
    second = manager.submit_background(
        parent_agent=parent, task="second", mode="explore", auto_verify=False
    )
    gates["second"].set()
    manager.wait_job(second, timeout=1)
    gates["first"].set()
    manager.wait_job(first, timeout=1)

    drained = manager.drain_completed_for_parent(parent_agent_id=parent.agent_id)

    assert [job.id for job in drained] == [second, first]
    assert [job.completion_seq for job in drained] == sorted(
        job.completion_seq for job in drained
    )
    manager.shutdown()


def test_global_active_job_cap_rejects_fifth_until_a_terminal_slot_opens(
    monkeypatch,
) -> None:
    gates = {f"job-{index}": threading.Event() for index in range(5)}

    def run(**kwargs):
        gates[kwargs["task"]].wait(timeout=3)
        return kwargs["task"]

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    parent = _Parent()
    manager = SubagentManager(max_parallel_explore=4)
    try:
        jobs = [
            manager.submit_background(
                parent_agent=parent,
                task=f"job-{index}",
                mode="explore",
                auto_verify=False,
            )
            for index in range(4)
        ]

        assert manager.active_job_count == 4
        assert manager.max_active_jobs == 4
        with pytest.raises(SubagentCapacityError, match=r"full \(4/4 active\)"):
            manager.submit_background(
                parent_agent=parent,
                task="job-4",
                mode="verify",
                auto_verify=False,
            )
        assert len(manager.list_jobs()) == 4

        gates["job-0"].set()
        assert manager.wait_job(jobs[0], timeout=2).status == "completed"
        replacement = manager.submit_background(
            parent_agent=parent,
            task="job-4",
            mode="execute",
            auto_verify=False,
        )
        assert isinstance(replacement, str)
        assert manager.active_job_count == 4
    finally:
        for gate in gates.values():
            gate.set()
        manager.shutdown()


def test_global_cap_counts_blocked_and_prior_generation_jobs() -> None:
    manager = SubagentManager(parent_agent_id="root", initial_generation=2)
    now = time.time()
    manager._jobs = {
        status: SubagentJob(
            id=f"sj_{status}",
            mode="explore",
            task=status,
            status=status,
            created_at=now,
            generation=1 if status == "cancelling" else 2,
        )
        for status in ("blocked", "queued", "cancelling", "completed")
    }

    assert manager.active_job_count == 3
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


def test_restore_cancels_oldest_blocked_job_above_global_cap(tmp_path) -> None:
    parent = _Parent()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"messages": []}', encoding="utf-8")
    for index in range(5):
        parent.history_ledger.append(
            "subagent_job_changed",
            {
                "job_id": f"sj_blocked_{index}",
                "mode": "explore",
                "task": f"blocked {index}",
                "status": "blocked",
                "created_at": float(index + 1),
                "generation": 0,
                "depth": 1,
                "resume_reference": str(checkpoint),
            },
        )
    manager = SubagentManager(parent_agent_id=parent.agent_id)

    assert manager.restore_from_history(parent, parent.history_ledger.events) == 5
    assert manager.active_job_count == 4
    assert manager.get_job("sj_blocked_0").status == "cancelled"
    assert "four-agent limit" in manager.get_job("sj_blocked_0").error
    assert all(
        manager.get_job(f"sj_blocked_{index}").status == "blocked"
        for index in range(1, 5)
    )
    manager.shutdown()


def test_restore_replays_queued_but_unconsumed_directive(tmp_path) -> None:
    parent = _Parent()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"messages": []}', encoding="utf-8")
    parent.history_ledger.append(
        "subagent_job_changed",
        {
            "job_id": "sj_directive_restore",
            "mode": "explore",
            "task": "wait",
            "status": "blocked",
            "created_at": time.time(),
            "generation": 0,
            "depth": 1,
            "resume_reference": str(checkpoint),
        },
    )
    parent.history_ledger.append(
        "subagent_communication_queued",
        {
            "item_id": "sd_durable",
            "direction": "parent_to_child",
            "target_job_id": "sj_directive_restore",
            "sender_agent_id": parent.agent_id,
            "content": "durable human choice",
            "generation": 0,
            "seq": 9,
            "source": "human",
            "content_hash": "hash",
        },
    )
    manager = SubagentManager(parent_agent_id=parent.agent_id)

    assert manager.restore_from_history(parent, parent.history_ledger.events) == 1
    directives = manager.drain_messages("sj_directive_restore")

    assert len(directives) == 1
    assert directives[0].directive_id == "sd_durable"
    assert directives[0].source == "human"
    assert directives[0].content == "durable human choice"
    manager.shutdown()


def test_restored_blocked_job_resumes_same_identity_after_guidance(
    monkeypatch, tmp_path
) -> None:
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return SubagentResult(status="ok", summary="resumed")

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    parent = _Parent()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"messages": []}', encoding="utf-8")
    parent.history_ledger.append(
        "subagent_job_changed",
        {
            "job_id": "sj_blocked",
            "mode": "explore",
            "task": "await choice",
            "status": "blocked",
            "created_at": time.time(),
            "generation": 0,
            "depth": 1,
            "resume_reference": str(checkpoint),
            "guidance_request_id": "sc_guidance",
            "max_rounds": 20,
            "max_tool_calls": 10,
        },
    )
    manager = SubagentManager(parent_agent_id=parent.agent_id)
    assert manager.restore_from_history(parent, parent.history_ledger.events) == 1
    assert manager.get_job("sj_blocked").status == "blocked"

    assert manager.send_message("sj_blocked", "Choose the compatible API")
    terminal = manager.wait_job("sj_blocked", timeout=2)

    assert terminal is not None and terminal.id == "sj_blocked"
    assert terminal.status == "completed"
    assert len(calls) == 1
    assert calls[0]["resume_reference"] == str(checkpoint)
    assert "Choose the compatible API" in calls[0]["resume_directives"][0]
    assert "parent-owned LSP document generations" in calls[0]["resume_directives"][-1]
    manager.shutdown()


def test_blocked_job_uses_independent_guidance_deadline(monkeypatch, tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"messages": []}', encoding="utf-8")

    def parked(**_kwargs):
        return SubagentResult(
            status="blocked",
            summary="waiting for a decision",
            transcript_ref=str(checkpoint),
        )

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", parked
    )
    parent = _Parent()
    manager = SubagentManager(guidance_timeout_seconds=0.05)
    job_id = manager.submit_background(
        parent_agent=parent,
        task="wait for guidance",
        mode="explore",
        auto_verify=False,
    )

    terminal = manager.wait_job(job_id, timeout=1)

    assert terminal is not None
    assert terminal.status == "timed_out"
    assert terminal.error == "Sub-agent guidance deadline expired."
    assert terminal.guidance_deadline_at is None
    assert terminal.cancellation_id == f"guidance_timeout_{job_id}_1"
    manager.shutdown()


def test_guidance_resume_cancels_old_deadline(monkeypatch, tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"messages": []}', encoding="utf-8")
    calls = 0

    def run(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SubagentResult(
                status="blocked",
                summary="waiting",
                transcript_ref=str(checkpoint),
            )
        return SubagentResult(status="ok", summary="resumed")

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    parent = _Parent()
    manager = SubagentManager(guidance_timeout_seconds=0.2)
    job_id = manager.submit_background(
        parent_agent=parent,
        task="wait then resume",
        mode="explore",
        auto_verify=False,
    )
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if manager.get_job(job_id).status == "blocked":
            break
        time.sleep(0.005)

    assert manager.send_message(job_id, "continue")
    terminal = manager.wait_job(job_id, timeout=1)
    time.sleep(0.25)

    assert terminal is not None and terminal.status == "completed"
    assert manager.get_job(job_id).status == "completed"
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


def test_failed_automatic_verify_releases_execute_barrier_as_attention(
    monkeypatch,
) -> None:
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
    job_id = manager.submit_background(parent_agent=parent, task="old", mode="explore")

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


def test_cancel_advances_epoch_before_worker_terminalizes(monkeypatch) -> None:
    release = threading.Event()

    def run(**_kwargs):
        release.wait(timeout=2)
        return "done"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(
        parent_agent=parent, task="wait", mode="explore", auto_verify=False
    )
    for _ in range(100):
        if manager.get_job(job_id).status == "running":
            break
        time.sleep(0.01)
    before = manager.get_job(job_id).cancellation_epoch
    assert manager.cancel_job(job_id) is True
    assert manager.get_job(job_id).cancellation_epoch == before + 1
    release.set()
    terminal = manager.wait_job(job_id, timeout=2)
    assert terminal.status == "cancelled"
    assert terminal.result is None
    assert "quarantined" in terminal.error
    manager.shutdown()


def test_child_progress_and_tool_activity_update_job_snapshot(monkeypatch) -> None:
    release = threading.Event()

    def run(**_kwargs):
        release.wait(timeout=2)
        return "done"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", run
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(
        parent_agent=parent, task="inspect", mode="explore", auto_verify=False
    )
    for _ in range(100):
        if manager.get_job(job_id).status == "running":
            break
        time.sleep(0.01)
    manager.register_child_agent(
        "sa-test", 1, parent_agent_id=parent.agent_id, job_id=job_id
    )

    assert manager.record_progress(
        "sa-test",
        phase="investigating",
        summary="reading parser",
        next_step="run tests",
    )
    assert manager.record_tool_activity("sa-test", "grep")
    job = manager.get_job(job_id)
    assert job.progress == ("investigating", "reading parser", "run tests")
    assert job.current_tool == "grep"
    assert job.tool_calls == 1
    assert job.last_activity_at is not None

    release.set()
    manager.wait_job(job_id, timeout=2)
    manager.shutdown()


def test_indeterminate_worker_result_is_attention_terminal(monkeypatch) -> None:
    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task",
        lambda **_kwargs: SubagentResult(
            status="indeterminate",
            summary="write outcome unknown",
            partial=True,
        ),
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(
        parent_agent=parent, task="write", mode="execute", auto_verify=False
    )
    job = manager.wait_job(job_id, timeout=2)

    assert job is not None and job.status == "indeterminate"
    assert job.error == "write outcome unknown"
    assert [item.id for item in manager.drain_completed_for_parent()] == [job_id]
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

    child_tool = next(
        tool
        for tool in _filter_subagent_tools(parent, "explore")
        if tool.name == "read_file"
    )

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

    child_tool = next(
        tool
        for tool in _filter_subagent_tools(parent, "explore")
        if tool.name == "read_file"
    )
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

    child_tool = next(
        tool
        for tool in _filter_subagent_tools(parent, "execute")
        if tool.name == "shell"
    )
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

    job_id = manager.submit_background(
        parent_agent=_Parent(),
        task="bounded",
        mode="execute",
        timeout_seconds=999,
        auto_verify=False,
    )
    manager.wait_job(job_id, timeout=2)

    assert captured["timeout"] == 7
    manager.shutdown()
