"""Single-flight background prewarming for transcript resize layouts."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable, Mapping

from reuleauxcoder.interfaces.tui.formatting import (
    fragments_to_visual_lines,
    wrap_fragments,
)
from reuleauxcoder.interfaces.tui.markdown_fragments import RetainedMarkdownRenderer
from reuleauxcoder.interfaces.tui.transcript import (
    cell_fragments,
    decorate_transcript_fragments,
)
from reuleauxcoder.interfaces.tui.virtual_transcript import VisualCell
from reuleauxcoder.presentation import TranscriptPlacement


VisualKey = tuple[str, int, int, int]
VisualLines = tuple[tuple[tuple[str, str], ...], ...]
SourceKey = tuple[int, int, int, int]
RenderKey = tuple[VisualKey, ...]


@dataclass(frozen=True, slots=True)
class TranscriptCacheStats:
    generation: int
    submitted: int
    completed: int
    stale_rejected: int
    failures: int
    batches: int
    cache_hits: int
    cache_misses: int
    render_rows: int
    workers_started: int
    in_flight: bool
    target_width: int | None


@dataclass(frozen=True, slots=True)
class TranscriptPrewarmObservation:
    generation: int
    width: int
    elapsed_ms: float
    status: str
    cell_count: int
    batches: int
    cache_hits: int
    cache_misses: int
    render_rows: int
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptPrewarmResult:
    generation: int
    source_key: SourceKey
    render_key: RenderKey
    visual_cells: tuple[VisualCell, ...]
    cache_entries: Mapping[VisualKey, VisualLines]


@dataclass(frozen=True, slots=True)
class _Request:
    generation: int
    source_key: SourceKey
    render_key: RenderKey
    placements: tuple[TranscriptPlacement, ...]
    cached_lines: Mapping[VisualKey, VisualLines]

    @property
    def width(self) -> int:
        return self.source_key[2]

    @property
    def theme_revision(self) -> int:
        return self.source_key[3]


@dataclass(slots=True)
class _WorkMetrics:
    started: float
    batches: int = 0
    hits: int = 0
    misses: int = 0
    rows: int = 0


def visual_cell_key(
    placement: TranscriptPlacement,
    *,
    width: int,
    theme_revision: int,
) -> VisualKey:
    cell = placement.cell
    decoration_revision = (
        cell.revision * 8
        + int(placement.begins_turn) * 4
        + int(placement.show_assistant_label) * 2
        + placement.blank_lines_after
    )
    return (cell.id, decoration_revision, width, theme_revision)


def render_visual_cell(
    placement: TranscriptPlacement,
    *,
    width: int,
    theme_revision: int,
    markdown_renderer: RetainedMarkdownRenderer,
) -> VisualCell:
    key = visual_cell_key(
        placement,
        width=width,
        theme_revision=theme_revision,
    )
    lines = fragments_to_visual_lines(
        wrap_fragments(
            decorate_transcript_fragments(
                placement,
                cell_fragments(
                    placement.cell,
                    width=width,
                    markdown_renderer=markdown_renderer,
                ),
            ),
            width=max(1, width),
        )
    )
    return VisualCell(key=key, lines=lines)


class TranscriptLayoutPrewarmer:
    """Run at most one resize worker and discard superseded generations."""

    def __init__(
        self,
        *,
        on_complete: Callable[[TranscriptPrewarmResult], bool],
        on_observation: Callable[[TranscriptPrewarmObservation], None],
        on_failure: Callable[[BaseException], None],
        batch_size: int = 32,
    ) -> None:
        self._on_complete = on_complete
        self._on_observation = on_observation
        self._on_failure = on_failure
        self._batch_size = max(1, int(batch_size))
        self._condition = threading.Condition()
        self._pending: _Request | None = None
        self._worker: threading.Thread | None = None
        self._generation = 0
        self._submitted = 0
        self._completed = 0
        self._stale_rejected = 0
        self._failures = 0
        self._batches = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._render_rows = 0
        self._workers_started = 0
        self._target_width: int | None = None

    def request(
        self,
        *,
        source_key: SourceKey,
        render_key: RenderKey,
        placements: tuple[TranscriptPlacement, ...],
        cached_lines: Mapping[VisualKey, VisualLines],
    ) -> int:
        start_error: BaseException | None = None
        with self._condition:
            self._generation += 1
            generation = self._generation
            self._submitted += 1
            self._pending = _Request(
                generation=generation,
                source_key=source_key,
                render_key=render_key,
                placements=placements,
                cached_lines=dict(cached_lines),
            )
            self._target_width = source_key[2]
            if self._worker is None:
                worker = threading.Thread(
                    target=self._run,
                    name="rcoder-transcript-prewarm",
                    daemon=True,
                )
                self._worker = worker
                self._workers_started += 1
                try:
                    worker.start()
                except BaseException as error:
                    self._worker = None
                    self._pending = None
                    self._target_width = None
                    self._failures += 1
                    start_error = error
                    self._condition.notify_all()
        if start_error is not None:
            self._safe_failure(start_error)
        return generation

    def cancel(self) -> None:
        with self._condition:
            if self._pending is None and self._target_width is None:
                return
            self._generation += 1
            self._pending = None
            self._target_width = None
            self._condition.notify_all()

    def snapshot(self) -> TranscriptCacheStats:
        with self._condition:
            return TranscriptCacheStats(
                generation=self._generation,
                submitted=self._submitted,
                completed=self._completed,
                stale_rejected=self._stale_rejected,
                failures=self._failures,
                batches=self._batches,
                cache_hits=self._cache_hits,
                cache_misses=self._cache_misses,
                render_rows=self._render_rows,
                workers_started=self._workers_started,
                in_flight=self._worker is not None,
                target_width=self._target_width,
            )

    def wait_idle(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._worker is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                request = self._pending
                self._pending = None
                if request is None:
                    self._worker = None
                    self._target_width = None
                    self._condition.notify_all()
                    return
            self._execute(request)

    def _execute(self, request: _Request) -> None:
        metrics = _WorkMetrics(started=time.monotonic())
        entries: dict[VisualKey, VisualLines] = {}
        cells: list[VisualCell] = []
        renderer = RetainedMarkdownRenderer()
        try:
            for offset in range(0, len(request.placements), self._batch_size):
                if not self._is_current(request.generation):
                    self._finish(request, metrics, "stale")
                    return
                for placement in request.placements[
                    offset : offset + self._batch_size
                ]:
                    key = visual_cell_key(
                        placement,
                        width=request.width,
                        theme_revision=request.theme_revision,
                    )
                    lines = request.cached_lines.get(key)
                    if lines is None:
                        visual = render_visual_cell(
                            placement,
                            width=request.width,
                            theme_revision=request.theme_revision,
                            markdown_renderer=renderer,
                        )
                        lines = visual.lines
                        metrics.misses += 1
                    else:
                        visual = VisualCell(key=key, lines=lines)
                        metrics.hits += 1
                    entries[key] = lines
                    cells.append(visual)
                    metrics.rows += len(lines)
                metrics.batches += 1
                time.sleep(0)
        except BaseException as error:
            self._finish(request, metrics, "error", error)
            return

        if not self._is_current(request.generation):
            self._finish(request, metrics, "stale")
            return
        try:
            accepted = self._on_complete(
                TranscriptPrewarmResult(
                    generation=request.generation,
                    source_key=request.source_key,
                    render_key=request.render_key,
                    visual_cells=tuple(cells),
                    cache_entries=entries,
                )
            )
        except BaseException as error:
            self._finish(request, metrics, "error", error)
            return
        self._finish(request, metrics, "ok" if accepted else "stale")

    def _finish(
        self,
        request: _Request,
        metrics: _WorkMetrics,
        status: str,
        error: BaseException | None = None,
    ) -> None:
        with self._condition:
            if status == "ok":
                self._completed += 1
            elif status == "stale":
                self._stale_rejected += 1
            else:
                self._failures += 1
            self._batches += metrics.batches
            self._cache_hits += metrics.hits
            self._cache_misses += metrics.misses
            self._render_rows += metrics.rows
            if self._generation == request.generation:
                self._target_width = None
            self._condition.notify_all()
        self._safe_observe(
            TranscriptPrewarmObservation(
                generation=request.generation,
                width=request.width,
                elapsed_ms=(time.monotonic() - metrics.started) * 1_000,
                status=status,
                cell_count=len(request.placements),
                batches=metrics.batches,
                cache_hits=metrics.hits,
                cache_misses=metrics.misses,
                render_rows=metrics.rows,
                error_type=_safe_error_type(error) if error is not None else None,
            )
        )
        if error is not None:
            self._safe_failure(error)

    def _is_current(self, generation: int) -> bool:
        with self._condition:
            return generation == self._generation

    def _safe_observe(self, observation: TranscriptPrewarmObservation) -> None:
        try:
            self._on_observation(observation)
        except BaseException as error:
            self._safe_failure(error)

    def _safe_failure(self, error: BaseException) -> None:
        try:
            self._on_failure(error)
        except BaseException:
            return


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name and len(name) <= 64 and name.isascii() and name.replace("_", "").isalnum():
        return name
    return "Exception"
