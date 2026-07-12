import subprocess
from types import SimpleNamespace

import pytest

from reuleauxcoder.extensions.subagent.context import project_parent_context
from reuleauxcoder.extensions.subagent.isolation import create_worktree, remove_worktree
from reuleauxcoder.extensions.subagent.manager import SubagentManager


class _Parent:
    agent_id = "root"
    session_generation = 0
    current_session_id = "session"
    subagent_depth = 0

    def __init__(self) -> None:
        self.messages = [
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current request"},
        ]


def test_context_projection_modes_are_bounded() -> None:
    parent = _Parent()
    minimal = project_parent_context(parent, "minimal")
    full = project_parent_context(parent, "full")
    assert "current request" in minimal
    assert "old answer" not in minimal
    assert "old answer" in full


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
    job_id = manager.submit_background(parent_agent=_Parent(), task="wait", mode="explore")
    for _ in range(100):
        if manager.get_job(job_id).status == "running":
            break
        __import__("time").sleep(0.01)
    assert manager.send_message(job_id, "new constraint") is True
    assert manager.drain_messages(job_id) == ["new constraint"]
    assert manager.drain_messages(job_id) == []
    release.set()
    manager.wait_job(job_id, timeout=2)
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
