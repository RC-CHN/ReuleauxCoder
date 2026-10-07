"""Private job records and OS-owned leases, independent of model/session state."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from reuleauxcoder.domain.jobs import JobSpec


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Invalid job or request ID")
    return value


def atomic_json(path: Path, value):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class LeaseBusy(RuntimeError):
    pass


@contextmanager
def lease(path: Path, *, wait=False):
    """Keep the file: unlinking a locked inode would allow a second owner."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(
                    stream.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1
                )
            except OSError as error:
                raise LeaseBusy("Another process owns this job or workspace") from error
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise LeaseBusy("Another process owns this job or workspace") from error
        locked = True
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def is_locked(path: Path) -> bool:
    try:
        with lease(path):
            return False
    except LeaseBusy:
        return True


class JobStore:
    def __init__(self, root: Path | str | None = None):
        self.root = (
            Path(root or Path.home() / ".rcoder" / "jobs").expanduser().resolve()
        )
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, job_id: str) -> Path:
        return self.root / valid_id(job_id)

    def create(self, spec: JobSpec, job_id: str | None = None) -> tuple[str, bool]:
        job_id = valid_id(job_id or uuid4().hex)
        path = self.path(job_id)
        with lease(self.root / ".create.lock", wait=True):
            if path.exists():
                if read_json(path / "spec.json") != spec.to_dict():
                    raise ValueError(
                        "Job ID already belongs to different task instructions"
                    )
                return job_id, False
            staging = self.root / f".creating-{uuid4().hex}"
            staging.mkdir(mode=0o700)
            try:
                for name in ("requests", "answers", "attempts"):
                    (staging / name).mkdir(mode=0o700)
                atomic_json(staging / "spec.json", spec.to_dict())
                atomic_json(
                    staging / "state.json",
                    {
                        "version": 1,
                        "job_id": job_id,
                        "workspace": spec.workspace,
                        "status": "queued",
                        "session_id": None,
                        "attempt_id": None,
                        "created_at": time.time(),
                        "updated_at": time.time(),
                        "seconds_used": 0,
                        "token_budget": spec.token_budget,
                        "max_seconds": spec.max_seconds,
                    },
                )
                os.replace(staging, path)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return job_id, True

    def spec(self, job_id: str) -> JobSpec:
        return JobSpec.from_dict(read_json(self.path(job_id) / "spec.json"))

    def status(self, job_id):
        path = self.path(job_id)
        state = read_json(path / "state.json")
        state["alive"] = is_locked(path / "run.lock")
        if not state["alive"] and state["status"] in {
            "starting",
            "running",
            "waiting_input",
            "verifying",
            "stopping",
        }:
            state["status"] = "interrupted"
        state["pending_requests"] = []
        if state["alive"]:
            for item in sorted((path / "requests").glob("*.json")):
                try:
                    request = read_json(item)
                except FileNotFoundError:
                    continue
                if request.get("attempt_id") == state["attempt_id"]:
                    state["pending_requests"].append(request)
        return state

    def control(self, job_id, action):
        if action not in {"pause", "cancel"}:
            raise ValueError("Unknown job control")
        with lease(self.path(job_id) / "control.lock", wait=True):
            state = self.status(job_id)
            if not state["alive"] or state["status"] not in {
                "starting",
                "running",
                "waiting_input",
                "verifying",
                "stopping",
            }:
                raise ValueError("Job is not running")
            control = self.path(job_id) / "control.json"
            if control.exists():
                previous = read_json(control)
                if (
                    previous.get("attempt_id") == state["attempt_id"]
                    and previous["action"] == "cancel"
                ):
                    action = "cancel"
            atomic_json(control, {"attempt_id": state["attempt_id"], "action": action})
        return {"job_id": job_id, "requested": action}

    def answer(self, job_id, request_id, response):
        path = self.path(job_id)
        with lease(path / "control.lock", wait=True):
            request = read_json(path / "requests" / f"{valid_id(request_id)}.json")
            state = self.status(job_id)
            if (
                not state["alive"]
                or state["status"] not in {"running", "waiting_input"}
                or request["attempt_id"] != state["attempt_id"]
            ):
                raise ValueError("This interaction is no longer active")
            control = path / "control.json"
            if (
                control.exists()
                and read_json(control).get("attempt_id") == state["attempt_id"]
            ):
                raise ValueError("This attempt is stopping; it cannot accept an answer")
            target = path / "answers" / f"{request_id}.json"
            value = {"attempt_id": state["attempt_id"], "response": response}
            if target.exists() and read_json(target) != value:
                raise ValueError("An answer has already been submitted")
            atomic_json(target, value)


class JobJournal:
    """One process owns state; a lock serializes its event/interaction threads."""

    def __init__(self, store: JobStore, job_id: str):
        self.path = store.path(job_id)
        self.state = read_json(self.path / "state.json")
        self._lock = threading.RLock()
        self.sequence = self.state.get("sequence", 0)
        events = self.path / "events.jsonl"
        if events.exists():
            # A crash can leave an incomplete tail. Preserve every full event
            # and resume strictly after its cursor, including after stale state.
            with events.open("r+b") as stream:
                while True:
                    start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        if stream.read():
                            raise ValueError(
                                "Job event log is corrupt before its final record"
                            )
                        stream.seek(start)
                        stream.truncate()
                        break
                    self.sequence = max(self.sequence, event["seq"])
                    if not line.endswith(b"\n"):
                        stream.write(b"\n")

    def update(self, **changes):
        with self._lock:
            self.state.update(changes, updated_at=time.time(), sequence=self.sequence)
            atomic_json(self.path / "state.json", self.state)

    def event(self, kind, data):
        with self._lock:
            self.sequence += 1
            record = {
                "seq": self.sequence,
                "at": time.time(),
                "kind": kind,
                "attempt_id": self.state.get("attempt_id"),
                "data": data,
            }
            descriptor = os.open(
                self.path / "events.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
            return record
