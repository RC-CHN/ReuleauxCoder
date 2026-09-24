"""Single writer boundary for live and explicit session snapshots."""

from contextlib import contextmanager, nullcontext
import threading

_LIVE_SNAPSHOT_DELAY_SECONDS = 0.15


class SessionSnapshotWriter:
    """Coalesce full snapshots while the append-only ledger stays durable."""

    def __init__(
        self,
        persist,
        *,
        incident_sink=None,
        stop_sink=None,
        delay: float = _LIVE_SNAPSHOT_DELAY_SECONDS,
        context_lock=None,
    ):
        self._persist = persist
        self._incident_sink = incident_sink
        self._stop_sink = stop_sink
        self._delay = max(0.0, delay)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        # Context mutations may synchronously persist (including first-message
        # discovery and compression usage). Always take context before writer.
        self._context_lock = context_lock if context_lock is not None else nullcontext()
        self._timer: threading.Timer | None = None
        self._closed = False
        self._generation = 0
        self._has_snapshot = False

    def __call__(self, *, deferred: bool = False) -> None:
        # A new session needs a discoverable manifest before its first reply.
        if not deferred or not self._has_snapshot:
            self.flush()
            return
        scheduling_error: BaseException | None = None
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            generation = self._generation
            if self._timer is not None:
                try:
                    self._timer.cancel()
                except BaseException as error:
                    scheduling_error = error
            try:
                timer = threading.Timer(
                    self._delay,
                    self._run_deferred,
                    args=(generation,),
                )
                timer.daemon = True
                self._timer = timer
                timer.start()
            except BaseException as error:
                self._timer = None
                scheduling_error = error
        if scheduling_error is not None:
            # Scheduling observes an already committed ledger/message mutation.
            self._record_failure(scheduling_error)

    def _run_deferred(self, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._generation:
                return
            self._timer = None
        with self._context_lock, self._write_lock:
            with self._lock:
                if self._closed or generation != self._generation:
                    return
            try:
                self._persist()
                self._has_snapshot = True
            except BaseException as error:
                # The event itself was fsync'd before this best-effort snapshot.
                # A later forced flush or restore-tail reconstruction recovers.
                self._record_failure(error)
                return

    def _record_failure(self, error: BaseException) -> None:
        if isinstance(
            error, (KeyboardInterrupt, SystemExit, GeneratorExit)
        ) and callable(self._stop_sink):
            try:
                self._stop_sink()
            except BaseException:
                pass
        if not callable(self._incident_sink):
            return
        try:
            self._incident_sink(
                "session_snapshot",
                _safe_persistence_error_type(error),
                "session_persistence",
            )
        except BaseException:
            # This is the final non-recursive diagnostic boundary.
            pass

    def flush(self) -> None:
        with self.write_scope() as writable:
            if not writable:
                return
            try:
                self._persist()
                self._has_snapshot = True
            except BaseException as error:
                self._record_failure(error)
                raise

    @contextmanager
    def write_scope(self):
        """Serialize an explicit save with timers using context-before-writer."""
        with self._lock:
            self._generation += 1
            timer, self._timer = self._timer, None
            if timer is not None:
                timer.cancel()
        with self._context_lock, self._write_lock:
            yield not self._closed

    def close(self) -> None:
        with self.write_scope() as writable:
            if not writable:
                return
            try:
                self._persist()
                self._has_snapshot = True
            except BaseException as error:
                self._record_failure(error)
                raise
            with self._lock:
                self._closed = True


def _safe_persistence_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name and len(name) <= 64 and name.isascii() and name.replace("_", "").isalnum():
        return name
    return "Exception"
