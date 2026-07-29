"""Small cooperative-cancellation contracts shared across runtime boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class CancellationSignal(Protocol):
    """The only cancellation capability exposed to consumers."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class CancellationView:
    """A monotonic view over turn stop and an optional round interrupt epoch."""

    stop_signal: CancellationSignal
    epoch_reader: Callable[[], int]
    baseline_epoch: int
    include_round_interrupt: bool = True

    def is_set(self) -> bool:
        if self.stop_signal.is_set():
            return True
        return (
            self.include_round_interrupt
            and self.epoch_reader() > self.baseline_epoch
        )
