"""Bounded fan-out for process control, independent of blocked input workers."""

from collections.abc import Callable, Sequence
from concurrent.futures import Future
import threading
import time
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_process_controls(
    entries: Sequence[T], operation: Callable[[T], R], *, deadline: float,
) -> tuple[R | None, ...]:
    """Wait within one shared deadline; never join a stuck adapter on exit.

    Each session owns at most one call per phase. Adapters must still unblock
    their I/O when terminated; an unconfirmed result is reported by the caller.
    Daemon workers keep a stuck remote/OS control call from owning shutdown.
    """
    futures: list[Future[R]] = []

    def invoke(entry: T, future: Future[R]) -> None:
        try:
            future.set_result(operation(entry))
        except BaseException as error:
            future.set_exception(error)

    for entry in entries:
        future: Future[R] = Future()
        futures.append(future)
        threading.Thread(
            target=invoke, args=(entry, future),
            name="rcoder-process-control", daemon=True,
        ).start()
    results: list[R | None] = []
    for future in futures:
        try:
            results.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
        except Exception:
            results.append(None)
    return tuple(results)
