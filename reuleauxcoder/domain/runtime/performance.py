"""Bounded, credential-free runtime performance observations."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time
from typing import TypeAlias


PerformanceValue: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class PerformanceSample:
    """One completed runtime operation without request or content payloads."""

    sequence: int
    category: str
    name: str
    elapsed_ms: float
    status: str
    observed_at: float
    attributes: tuple[tuple[str, PerformanceValue], ...] = ()

    def attribute_map(self) -> dict[str, PerformanceValue]:
        return dict(self.attributes)


class RuntimePerformanceMonitor:
    """Keep a small in-memory window of completed runtime timings."""

    def __init__(self, *, capacity: int = 256) -> None:
        self.capacity = max(16, int(capacity))
        self._samples: deque[PerformanceSample] = deque(maxlen=self.capacity)
        self._lock = threading.Lock()
        self._sequence = 0
        self._dropped = 0

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def record(
        self,
        category: str,
        name: str,
        elapsed_ms: float,
        *,
        status: str = "ok",
        attributes: Mapping[str, PerformanceValue] | None = None,
    ) -> PerformanceSample:
        """Record one bounded sample, retaining only scalar metadata."""
        normalized_attributes = tuple(
            sorted(
                (
                    str(key),
                    value
                    if isinstance(value, (str, int, float, bool)) or value is None
                    else str(value),
                )
                for key, value in (attributes or {}).items()
            )
        )
        with self._lock:
            if len(self._samples) == self.capacity:
                self._dropped += 1
            self._sequence += 1
            sample = PerformanceSample(
                sequence=self._sequence,
                category=str(category),
                name=str(name),
                elapsed_ms=max(0.0, round(float(elapsed_ms), 3)),
                status=str(status),
                observed_at=time.time(),
                attributes=normalized_attributes,
            )
            self._samples.append(sample)
            return sample

    @contextmanager
    def measure(
        self,
        category: str,
        name: str,
        *,
        attributes: Mapping[str, PerformanceValue] | None = None,
    ) -> Iterator[None]:
        """Measure a synchronous scope and record failures without swallowing them."""
        started = time.monotonic()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            self.record(
                category,
                name,
                (time.monotonic() - started) * 1000,
                status=status,
                attributes=attributes,
            )

    def snapshot(
        self,
        *,
        limit: int | None = None,
        category: str | None = None,
    ) -> tuple[PerformanceSample, ...]:
        """Return retained samples in chronological order."""
        with self._lock:
            samples = tuple(self._samples)
        if category is not None:
            samples = tuple(sample for sample in samples if sample.category == category)
        if limit is not None:
            bounded_limit = max(0, int(limit))
            if bounded_limit == 0:
                return ()
            return samples[-bounded_limit:]
        return samples

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._dropped = 0


__all__ = [
    "PerformanceSample",
    "PerformanceValue",
    "RuntimePerformanceMonitor",
]
