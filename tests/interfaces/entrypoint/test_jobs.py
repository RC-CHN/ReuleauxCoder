"""Unattended jobs exercise the real runtime/ledger without paid model calls."""

import json
import multiprocessing
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.interaction_contracts import (
    InputTextRequest,
    ReviewDocument,
    ReviewRequest,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.jobs import JobCheck, JobSpec
from reuleauxcoder.infrastructure.persistence.job_store import (
    JobJournal,
    JobStore,
    LeaseBusy,
    atomic_json,
    is_locked,
    lease,
    read_json,
)
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies
from reuleauxcoder.interfaces.entrypoint.job import run_job
from reuleauxcoder.interfaces.job_interactor import JobInteractor


class FakeLLM:
    model = "test-model"
    debug_trace = False

    def reconfigure(self, **values):
        self.__dict__.update(values)


def dependencies(handler):
    def create_agent(llm, tools, config, hooks):
        loop = SimpleNamespace()
        agent = Agent(llm, tools=tools, config=config, hook_registry=hooks, loop=loop)
        loop.run = lambda: handler(agent)
        return agent

    return replace(
        AppDependencies(),
        load_config=lambda _: Config(api_key="test", lsp={"enabled": False}),
        create_llm=lambda _: FakeLLM(),
        load_tools=lambda _: [],
        create_agent=create_agent,
    )


def make_job(tmp_path, **kwargs):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = JobStore(tmp_path / "jobs")
    spec = JobSpec(
        str(workspace), "在当前工作区完成任务，并保存成果和交接记录。", **kwargs
    )
    job_id, _ = store.create(spec)
    return store, job_id


def test_job_runs_multiple_turns_and_checks_real_outputs(tmp_path):
    store, job_id = make_job(
        tmp_path,
        artifacts=["result.txt"],
        checks=[
            JobCheck(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('result.txt').read_text() == 'done'",
                ]
            ),
        ],
    )
    turns = []

    def work(agent):
        turns.append(1)
        if len(turns) == 2:
            Path("result.txt").write_text("done")
            agent.goal_controller.update(status="complete")
        return "done" if len(turns) == 2 else "working"

    assert run_job(store, job_id, dependencies=dependencies(work)) == 0
    state = store.status(job_id)
    assert state["status"] == "completed"
    assert not state["alive"]
    assert len(turns) == 2
    assert state["verification"]["passed"]
    assert state["verification"]["artifacts"][0]["sha256"]
    assert state["response"] == "done"
    assert read_json(store.path(job_id) / "result.json")["exit_code"] == 0
    assert run_job(store, job_id, resume=True, dependencies=dependencies(work)) == 0
    assert len(turns) == 2


def test_failed_job_resumes_exact_session_without_replaying_initial_instructions(
    tmp_path,
):
    store, job_id = make_job(tmp_path)
    sessions, user_messages = [], []

    def fail(agent):
        sessions.append(agent.current_session_id)
        raise RuntimeError("temporary failure after message admission")

    assert run_job(store, job_id, dependencies=dependencies(fail)) == 3
    first = store.status(job_id)

    def finish(agent):
        sessions.append(agent.current_session_id)
        user_messages.extend(
            event
            for event in agent.history_ledger.events
            if event.kind == "message_committed"
            and event.payload.get("source") == "user_input"
        )
        agent.goal_controller.update(status="complete")
        return "recovered"

    assert run_job(store, job_id, resume=True, dependencies=dependencies(finish)) == 0
    assert sessions[0] == sessions[1] == first["session_id"]
    assert len(user_messages) == 1
    assert first["attempt_id"] != store.status(job_id)["attempt_id"]


def test_same_id_is_idempotent_and_workspaces_get_independent_sessions(tmp_path):
    store, job_id = make_job(tmp_path)
    spec = store.spec(job_id)
    assert store.create(spec, job_id) == (job_id, False)
    with pytest.raises(ValueError, match="different"):
        store.create(replace(spec, prompt="different instructions"), job_id)
    second = tmp_path / "second"
    second.mkdir()
    other_id, _ = store.create(replace(spec, workspace=str(second)))

    def finish(agent):
        agent.goal_controller.update(status="complete")
        return "done"

    assert run_job(store, job_id, dependencies=dependencies(finish)) == 0
    assert run_job(store, other_id, dependencies=dependencies(finish)) == 0
    assert store.status(job_id)["session_id"] != store.status(other_id)["session_id"]


