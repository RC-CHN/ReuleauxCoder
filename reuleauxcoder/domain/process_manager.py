"""Session-level ownership and observation for process ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import concurrent.futures
from dataclasses import dataclass, field, replace
from enum import Enum
import threading
import time
import uuid

from reuleauxcoder.domain.process import (
    MAX_PROCESS_INPUT_BYTES,
    MAX_PROCESS_SESSION_INPUT_BYTES,
    ProcessCapacityError,
    ProcessCursor,
    ProcessHandle,
    ProcessOperationUnsupported,
    ProcessPort,
    ProcessSessionNotFound,
    ProcessShutdownReport,
    ProcessSnapshot,
    ProcessState,
    ProcessStreamMode,
    ProcessStreamHandler,
)


class ProcessEventKind(str, Enum):
    PUBLISHED = "published"
    OUTPUT = "output"
    UPDATED = "updated"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ProcessEvent:
    kind: ProcessEventKind
    snapshot: ProcessSnapshot
    command: str
    cwd: str
    owner_agent_id: str
    owner_session_id: str | None
    session_generation: int
    origin_turn_id: str | None


ProcessEventSink = Callable[[ProcessEvent], None]
_HIDDEN_INPUT_MARKER = "[hidden input redacted]"


@dataclass(frozen=True, slots=True)
class ManagedProcessView:
    session_id: str
    command: str
    cwd: str
    state: ProcessState
    stream_mode: str
    backend: str
    elapsed_seconds: float
    exit_code: int | None
    termination_reason: str | None
    output_truncated: bool
    output_decode_replaced: bool
    published: bool
    observed: bool


@dataclass(slots=True)
class _ManagedEntry:
    handle: ProcessHandle
    port: ProcessPort
    command: str
    cwd: str
    owner_agent_id: str
    owner_session_id: str | None
    session_generation: int
    origin_turn_id: str | None
    created_monotonic: float
    last_snapshot: ProcessSnapshot
    published: bool = False
    observed: bool = False
    abandoned: bool = False
    observed_at: float | None = None
    terminal_at: float | None = None
    cursors: dict[str, ProcessCursor] = field(default_factory=dict)
    consumer_locks: dict[str, threading.Lock] = field(default_factory=dict)
    watcher_cursor: ProcessCursor = field(default_factory=ProcessCursor)
    watcher: threading.Thread | None = None
    watcher_started: bool = False
    completion_emitted: bool = False
    input_bytes: int = 0
    input_lock: threading.Lock = field(default_factory=threading.Lock)
    sensitive_values: list[str] = field(default_factory=list)
    output_filters: dict[tuple[str, str], "_SensitiveOutputFilter"] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class _SensitiveOutputFilter:
    """Redact exact hidden values while preserving cross-chunk matches."""

    values: list[str]
    pending: str = ""

    def apply(self, chunk: str, *, final: bool) -> str:
        text = self.pending + chunk
        self.pending = ""
        if not text or not self.values:
            return text

        values = sorted(
            (value for value in self.values if value),
            key=len,
            reverse=True,
        )
        output: list[str] = []
        index = 0
        while index < len(text):
            matched = next(
                (value for value in values if text.startswith(value, index)),
                None,
            )
            if matched is not None:
                output.append(_HIDDEN_INPUT_MARKER)
                index += len(matched)
                continue
            suffix = text[index:]
            if not final and any(value.startswith(suffix) for value in values):
                self.pending = suffix
                break
            output.append(text[index])
            index += 1
        return "".join(output)


class ProcessManager:
    """Own process sessions across tool calls and user turns."""

    def __init__(
        self,
        *,
        event_sink: ProcessEventSink | None = None,
        max_sessions: int = 32,
        observed_retention_seconds: float = 30.0,
        terminal_ttl_seconds: float = 600.0,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        if observed_retention_seconds < 0 or terminal_ttl_seconds < 0:
            raise ValueError("retention periods must be non-negative")
        self._event_sink = event_sink
        self._max_sessions = max_sessions
        self._observed_retention_seconds = observed_retention_seconds
        self._terminal_ttl_seconds = terminal_ttl_seconds
        self._entries: dict[str, _ManagedEntry] = {}
        self._lock = threading.RLock()
        self._start_condition = threading.Condition(self._lock)
        self._ports: dict[int, ProcessPort] = {}
        self._closing = False
        self._starting = 0

    def start(
        self,
        port: ProcessPort,
        command: str,
        *,
        cwd: str,
        runtime_timeout: int,
        tty: bool,
        owner_agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
        origin_turn_id: str | None,
        env: Mapping[str, str] | None = None,
        stream_handler: ProcessStreamHandler | None = None,
    ) -> ProcessHandle:
        with self._lock:
            if self._closing:
                raise RuntimeError("process manager is shutting down")
            self._cleanup_expired_locked()
            if len(self._entries) + self._starting >= self._max_sessions:
                raise ProcessCapacityError(
                    "process session capacity reached "
                    f"({self._max_sessions} retained/running sessions)"
                )
            self._ports[id(port)] = port
            self._starting += 1

        idempotency_key = f"rcoder-{uuid.uuid4().hex}"
        handle: ProcessHandle | None = None
        registered = False
        try:
            handle = port.start(
                command,
                cwd=cwd,
                runtime_timeout=runtime_timeout,
                tty=tty,
                env=env,
                idempotency_key=idempotency_key,
                stream_handler=stream_handler,
            )
            initial = port.poll(handle.session_id)
            entry = _ManagedEntry(
                handle=handle,
                port=port,
                command=command,
                cwd=cwd,
                owner_agent_id=owner_agent_id,
                owner_session_id=owner_session_id,
                session_generation=session_generation,
                origin_turn_id=origin_turn_id,
                created_monotonic=time.monotonic(),
                last_snapshot=initial,
            )
            with self._lock:
                if self._closing:
                    raise RuntimeError("process manager is shutting down")
                if handle.session_id in self._entries:
                    raise RuntimeError(
                        f"duplicate process session id '{handle.session_id}'"
                    )
                self._entries[handle.session_id] = entry
                registered = True

            watcher = threading.Thread(
                target=self._watch,
                args=(entry,),
                name=f"rcoder-managed-process-{handle.session_id[-8:]}",
                daemon=True,
            )
            entry.watcher = watcher
            return handle
        except BaseException:
            if handle is not None:
                if registered:
                    with self._lock:
                        self._entries.pop(handle.session_id, None)
                    registered = False
                try:
                    port.terminate(
                        handle.session_id,
                        reason=("shutdown" if self._closing else "start_failed"),
                    )
                except Exception:
                    pass
            raise
        finally:
            with self._start_condition:
                self._starting -= 1
                self._start_condition.notify_all()

    def publish(self, session_id: str, *, observed: bool = False) -> None:
        emit_completion = False
        with self._lock:
            entry = self._require_entry_locked(session_id)
            entry.published = True
            self._ensure_watcher_started_locked(entry)
            if observed and entry.last_snapshot.state is ProcessState.EXITED:
                entry.observed = True
                entry.observed_at = time.monotonic()
            if (
                entry.last_snapshot.state is ProcessState.EXITED
                and not entry.completion_emitted
            ):
                entry.completion_emitted = True
                emit_completion = True
        self._emit(ProcessEventKind.PUBLISHED, entry, entry.last_snapshot)
        if emit_completion:
            self._emit(ProcessEventKind.COMPLETED, entry, entry.last_snapshot)

    def abandon(self, session_id: str, *, reason: str = "cancelled") -> None:
        with self._lock:
            entry = self._require_entry_locked(session_id)
            if entry.published:
                return
            entry.abandoned = True
            terminal = entry.last_snapshot.state is ProcessState.EXITED
            if terminal:
                self._entries.pop(session_id, None)
            else:
                self._ensure_watcher_started_locked(entry)
        if terminal:
            try:
                entry.port.release(session_id)
            except Exception:
                pass
            return
        try:
            entry.port.terminate(session_id, reason=reason)
        except ProcessSessionNotFound:
            pass

    def poll(
        self,
        session_id: str,
        *,
        consumer: str,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
        wait_ms: int = 0,
        mark_observed: bool = True,
    ) -> ProcessSnapshot:
        entry, cursor, consumer_lock = self._entry_and_cursor(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
        )
        with consumer_lock:
            with self._lock:
                current = self._entries.get(session_id)
                if current is not entry:
                    raise ProcessSessionNotFound(
                        f"process session '{session_id}' was not found"
                    )
                cursor = entry.cursors.get(consumer, cursor)
            snapshot = entry.port.poll(
                session_id,
                cursor=cursor,
                wait_ms=wait_ms,
            )
            with self._lock:
                current = self._entries.get(session_id)
                if current is not entry:
                    raise ProcessSessionNotFound(
                        f"process session '{session_id}' was not found"
                    )
                snapshot = self._redact_snapshot_locked(
                    entry,
                    consumer,
                    snapshot,
                )
                entry.cursors[consumer] = snapshot.cursor
                entry.last_snapshot = snapshot
                if snapshot.state is ProcessState.EXITED:
                    entry.terminal_at = entry.terminal_at or time.monotonic()
                    if mark_observed:
                        entry.observed = True
                        entry.observed_at = time.monotonic()
        return snapshot

    def write(
        self,
        session_id: str,
        chars: str,
        *,
        consumer: str,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
    ) -> ProcessSnapshot:
        entry, _, _ = self._entry_and_cursor(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
        )
        with entry.input_lock:
            self._write_input_bounded(entry, chars)
        return self.poll(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
            wait_ms=0,
        )

    def write_sensitive_line(
        self,
        session_id: str,
        value: str,
        *,
        consumer: str,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
    ) -> ProcessSnapshot:
        """Write one hidden TTY line without exposing its value in observations."""
        if not value:
            raise ValueError("hidden input cannot be empty")
        if "\n" in value or "\r" in value:
            raise ValueError("hidden input must be one line")
        entry, _, _ = self._entry_and_cursor(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
        )
        with entry.input_lock:
            byte_count = self._validated_input_byte_count(entry, value + "\n")
            with self._lock:
                if entry.handle.stream_mode is not ProcessStreamMode.PTY:
                    raise ProcessOperationUnsupported(
                        f"session '{session_id}' uses pipe mode; "
                        "hidden input was not sent"
                    )
                if entry.last_snapshot.state is not ProcessState.RUNNING:
                    raise ProcessOperationUnsupported(
                        f"session '{session_id}' is "
                        f"{entry.last_snapshot.state.value}; hidden input was not sent"
                    )
                if value not in entry.sensitive_values:
                    entry.sensitive_values.append(value)
            # Retain the redaction even if the transport reports an ambiguous
            # failure: the peer may have received the write before the error.
            self._write_input_bounded(
                entry,
                value + "\n",
                byte_count=byte_count,
            )
        return self.poll(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
            wait_ms=0,
        )

    def _validated_input_byte_count(
        self,
        entry: _ManagedEntry,
        chars: str,
    ) -> int:
        byte_count = len(chars.encode("utf-8"))
        if byte_count > MAX_PROCESS_INPUT_BYTES:
            raise ProcessCapacityError(
                "process input exceeds the 64 KiB per-write limit; no input was sent"
            )
        with self._lock:
            if entry.input_bytes + byte_count > MAX_PROCESS_SESSION_INPUT_BYTES:
                raise ProcessCapacityError(
                    "process input exceeds the 1 MiB session limit; no input was sent"
                )
        return byte_count

    def _write_input_bounded(
        self,
        entry: _ManagedEntry,
        chars: str,
        *,
        byte_count: int | None = None,
    ) -> None:
        byte_count = (
            self._validated_input_byte_count(entry, chars)
            if byte_count is None
            else byte_count
        )
        confirmed = entry.port.write_input(entry.handle.session_id, chars)
        with self._lock:
            entry.input_bytes += max(0, confirmed)
        if confirmed != byte_count:
            raise RuntimeError(
                f"process input write confirmed {confirmed} of {byte_count} bytes"
            )

    def interrupt(
        self,
        session_id: str,
        *,
        consumer: str,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
    ) -> ProcessSnapshot:
        entry, _, _ = self._entry_and_cursor(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
        )
        entry.port.interrupt(session_id)
        return self.poll(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
            wait_ms=0,
        )

    def terminate(
        self,
        session_id: str,
        *,
        consumer: str,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
        reason: str = "terminated",
    ) -> ProcessSnapshot:
        entry, _, _ = self._entry_and_cursor(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
        )
        entry.port.terminate(session_id, reason=reason)
        return self.poll(
            session_id,
            consumer=consumer,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=session_generation,
            wait_ms=0,
        )

    def get_view(
        self,
        session_id: str,
        *,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
    ) -> ManagedProcessView:
        """Return non-consuming process facts for validation and UI."""
        with self._lock:
            entry = self._require_entry_locked(session_id)
            if not entry.published or not self._can_access(
                entry,
                agent_id=agent_id,
                owner_session_id=owner_session_id,
                session_generation=session_generation,
            ):
                raise ProcessSessionNotFound(
                    f"process session '{session_id}' is not available in this session"
                )
            return self._view(entry)

    def list(
        self,
        *,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
        include_observed: bool = False,
    ) -> tuple[ManagedProcessView, ...]:
        with self._lock:
            self._cleanup_expired_locked()
            entries = [
                entry
                for entry in self._entries.values()
                if self._can_access(
                    entry,
                    agent_id=agent_id,
                    owner_session_id=owner_session_id,
                    session_generation=session_generation,
                )
                and (include_observed or not entry.observed)
                and entry.published
            ]
            entries.sort(key=lambda item: item.created_monotonic)
            return tuple(self._view(entry) for entry in entries)

    def active_count(
        self,
        *,
        owner_session_id: str | None = None,
    ) -> int:
        with self._lock:
            return sum(
                entry.last_snapshot.state is not ProcessState.EXITED
                and (
                    owner_session_id is None
                    or entry.owner_session_id == owner_session_id
                )
                for entry in self._entries.values()
            )

    def resize_tty_sessions(
        self,
        *,
        rows: int,
        columns: int,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
    ) -> int:
        """Synchronize visible live PTYs without adding a model-facing action."""
        if rows < 1 or columns < 1:
            raise ValueError("terminal rows and columns must be positive")
        with self._lock:
            entries = [
                entry
                for entry in self._entries.values()
                if entry.published
                and entry.handle.stream_mode is ProcessStreamMode.PTY
                and entry.last_snapshot.state is ProcessState.RUNNING
                and self._can_access(
                    entry,
                    agent_id=agent_id,
                    owner_session_id=owner_session_id,
                    session_generation=session_generation,
                )
            ]
        resized = 0
        for entry in entries:
            try:
                entry.port.resize(
                    entry.handle.session_id,
                    rows=rows,
                    columns=columns,
                )
            except Exception:
                # Resize is a best-effort UI synchronization. A concurrent
                # process exit or one backend failure must not block rendering
                # or prevent the remaining PTYs from being updated.
                continue
            resized += 1
        return resized

    def rebind_generation(
        self,
        *,
        owner_session_id: str | None,
        previous_generation: int,
        next_generation: int,
    ) -> int:
        rebound = 0
        with self._lock:
            for entry in self._entries.values():
                if (
                    entry.owner_session_id == owner_session_id
                    and entry.session_generation == previous_generation
                    and entry.published
                ):
                    entry.session_generation = next_generation
                    rebound += 1
        return rebound

    def stop_all(
        self,
        *,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
        reason: str = "terminated",
    ) -> int:
        with self._lock:
            entries = [
                entry
                for entry in self._entries.values()
                if self._can_access(
                    entry,
                    agent_id=agent_id,
                    owner_session_id=owner_session_id,
                    session_generation=session_generation,
                )
                and entry.last_snapshot.state is not ProcessState.EXITED
            ]

        def terminate_entry(entry: _ManagedEntry) -> None:
            try:
                snapshot = entry.port.terminate(
                    entry.handle.session_id,
                    reason=reason,
                )
                with self._lock:
                    if self._entries.get(entry.handle.session_id) is entry:
                        snapshot = self._redact_snapshot_locked(
                            entry,
                            "manager",
                            snapshot,
                        )
                        entry.last_snapshot = snapshot
                        if snapshot.state is ProcessState.EXITED:
                            entry.terminal_at = (
                                entry.terminal_at or time.monotonic()
                            )
            except Exception:
                return

        if entries:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self._max_sessions, len(entries))
            ) as pool:
                tuple(pool.map(terminate_entry, entries))
        return len(entries)

    def shutdown(self, *, grace_seconds: float = 0.5) -> ProcessShutdownReport:
        start_reap_timeouts = 0
        with self._start_condition:
            self._closing = True
            start_deadline = time.monotonic() + 2.0
            while self._starting:
                remaining = start_deadline - time.monotonic()
                if remaining <= 0:
                    start_reap_timeouts = self._starting
                    break
                self._start_condition.wait(timeout=remaining)
            entries = tuple(self._entries.values())
            ports = tuple(self._ports.values())
            for entry in entries:
                self._ensure_watcher_started_locked(entry)
        terminal = [
            entry
            for entry in entries
            if entry.last_snapshot.state is ProcessState.EXITED
        ]
        live = [
            entry
            for entry in entries
            if entry.last_snapshot.state is ProcessState.RUNNING
        ]
        unknown = [
            entry
            for entry in entries
            if entry.last_snapshot.state is ProcessState.UNKNOWN
        ]
        def interrupt_entry(entry: _ManagedEntry) -> None:
            try:
                entry.port.interrupt(entry.handle.session_id)
            except Exception:
                return

        if live:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self._max_sessions, len(live))
            ) as pool:
                tuple(pool.map(interrupt_entry, live))
        deadline = time.monotonic() + max(0.0, grace_seconds)
        for entry in live:
            watcher = entry.watcher
            if watcher is not None:
                watcher.join(timeout=max(0.0, deadline - time.monotonic()))
        remaining = [
            entry
            for entry in live
            if entry.last_snapshot.state is ProcessState.RUNNING
        ]
        force_targets = [*remaining, *unknown]

        def terminate_entry(entry: _ManagedEntry) -> None:
            try:
                entry.port.terminate(entry.handle.session_id, reason="shutdown")
            except Exception:
                return

        if force_targets:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(self._max_sessions, len(force_targets))
            ) as pool:
                tuple(pool.map(terminate_entry, force_targets))
        reap_timeouts = start_reap_timeouts
        reap_deadline = time.monotonic() + 2.0
        for entry in force_targets:
            watcher = entry.watcher
            if watcher is not None:
                watcher.join(
                    timeout=max(0.0, reap_deadline - time.monotonic())
                )
                if watcher.is_alive():
                    reap_timeouts += 1
        with self._lock:
            for entry in tuple(self._entries.values()):
                if entry.last_snapshot.state is ProcessState.EXITED:
                    try:
                        entry.port.release(entry.handle.session_id)
                    except Exception:
                        pass
            self._entries.clear()
            self._ports.clear()
        for port in ports:
            try:
                report = port.shutdown(grace_seconds=0)
                reap_timeouts += report.reap_timeouts
            except Exception:
                reap_timeouts += 1
        return ProcessShutdownReport(
            total=len(entries),
            already_exited=len(terminal),
            interrupted=len(live),
            terminated=len(force_targets),
            unknown=len(unknown),
            reap_timeouts=reap_timeouts,
        )

    def _watch(self, entry: _ManagedEntry) -> None:
        cursor = ProcessCursor()
        while True:
            try:
                raw_snapshot = entry.port.poll(
                    entry.handle.session_id,
                    cursor=cursor,
                    wait_ms=250,
                )
            except ProcessSessionNotFound:
                return
            cursor = raw_snapshot.cursor
            with self._lock:
                current = self._entries.get(entry.handle.session_id)
                if current is not entry:
                    return
                snapshot = self._redact_snapshot_locked(
                    entry,
                    "watcher",
                    raw_snapshot,
                )
                has_output = bool(snapshot.stdout or snapshot.stderr)
                previous_state = entry.last_snapshot.state
                entry.watcher_cursor = cursor
                entry.last_snapshot = snapshot
                if snapshot.state is ProcessState.EXITED:
                    entry.terminal_at = entry.terminal_at or time.monotonic()
                published = entry.published
                abandoned = entry.abandoned
                emit_completion = (
                    published
                    and snapshot.state is ProcessState.EXITED
                    and not entry.completion_emitted
                )
                if emit_completion:
                    entry.completion_emitted = True
                if abandoned and snapshot.state is ProcessState.EXITED:
                    self._entries.pop(entry.handle.session_id, None)
            if abandoned and snapshot.state is ProcessState.EXITED:
                try:
                    entry.port.release(entry.handle.session_id)
                except Exception:
                    pass
                return
            if has_output and published:
                self._emit(ProcessEventKind.OUTPUT, entry, snapshot)
            elif published and snapshot.state is not previous_state:
                self._emit(ProcessEventKind.UPDATED, entry, snapshot)
            if emit_completion:
                self._emit(ProcessEventKind.COMPLETED, entry, snapshot)
                return
            if snapshot.state is ProcessState.EXITED:
                return

    def _emit(
        self,
        kind: ProcessEventKind,
        entry: _ManagedEntry,
        snapshot: ProcessSnapshot,
    ) -> None:
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(
                ProcessEvent(
                    kind=kind,
                    snapshot=snapshot,
                    command=entry.command,
                    cwd=entry.cwd,
                    owner_agent_id=entry.owner_agent_id,
                    owner_session_id=entry.owner_session_id,
                    session_generation=entry.session_generation,
                    origin_turn_id=entry.origin_turn_id,
                )
            )
        except Exception:
            pass

    @staticmethod
    def _redact_snapshot_locked(
        entry: _ManagedEntry,
        consumer: str,
        snapshot: ProcessSnapshot,
    ) -> ProcessSnapshot:
        if not entry.sensitive_values:
            return snapshot
        final = snapshot.state is ProcessState.EXITED

        def redact(stream: str, value: str) -> str:
            output_filter = entry.output_filters.setdefault(
                (consumer, stream),
                _SensitiveOutputFilter(entry.sensitive_values),
            )
            return output_filter.apply(value, final=final)

        stdout = redact("stdout", snapshot.stdout)
        stderr = redact("stderr", snapshot.stderr)
        if stdout == snapshot.stdout and stderr == snapshot.stderr:
            return snapshot
        return replace(snapshot, stdout=stdout, stderr=stderr)

    @staticmethod
    def _ensure_watcher_started_locked(entry: _ManagedEntry) -> None:
        watcher = entry.watcher
        if watcher is None or entry.watcher_started:
            return
        entry.watcher_started = True
        watcher.start()

    def _entry_and_cursor(
        self,
        session_id: str,
        *,
        consumer: str,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
    ) -> tuple[_ManagedEntry, ProcessCursor, threading.Lock]:
        with self._lock:
            entry = self._require_entry_locked(session_id)
            if not self._can_access(
                entry,
                agent_id=agent_id,
                owner_session_id=owner_session_id,
                session_generation=session_generation,
            ):
                raise ProcessSessionNotFound(
                    f"process session '{session_id}' is not available in this session"
                )
            consumer_lock = entry.consumer_locks.setdefault(
                consumer, threading.Lock()
            )
            return (
                entry,
                entry.cursors.get(consumer, ProcessCursor()),
                consumer_lock,
            )

    def _require_entry_locked(self, session_id: str) -> _ManagedEntry:
        entry = self._entries.get(session_id)
        if entry is None:
            raise ProcessSessionNotFound(
                f"process session '{session_id}' was not found"
            )
        return entry

    @staticmethod
    def _can_access(
        entry: _ManagedEntry,
        *,
        agent_id: str,
        owner_session_id: str | None,
        session_generation: int,
    ) -> bool:
        if entry.session_generation != session_generation:
            return False
        if owner_session_id is not None and entry.owner_session_id == owner_session_id:
            return True
        return entry.owner_agent_id == agent_id

    @staticmethod
    def _view(entry: _ManagedEntry) -> ManagedProcessView:
        snapshot = entry.last_snapshot
        return ManagedProcessView(
            session_id=entry.handle.session_id,
            command=entry.command,
            cwd=entry.cwd,
            state=snapshot.state,
            stream_mode=snapshot.stream_mode.value,
            backend=snapshot.backend,
            elapsed_seconds=snapshot.elapsed_seconds,
            exit_code=snapshot.exit_code,
            termination_reason=snapshot.termination_reason,
            output_truncated=snapshot.output_truncated,
            output_decode_replaced=snapshot.output_decode_replaced,
            published=entry.published,
            observed=entry.observed,
        )

    def _cleanup_expired_locked(self) -> None:
        now = time.monotonic()
        expired: list[_ManagedEntry] = []
        for entry in self._entries.values():
            if entry.last_snapshot.state is not ProcessState.EXITED:
                continue
            terminal_at = entry.terminal_at or entry.created_monotonic
            if entry.observed:
                observed_at = entry.observed_at or terminal_at
                if now - observed_at >= self._observed_retention_seconds:
                    expired.append(entry)
            elif now - terminal_at >= self._terminal_ttl_seconds:
                expired.append(entry)
        for entry in expired:
            self._entries.pop(entry.handle.session_id, None)
            try:
                entry.port.release(entry.handle.session_id)
            except Exception:
                pass
