"""Bounded incremental text retention for process streams."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading


@dataclass(frozen=True, slots=True)
class BufferRead:
    text: str
    next_offset: int
    truncated: bool


@dataclass(slots=True)
class _Segment:
    start: int
    text: str
    byte_count: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)


def _suffix_within_bytes(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text.encode("utf-8", errors="replace")) <= limit:
        return text
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high) // 2
        candidate = text[middle:]
        if len(candidate.encode("utf-8", errors="replace")) <= limit:
            high = middle
        else:
            low = middle + 1
    return text[low:]


def _prefix_within_bytes(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text.encode("utf-8", errors="replace")) <= limit:
        return text
    low = 0
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle]
        if len(candidate.encode("utf-8", errors="replace")) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


class BoundedTextBuffer:
    """Thread-safe tail retention with monotonic character offsets."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._segments: deque[_Segment] = deque()
        self._retained_bytes = 0
        self._next_offset = 0
        self._total_bytes = 0
        self._truncated = False
        self._lock = threading.RLock()

    @property
    def end_offset(self) -> int:
        with self._lock:
            return self._next_offset

    @property
    def retained_start(self) -> int:
        with self._lock:
            if not self._segments:
                return self._next_offset
            return self._segments[0].start

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def truncated(self) -> bool:
        with self._lock:
            return self._truncated

    def append(self, text: str, *, byte_count: int | None = None) -> None:
        measured = (
            len(text.encode("utf-8", errors="replace"))
            if byte_count is None
            else max(0, byte_count)
        )
        with self._lock:
            self._total_bytes += measured
            if not text:
                return
            encoded_bytes = len(text.encode("utf-8", errors="replace"))
            segment = _Segment(
                start=self._next_offset,
                text=text,
                byte_count=encoded_bytes,
            )
            self._next_offset += len(text)
            self._segments.append(segment)
            self._retained_bytes += encoded_bytes
            self._enforce_limit()

    def _enforce_limit(self) -> None:
        while self._segments and self._retained_bytes > self.max_bytes:
            segment = self._segments[0]
            excess = self._retained_bytes - self.max_bytes
            if segment.byte_count <= excess and len(self._segments) > 1:
                removed = self._segments.popleft()
                self._retained_bytes -= removed.byte_count
                self._truncated = True
                continue
            target_bytes = max(0, segment.byte_count - excess)
            retained = _suffix_within_bytes(segment.text, target_bytes)
            dropped_chars = len(segment.text) - len(retained)
            retained_bytes = len(retained.encode("utf-8", errors="replace"))
            segment.start += dropped_chars
            segment.text = retained
            self._retained_bytes -= segment.byte_count - retained_bytes
            segment.byte_count = retained_bytes
            self._truncated = True
            if not retained:
                self._segments.popleft()
            # A multi-byte code point can make the retained total smaller than
            # the byte limit, but never larger.
            break

    def read_after(self, offset: int, *, max_bytes: int) -> BufferRead:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        with self._lock:
            retained_start = (
                self._segments[0].start if self._segments else self._next_offset
            )
            requested = max(0, min(offset, self._next_offset))
            start = max(requested, retained_start)
            pieces: list[str] = []
            for segment in self._segments:
                if segment.end <= start:
                    continue
                local_start = max(0, start - segment.start)
                pieces.append(segment.text[local_start:])
            complete = "".join(pieces)
            selected = _prefix_within_bytes(complete, max_bytes)
            return BufferRead(
                text=selected,
                next_offset=start + len(selected),
                truncated=(
                    requested < retained_start or len(selected) < len(complete)
                ),
            )

    def retained(self) -> BufferRead:
        with self._lock:
            retained_start = (
                self._segments[0].start if self._segments else self._next_offset
            )
            text = "".join(segment.text for segment in self._segments)
            return BufferRead(
                text=text,
                next_offset=self._next_offset,
                truncated=self._truncated or retained_start > 0,
            )
