import json
from pathlib import Path
import subprocess

import pytest

from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.extensions.subagent.context import project_parent_context
from reuleauxcoder.extensions.subagent.isolation import create_worktree, remove_worktree
from reuleauxcoder.extensions.subagent.manager import SubagentManager
from reuleauxcoder.extensions.subagent.models import (
    SubagentResult,
    SubagentTranscriptStore,
)


class _Parent:
    agent_id = "root"
    session_generation = 0
    current_session_id = "session"
    subagent_depth = 0

    def __init__(self) -> None:
        self.history_ledger = HistoryLedger(session_id="session", agent_id="root")
        self.messages = [
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current request"},
        ]

    def persist_runtime_snapshot(self) -> None:
        pass


def test_context_projection_modes_are_bounded() -> None:
    parent = _Parent()
    minimal = project_parent_context(parent, "minimal")
    full = project_parent_context(parent, "full")
    assert "current request" in minimal
    assert "old answer" not in minimal
    assert "old answer" in full


def test_result_projection_hash_ignores_wall_clock_duration() -> None:
    first = SubagentResult(
        status="ok", summary="done", files=["a.py"], duration_seconds=1.0
    )
    second = SubagentResult(
        status="ok", summary="done", files=["a.py"], duration_seconds=999.0
    )
    assert first.content_hash() == second.content_hash()
    assert first.model_text() == second.model_text()
    assert "duration_seconds" not in first.model_text()


def test_transcript_checkpoint_is_atomic_and_hash_validated(tmp_path) -> None:
    store = SubagentTranscriptStore(tmp_path)
    reference = store.write(
        "sj_hash",
        [{"role": "assistant", "content": "stable"}],
        {"status": "blocked"},
    )
    assert store.read(reference) == [
        {"role": "assistant", "content": "stable"}
    ]
    payload = json.loads(Path(reference).read_text(encoding="utf-8"))
    payload["messages"][0]["content"] = "tampered"
    Path(reference).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.read(reference)


def test_background_completion_is_drained_from_mailbox(monkeypatch) -> None:
    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task",
        lambda **_kwargs: "conclusion",
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(parent_agent=parent, task="scan", mode="explore")
    job = manager.wait_job(job_id, timeout=2)
    assert job is not None and job.status == "completed"
    assert job.injected_to_parent is False
    assert [item.id for item in manager.drain_completed_for_parent()] == [job_id]
    assert manager.drain_completed_for_parent() == []
    manager.shutdown()


def test_follow_up_links_invocations(monkeypatch) -> None:
    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task",
        lambda **_kwargs: "done",
    )
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    first = manager.submit_background(parent_agent=parent, task="first", mode="explore")
    manager.wait_job(first, timeout=2)
    second = manager.follow_up(parent_agent=parent, job_id=first, message="clarify")
    resumed = manager.wait_job(second, timeout=2)
    assert resumed is not None and resumed.parent_job_id == first
    manager.shutdown()


def test_depth_limit_rejects_unbounded_recursion() -> None:
    manager = SubagentManager(max_depth=1)
    with pytest.raises(ValueError, match="depth limit"):
        manager.submit_background(
            parent_agent=_Parent(), task="too deep", mode="explore", depth=2
        )
    manager.shutdown()


def test_running_agent_message_queue_is_lossless(monkeypatch) -> None:
    release = __import__("threading").Event()

    def run(**_kwargs):
        release.wait(timeout=2)
        return "done"

    monkeypatch.setattr("reuleauxcoder.extensions.subagent.manager.run_subagent_task", run)
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(parent_agent=parent, task="wait", mode="explore")
    for _ in range(100):
        if manager.get_job(job_id).status == "running":
            break
        __import__("time").sleep(0.01)
    assert manager.send_message(job_id, "new constraint") is True
    directives = manager.drain_messages(job_id)
    assert [item.content for item in directives] == ["new constraint"]
    assert directives[0].directive_id.startswith("sd_")
    assert len(directives[0].content_hash) == 64
    assert manager.drain_messages(job_id) == []
    communication_events = [
        event
        for event in parent.history_ledger.events
        if event.kind.startswith("subagent_communication_")
        and event.payload.get("direction") == "parent_to_child"
    ]
    assert [event.kind for event in communication_events] == [
        "subagent_communication_queued",
        "subagent_communication_delivered",
    ]
    release.set()
    manager.wait_job(job_id, timeout=2)
    manager.shutdown()


def test_directive_arriving_before_park_completion_resumes_without_loss(
    monkeypatch, tmp_path
) -> None:
    import threading
    import time

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=2)
            reference = tmp_path / "checkpoint.json"
            reference.write_text('{"messages": []}', encoding="utf-8")
            return SubagentResult(
                status="blocked",
                summary="waiting",
                transcript_ref=str(reference),
            )
        return SubagentResult(status="ok", summary="continued")

    monkeypatch.setattr("reuleauxcoder.extensions.subagent.manager.run_subagent_task", run)
    manager = SubagentManager(max_parallel_explore=1)
    parent = _Parent()
    job_id = manager.submit_background(
        parent_agent=parent, task="park", mode="explore", auto_verify=False
    )
    assert entered.wait(timeout=2)
    assert manager.send_message(job_id, "continue with option A") is True
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = manager.get_job(job_id)
        if job is not None and job.status == "completed":
            break
        time.sleep(0.01)
    assert job is not None and job.status == "completed"
    assert job.id == job_id
    assert len(calls) == 2
    assert calls[1]["resume_directives"]
    assert "continue with option A" in calls[1]["resume_directives"][0]
    manager.shutdown()


def test_worktree_lease_round_trip(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    lease = create_worktree(tmp_path, "sj_test")
    assert (lease.path / "tracked.txt").read_text(encoding="utf-8") == "base"
    remove_worktree(lease)
    assert not lease.path.exists()
