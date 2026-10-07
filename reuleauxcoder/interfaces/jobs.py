"""Machine-readable job commands. Task prose remains ordinary user input."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from reuleauxcoder.domain.goal import validate_budget
from reuleauxcoder.domain.jobs import JobSpec
from reuleauxcoder.infrastructure.persistence.job_store import (
    JobStore,
    read_json,
    valid_id,
)


def _text(path):
    if path == "-":
        return (
            sys.stdin.buffer.read().decode("utf-8-sig")
            if hasattr(sys.stdin, "buffer")
            else sys.stdin.read()
        )
    return Path(path).read_text(encoding="utf-8-sig")


def _print(value):
    # In redirected Windows consoles Python otherwise commonly selects GBK.
    text = json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(text)


def _launch(store, job_id, args):
    if not args.detach:
        from contextlib import redirect_stdout

        from reuleauxcoder.interfaces.entrypoint.job import run_job

        with redirect_stdout(sys.stderr):
            code = run_job(
                store,
                job_id,
                resume=args.command == "resume",
                token_budget=args.token_budget,
                max_seconds=args.max_seconds,
            )
        _print(store.status(job_id))
        return code
    previous_attempt = store.status(job_id).get("attempt_id")
    command = [
        sys.executable,
        "-m",
        "reuleauxcoder",
        "job",
        "--store",
        str(store.root),
        "_run",
        job_id,
    ]
    if args.command == "resume":
        command.append("--resume")
    if args.token_budget is not None:
        command.extend(["--token-budget", str(args.token_budget)])
    if args.max_seconds is not None:
        command.extend(["--max-seconds", str(args.max_seconds)])
    descriptor = os.open(
        store.path(job_id) / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    with os.fdopen(descriptor, "ab") as output:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            if os.name == "nt"
            else 0,
        )
    # Observe the worker's lease-backed admission, including immediate failures
    # such as an occupied workspace. Spawning a process alone is not admission.
    deadline = time.monotonic() + 10
    while True:
        state = store.status(job_id)
        admitted = state.get("pid") == process.pid and state.get("attempt_id") not in {
            None,
            previous_attempt,
        }
        code = process.poll()
        if admitted or code is not None or time.monotonic() >= deadline:
            _print(
                {
                    "job_id": job_id,
                    "launch_pid": process.pid,
                    "launch_state": "admitted"
                    if admitted
                    else "failed"
                    if code is not None
                    else "pending",
                    "state": state,
                    "worker_log": str(store.path(job_id) / "worker.log"),
                }
            )
            return code if code is not None else 0
        time.sleep(0.05)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="rcoder job",
        description="Run and supervise unattended goals without terminal input",
    )
    parser.add_argument(
        "--store", type=Path, help="Private job registry (default: ~/.rcoder/jobs)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Create an independent task/session")
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--task", help="Ordinary task instructions, passed verbatim to the runtime"
    )
    source.add_argument("--task-file", help="UTF-8 task instructions; - reads stdin")
    source.add_argument("--spec", type=Path, help="Version 1 JSON launch specification")
    start.add_argument("--workspace", type=Path)
    start.add_argument("--id", help="Stable caller-supplied idempotency key")
    start.add_argument(
        "--objective",
        default="",
        help="Short goal when instructions exceed 4000 characters",
    )
    start.add_argument("--config", type=Path)
    start.add_argument("--model")
    start.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Required workspace file (repeatable)",
    )
    resume = commands.add_parser(
        "resume", help="Explicitly resume the same job and saved session"
    )
    resume.add_argument("id")
    worker = commands.add_parser(
        "_run", help="Internal detached worker (use start or resume)"
    )
    worker.add_argument("id")
    worker.add_argument("--resume", action="store_true")
    for command in (start, resume, worker):
        command.add_argument("--token-budget", type=int)
        command.add_argument(
            "--max-seconds", type=float, help="Cumulative active execution time limit"
        )
        if command is not worker:
            command.add_argument("--detach", action="store_true")
    for name in ("status", "pause", "cancel", "result"):
        commands.add_parser(name).add_argument("id")
    commands.add_parser("list")
    logs = commands.add_parser("logs", help="Read structured events after a cursor")
    logs.add_argument("id")
    logs.add_argument("--after", type=int, default=0)
    logs.add_argument("--limit", type=int, default=100)
    answer = commands.add_parser(
        "answer", help="Answer a currently pending interaction"
    )
    answer.add_argument("id")
    answer.add_argument("request_id")
    answer.add_argument(
        "--answer-file", required=True, help="UTF-8 JSON response; - reads stdin"
    )
    args = parser.parse_args(argv)
    try:
        store = JobStore(args.store)
        if args.command in {"start", "resume", "_run"}:
            validate_budget(args.token_budget)
            if args.max_seconds is not None:
                import math

                if not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
                    raise ValueError("max-seconds must be positive and finite")
        if args.command == "start":
            if args.spec:
                if any(
                    (
                        args.workspace,
                        args.objective,
                        args.config,
                        args.model,
                        args.artifact,
                        args.token_budget is not None,
                        args.max_seconds is not None,
                    )
                ):
                    raise ValueError(
                        "--spec cannot be mixed with task specification flags"
                    )
                spec = JobSpec.from_dict(json.loads(_text(str(args.spec))))
            else:
                spec = JobSpec(
                    workspace=str(args.workspace or Path.cwd()),
                    prompt=args.task
                    if args.task is not None
                    else _text(args.task_file),
                    objective=args.objective,
                    config=str(args.config) if args.config else None,
                    model=args.model,
                    token_budget=args.token_budget,
                    max_seconds=args.max_seconds,
                    artifacts=args.artifact,
                )
            job_id, created = store.create(spec, args.id)
            if not created:
                _print(store.status(job_id))
                return 0
            return _launch(store, job_id, args)
        if args.command == "resume":
            state = store.status(args.id)
            if state["status"] in {"completed", "cancelled"}:
                from reuleauxcoder.domain.jobs import EXIT_CODES

                _print(state)
                return EXIT_CODES[state["status"]]
            if state["alive"]:
                _print(state)
                return 0
            return _launch(store, args.id, args)
        if args.command == "_run":
            from reuleauxcoder.interfaces.entrypoint.job import run_job

            return run_job(
                store,
                args.id,
                resume=args.resume,
                token_budget=args.token_budget,
                max_seconds=args.max_seconds,
            )
        if args.command == "status":
            _print(store.status(args.id))
        elif args.command == "list":
            _print(
                [
                    store.status(path.name)
                    for path in sorted(store.root.iterdir())
                    if path.is_dir() and (path / "state.json").is_file()
                ]
            )
        elif args.command in {"pause", "cancel"}:
            _print(store.control(args.id, args.command))
        elif args.command == "result":
            _print(read_json(store.path(args.id) / "result.json"))
        elif args.command == "logs":
            if not 1 <= args.limit <= 1000 or args.after < 0:
                raise ValueError("Use a nonnegative cursor and limit 1–1000")
            events = []
            path = store.path(args.id) / "events.jsonl"
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # A concurrent append may have a partial tail.
                        if event["seq"] > args.after:
                            events.append(event)
                            if len(events) == args.limit:
                                break
            _print(
                {
                    "events": events,
                    "cursor": events[-1]["seq"] if events else args.after,
                }
            )
        elif args.command == "answer":
            from reuleauxcoder.interfaces.job_interactor import validate_stored_answer

            value = json.loads(_text(args.answer_file))
            pending = read_json(
                store.path(args.id) / "requests" / f"{valid_id(args.request_id)}.json"
            )
            validate_stored_answer(pending, value)
            store.answer(args.id, args.request_id, value)
            _print(
                {"job_id": args.id, "request_id": args.request_id, "submitted": True}
            )
        return 0
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        print(
            json.dumps(
                {"error": type(error).__name__, "message": str(error)},
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