def test_concurrent_creation_has_one_winner(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = JobStore(tmp_path / "registry")
    spec = JobSpec(str(workspace), "Do the task")
    with ThreadPoolExecutor(8) as executor:
        results = list(
            executor.map(lambda _: store.create(spec, "stable-key"), range(16))
        )
    assert sum(created for _, created in results) == 1


def test_job_and_workspace_leases_prevent_duplicate_execution(tmp_path):
    store, job_id = make_job(tmp_path)
    called = []
    with lease(store.path(job_id) / "run.lock"), pytest.raises(LeaseBusy):
        run_job(store, job_id, dependencies=dependencies(lambda _: called.append(1)))
    control = Path(store.spec(job_id).workspace) / ".rcoder"
    control.mkdir(exist_ok=True)
    with lease(control / "unattended.lock"), pytest.raises(LeaseBusy):
        run_job(store, job_id, dependencies=dependencies(lambda _: called.append(1)))
    assert not called


def _hold_lease(path, ready):
    with lease(Path(path)):
        ready.set()
        time.sleep(60)


def test_process_death_releases_lease(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    path = tmp_path / "run.lock"
    child = context.Process(target=_hold_lease, args=(str(path), ready))
    child.start()
    try:
        assert ready.wait(10)
        assert is_locked(path)
    finally:
        child.terminate()
        child.join(10)
    assert not child.is_alive()
    assert not is_locked(path)


def test_control_at_completion_wins_and_cannot_target_next_attempt(tmp_path):
    store, job_id = make_job(tmp_path)

    def finish(agent):
        agent.goal_controller.update(status="complete")
        store.control(job_id, "cancel")
        return "done"

    assert run_job(store, job_id, dependencies=dependencies(finish)) == 130
    assert store.status(job_id)["status"] == "cancelled"
    with pytest.raises(ValueError, match="not running"):
        store.control(job_id, "pause")


def test_old_control_is_ignored_and_failed_checks_are_not_success(tmp_path):
    store, job_id = make_job(tmp_path, artifacts=["missing.txt"])
    atomic_json(
        store.path(job_id) / "control.json", {"attempt_id": "stale", "action": "cancel"}
    )

    def finish(agent):
        agent.goal_controller.update(status="complete")
        return "done"

    assert run_job(store, job_id, dependencies=dependencies(finish)) == 8
    assert store.status(job_id)["status"] == "verification_failed"


def test_pending_question_accepts_only_current_attempt_answer(tmp_path):
    store, job_id = make_job(tmp_path)
    journal = JobJournal(store, job_id)
    journal.update(status="waiting_input", attempt_id="current")
    adapter = JobInteractor(journal, "current")
    request = InputTextRequest("Clarification", "Which target?")
    with lease(store.path(job_id) / "run.lock"), ThreadPoolExecutor(1) as executor:
        future = executor.submit(adapter.input_text, request)
        deadline = time.monotonic() + 5
        while not (journal.path / "requests" / f"{request.request_id}.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        atomic_json(
            journal.path / "answers" / f"{request.request_id}.json",
            {
                "attempt_id": "old",
                "response": {"value": "wrong"},
            },
        )
        deadline = time.monotonic() + 5
        while (journal.path / "answers" / f"{request.request_id}.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert not future.done()
        store.answer(job_id, request.request_id, {"value": "correct"})
        assert future.result(5).value == "correct"
        with pytest.raises(FileNotFoundError):
            store.answer(job_id, request.request_id, {"value": "late"})


def test_event_cursor_recovers_after_stale_snapshot_and_partial_tail(tmp_path):
    store, job_id = make_job(tmp_path)
    journal = JobJournal(store, job_id)
    journal.event("first", {})
    journal.event("second", {})
    with (journal.path / "events.jsonl").open("ab") as stream:
        stream.write(b'{"seq":')
    recovered = JobJournal(store, job_id)
    assert recovered.event("third", {})["seq"] == 3
    events = [
        json.loads(line)
        for line in (journal.path / "events.jsonl").read_text().splitlines()
    ]
    assert [event["seq"] for event in events] == [1, 2, 3]


def test_pause_resume_retains_usage_and_does_not_repeat_prompt(tmp_path):
    store, job_id = make_job(tmp_path, token_budget=100)

    def pause(agent):
        store.control(job_id, "pause")
        return "checkpoint"

    assert run_job(store, job_id, dependencies=dependencies(pause)) == 4
    first = store.status(job_id)

    def finish(agent):
        assert agent.goal_controller.state.token_budget == 100
        agent.goal_controller.update(status="complete")
        return "done"

    assert run_job(store, job_id, resume=True, dependencies=dependencies(finish)) == 0
    second = store.status(job_id)
    assert second["session_id"] == first["session_id"]
    assert second["seconds_used"] >= first["seconds_used"]


def test_time_limit_during_startup_never_submits_model_work(tmp_path):
    store, job_id = make_job(tmp_path, max_seconds=0.001)
    called = []
    assert (
        run_job(store, job_id, dependencies=dependencies(lambda _: called.append(1)))
        == 7
    )
    assert not called


def test_verification_timeout_and_hash_after_check(tmp_path):
    store, job_id = make_job(
        tmp_path,
        artifacts=["generated.txt"],
        checks=[
            JobCheck(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('generated.txt').write_text('checked')",
                ]
            ),
            JobCheck(
                [sys.executable, "-c", "import time; time.sleep(10)"], timeout_seconds=1
            ),
        ],
    )

    def finish(agent):
        agent.goal_controller.update(status="complete")
        return "done"

    assert run_job(store, job_id, dependencies=dependencies(finish)) == 8
    result = store.status(job_id)["verification"]
    assert result["checks"][1]["timed_out"]
    assert result["artifacts"][0]["sha256"]


def test_missing_resume_session_fails_without_creating_replacement(tmp_path):
    import shutil

    store, job_id = make_job(tmp_path)

    def fail(_):
        raise RuntimeError("failure")

    assert run_job(store, job_id, dependencies=dependencies(fail)) == 3
    before = store.status(job_id)["session_id"]
    shutil.rmtree(store.path(job_id) / "sessions")
    called = []
    assert (
        run_job(
            store,
            job_id,
            resume=True,
            dependencies=dependencies(lambda _: called.append(1)),
        )
        == 1
    )
    assert not called and store.status(job_id)["session_id"] == before


def test_cancellation_rejects_late_approval(tmp_path):
    store, job_id = make_job(tmp_path)
    journal = JobJournal(store, job_id)
    journal.update(status="waiting_input", attempt_id="current")
    adapter = JobInteractor(journal, "current")
    request = ReviewRequest("Approve edit", "Change a file")
    with lease(store.path(job_id) / "run.lock"), ThreadPoolExecutor(1) as pool:
        pending = pool.submit(adapter.review, request)
        deadline = time.monotonic() + 5
        while not (journal.path / "requests" / f"{request.request_id}.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        store.control(job_id, "cancel")
        with pytest.raises(ValueError, match="stopping"):
            store.answer(job_id, request.request_id, {"approved": True})
        adapter.cancel(request.request_id)
        assert not pending.result(5).approved


def test_review_copies_full_frozen_pages_before_requesting_approval(tmp_path):
    store, job_id = make_job(tmp_path)
    adapter = JobInteractor(JobJournal(store, job_id), "current")
    document = ReviewDocument("doc", "new.txt", False, None, 0, 6)
    request = ReviewRequest("Approve edit", "New file", documents=(document,))
    calls = []

    def page(method, args):
        calls.append(args)
        return {
            "text": "abc" if args["offset"] == 0 else "def",
            "complete": args["offset"] > 0,
        }

    adapter.client = SimpleNamespace(peer=SimpleNamespace(request=page))
    received = []
    adapter._request = lambda kind, req: received.append(req)
    adapter.review(request)
    assert received[0].documents[0].after == "abcdef"
    assert all(item["side"] == "after" for item in calls)
    assert document.after is None


def test_selected_workspace_configuration_is_not_import_time_directory(
    tmp_path, monkeypatch
):
    from reuleauxcoder.interfaces.entrypoint.dependencies import _default_load_config
    from reuleauxcoder.services.config.loader import ConfigLoader

    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", tmp_path / "absent")
    (tmp_path / ".rcoder").mkdir()
    (tmp_path / ".rcoder/config.yaml").write_text(
        "app:\n  model: workspace-model\n  api_key: workspace-key\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    config = _default_load_config(None)
    assert config.model == "workspace-model" and config.api_key == "workspace-key"


def test_budget_resume_preserves_spent_tokens(tmp_path):
    store, job_id = make_job(tmp_path, token_budget=10)

    def spend(agent):
        controller = agent.goal_controller
        controller.record_usage(
            controller.state.id,
            {
                "input_tokens": 12,
                "cached_input_tokens": 2,
                "output_tokens": 3,
                "estimated": False,
            },
        )
        return "limit reached"

    assert run_job(store, job_id, dependencies=dependencies(spend)) == 5
    first = store.status(job_id)
    assert first["goal"]["tokens_used"] == 13
    called = []
    assert (
        run_job(
            store,
            job_id,
            resume=True,
            dependencies=dependencies(lambda _: called.append(1)),
        )
        == 5
    )
    assert not called

    def finish(agent):
        assert agent.goal_controller.state.tokens_used == 13
        agent.goal_controller.update(status="complete")
        return "done"

    assert (
        run_job(
            store,
            job_id,
            resume=True,
            token_budget=30,
            dependencies=dependencies(finish),
        )
        == 0
    )
    assert store.status(job_id)["goal"]["tokens_used"] == 13
