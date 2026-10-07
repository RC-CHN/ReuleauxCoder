"""Unattended frontend composition over the same runtime used by CLI/TUI."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
import traceback
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.rpc.codec import encode
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.images import ChatInput
from reuleauxcoder.domain.jobs import EXIT_CODES, JobSpec
from reuleauxcoder.infrastructure.persistence.job_store import (
    JobJournal,
    JobStore,
    atomic_json,
    lease,
    read_json,
)
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies, AppOptions
from reuleauxcoder.interfaces.entrypoint.rpc import connect_local
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner
from reuleauxcoder.interfaces.job_interactor import JobInteractor


def _has_prompt(agent, prompt):
    return any(
        event.kind == "message_committed"
        and event.payload.get("source") == "user_input"
        and event.payload.get("message", {}).get("content") == prompt
        for event in agent.history_ledger.events
    )


def _verify(spec: JobSpec, journal: JobJournal, stopped):
    artifacts, checks = [], []
    workspace = Path(spec.workspace)
    for index, check in enumerate(spec.checks):
        log = (
            journal.path
            / "attempts"
            / f"{journal.state['attempt_id']}-check-{index}.log"
        )
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            process = subprocess.Popen(
                check.argv,
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
            deadline = time.monotonic() + check.timeout_seconds
            timed_out = False
            try:
                while process.poll() is None:
                    if stopped() or time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()
            checks.append(
                {
                    "argv": check.argv,
                    "exit_code": process.returncode,
                    "timed_out": timed_out,
                    "log": str(log),
                }
            )
        if stopped():
            break
    for name in spec.artifacts:
        path = (workspace / name).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            artifacts.append(
                {"path": name, "error": "Required workspace file is missing"}
            )
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        artifacts.append(
            {"path": name, "size": path.stat().st_size, "sha256": digest.hexdigest()}
        )
    passed = all("error" not in item for item in artifacts) and all(
        item["exit_code"] == 0 and not item["timed_out"] for item in checks
    )
    return {
        "passed": passed,
        "artifacts": artifacts,
        "checks": checks,
        "basis": "declared_checks"
        if spec.checks or spec.artifacts
        else "agent_reported",
    }


def run_job(
    store: JobStore,
    job_id: str,
    *,
    resume: bool = False,
    token_budget: int | None = None,
    max_seconds: float | None = None,
    dependencies: AppDependencies | None = None,
) -> int:
    """A job owns its session directory and exactly one workspace lease."""
    path = store.path(job_id)
    spec = store.spec(job_id)
    workspace_control = Path(spec.workspace) / ".rcoder"
    workspace_control.mkdir(exist_ok=True)
    with lease(path / "run.lock"), lease(workspace_control / "unattended.lock"):
        journal = JobJournal(store, job_id)
        if journal.state["status"] in {"completed", "cancelled"}:
            return EXIT_CODES[journal.state["status"]]
        if journal.state["session_id"] and not resume:
            raise ValueError("This job has a session; use job resume")
        attempt_id = uuid4().hex
        started = time.monotonic()
        previous_seconds = journal.state["seconds_used"]
        previous_response = journal.state.get("response", "")
        journal.update(
            status="starting",
            attempt_id=attempt_id,
            pid=os.getpid(),
            error=None,
            verification=None,
            token_budget=token_budget
            if token_budget is not None
            else journal.state["token_budget"],
            max_seconds=max_seconds
            if max_seconds is not None
            else journal.state["max_seconds"],
        )
        journal.event("attempt_started", {"resume": resume})
        stopped = threading.Event()
        stop_reason = None
        signals = {}
        if threading.current_thread() is threading.main_thread():
            for name in (signal.SIGINT, signal.SIGTERM):
                signals[name] = signal.signal(name, lambda *_: stopped.set())
        old_cwd = Path.cwd()
        old_env = {
            name: os.environ.get(name)
            for name in ("RCODER_JOB_ID", "RCODER_JOB_ATTEMPT_ID")
        }
        os.chdir(spec.workspace)
        os.environ.update(RCODER_JOB_ID=job_id, RCODER_JOB_ATTEMPT_ID=attempt_id)
        runner = connection = agent = None
        status = "failed"
        response = previous_response
        verification = None
        final_goal = None
        error = None
        last_heartbeat = 0.0

        def should_stop():
            nonlocal stop_reason
            control = path / "control.json"
            if control.exists():
                request = read_json(control)
                if request.get("attempt_id") == attempt_id:
                    stop_reason = (
                        "cancelled" if request["action"] == "cancel" else "paused"
                    )
                    stopped.set()
            limit = journal.state["max_seconds"]
            if (
                stop_reason is None
                and limit is not None
                and previous_seconds + time.monotonic() - started >= limit
            ):
                stop_reason = "time_limited"
                stopped.set()
            return stopped.is_set()

        def on_completed(result):
            nonlocal response
            if result.response:
                response = result.response
                journal.update(response=response)

        try:
            base = dependencies or AppDependencies()

            def load_config(config_path):
                config = base.load_config(config_path)
                if config.remote_exec.enabled and config.remote_exec.host_mode:
                    raise ValueError(
                        "Unattended jobs need a workspace runtime, not a relay host"
                    )
                if not config.api_key:
                    raise ValueError("No model API key configured")
                config.session_dir = str(path / "sessions")
                config.session_auto_save = True
                return config

            runner = AppRunner(
                AppOptions(
                    config_path=Path(spec.config) if spec.config else None,
                    model=spec.model,
                    resume_session_id=journal.state["session_id"],
                    auto_resume_latest=False,
                ),
                dependencies=replace(base, load_config=load_config),
                startup_progress=lambda text: journal.event(
                    "startup", {"message": text}
                ),
            )
            ctx = runner.initialize()
            agent = ctx.agent
            ctx.agent.persist_runtime_snapshot()
            journal.update(
                session_id=ctx.agent.current_session_id, agent_id=ctx.agent.agent_id
            )
            if (
                ctx.agent.active_mode == "planner"
                or not ctx.agent.is_tool_allowed_in_mode("update_goal")
            ):
                raise ValueError("Job mode must permit goal execution and completion")
            interactor = JobInteractor(journal, attempt_id)
            bus = UIEventBus()
            bus.subscribe(
                lambda event: journal.event("ui", encode(event)), replay_history=False
            )
            connection = connect_local(
                ctx,
                UIProfile(
                    "job",
                    "Unattended job",
                    frozenset({UICapability.TEXT_INPUT, UICapability.STREAM_OUTPUT}),
                ),
                bus,
                interactor,
                activate=False,
            )
            client = connection.client
            interactor.client = client
            client.on_completed = on_completed
            controller = ctx.agent.goal_controller
            goal = controller.state
            if goal is None:
                budget = journal.state["token_budget"]
                if budget is None:
                    budget = ctx.config.goal_default_token_budget
                goal = controller.create(spec.objective, budget)
                journal.update(token_budget=budget)
            elif goal.objective != spec.objective:
                raise ValueError("Restored goal does not match the job objective")
            elif goal.status != "complete":
                if token_budget is not None:
                    controller.update(token_budget=token_budget, change_budget=True)
                if resume and goal.status != "active":
                    controller.update(status="active")
            ctx.agent.clear_stop_request()
            journal.update(goal=controller.state.to_dict(), status="running")

            def pump():
                nonlocal last_heartbeat
                if should_stop():
                    client.interrupt()
                now = time.monotonic()
                if now - last_heartbeat >= 1:
                    snapshot = client.state
                    journal.update(
                        status="stopping"
                        if stopped.is_set()
                        else "waiting_input"
                        if interactor.waiting
                        else "running",
                        goal=asdict(snapshot.goal) if snapshot.goal else None,
                        seconds_used=previous_seconds + now - started,
                        heartbeat_at=time.time(),
                    )
                    last_heartbeat = now

            if not should_stop():
                # The session ledger proves admission even if the process died
                # before writing a job receipt. Never replay admitted tool work.
                if not _has_prompt(ctx.agent, spec.prompt):
                    if controller.state.status != "active":
                        raise ValueError("Goal cannot start within its current budget")
                    client.submit(ChatInput(text=spec.prompt))
                client.ready()
                client.wait_idle(pump=pump)
            final_goal = controller.state.to_dict()
            if final_goal["objective"] != spec.objective:
                raise ValueError("The runtime replaced the assigned job objective")
            status = stop_reason or (
                "paused" if stopped.is_set() else final_goal["status"]
            )
            if status == "complete":
                journal.update(status="verifying")
                verification = _verify(spec, journal, should_stop)
                status = stop_reason or (
                    "paused"
                    if stopped.is_set()
                    else "completed"
                    if verification["passed"]
                    else "verification_failed"
                )
            if status not in EXIT_CODES:
                status = "blocked"
        except BaseException as exception:  # noqa: BLE001 - process boundary records every terminal failure
            error = {"type": type(exception).__name__, "message": str(exception)}
            journal.event("failure", error)
            traceback.print_exc()
            if stopped.is_set() or isinstance(exception, KeyboardInterrupt):
                status = stop_reason or "paused"
            elif agent is not None:
                goal = agent.goal_controller.state
                status = (
                    goal.status
                    if goal
                    and goal.status in {"blocked", "usage_limited", "budget_limited"}
                    else "failed"
                )
        finally:
            try:
                try:
                    if connection is not None:
                        connection.close()
                finally:
                    if runner is not None:
                        runner.cleanup()
            except BaseException as exception:  # noqa: BLE001 - cleanup failures must invalidate success
                status = "failed"
                error = {"type": type(exception).__name__, "message": str(exception)}
                journal.event("cleanup_failed", error)
                traceback.print_exc()
            finally:
                if agent is not None:
                    goal = agent.goal_controller.state
                    final_goal = goal.to_dict() if goal else None
                os.chdir(old_cwd)
                for name, value in old_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                for name, handler in signals.items():
                    signal.signal(name, handler)
                # Terminal publication and control admission share one lock:
                # a stop accepted before this commit wins over completion.
                with lease(path / "control.lock", wait=True):
                    should_stop()
                    if stopped.is_set() and status != "failed":
                        status = stop_reason or "paused"
                    journal.event("attempt_finished", {"status": status})
                    result = {
                        **journal.state,
                        "status": status,
                        "goal": final_goal,
                        "error": error,
                        "response": response,
                        "verification": verification,
                        "seconds_used": previous_seconds + time.monotonic() - started,
                        "exit_code": EXIT_CODES[status],
                        "finished_at": time.time(),
                    }
                    atomic_json(path / "attempts" / f"{attempt_id}.json", result)
                    atomic_json(path / "result.json", result)
                    journal.update(**result)
        return EXIT_CODES[status]
