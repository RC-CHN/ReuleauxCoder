"""Repeatable virtual-transcript layout benchmark used by TUI acceptance."""

from __future__ import annotations

from dataclasses import replace
import time

from prompt_toolkit.data_structures import Point

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.interfaces.tui import MiniTUIEventAdapter
from reuleauxcoder.interfaces.tui.virtual_transcript import VirtualTranscriptControl
from reuleauxcoder.presentation import AssistantCell, DiffCell, ToolCell, UserCell
from reuleauxcoder.presentation.models import ToolCellStatus


def benchmark_transcript(
    *,
    sizes: tuple[int, ...] = (100, 500, 1_000),
    width: int = 100,
    viewport_rows: int = 30,
    iterations: int = 20,
) -> list[dict[str, float | int]]:
    return [
        _benchmark_case(
            size=size,
            width=width,
            viewport_rows=viewport_rows,
            iterations=iterations,
        )
        for size in sizes
    ]


def _benchmark_case(
    *, size: int, width: int, viewport_rows: int, iterations: int
) -> dict[str, float | int]:
    adapter = MiniTUIEventAdapter()
    _populate(adapter, size)

    started = time.perf_counter()
    layout = adapter.transcript_layout(width)
    initial_layout_ms = (time.perf_counter() - started) * 1_000

    cursor = [0]
    control = VirtualTranscriptControl(
        lambda requested_width: adapter.transcript_layout(requested_width),
        lambda: Point(x=0, y=cursor[0]),
    )
    started = time.perf_counter()
    for iteration in range(max(1, iterations)):
        maximum = max(0, layout.line_count - viewport_rows)
        cursor[0] = min(maximum, iteration * 7)
        content = control.create_content(width, viewport_rows)
        for row in range(cursor[0], min(content.line_count, cursor[0] + viewport_rows)):
            content.get_line(row)
    scroll_frame_ms = (time.perf_counter() - started) * 1_000 / max(1, iterations)

    resize_width = max(40, width - 28)
    started = time.perf_counter()
    adapter.transcript_layout_rebased(resize_width, cursor[0])
    resize_schedule_ms = (time.perf_counter() - started) * 1_000
    assert adapter.wait_for_transcript_prewarm(10.0)
    resized_layout = adapter.transcript_layout(resize_width)
    resize_ready_ms = (time.perf_counter() - started) * 1_000

    assistant = next(
        cell
        for cell in reversed(adapter.transcript.state.transcript.cells)
        if isinstance(cell, AssistantCell)
    )
    adapter.transcript.state.transcript.replace(
        replace(
            assistant,
            text=assistant.text + "\nstream 尾部 🚀",
            revision=assistant.revision + 1,
        )
    )
    started = time.perf_counter()
    adapter.transcript_layout(width)
    chunk_to_paint_ms = (time.perf_counter() - started) * 1_000

    return {
        "cells": size,
        "visual_lines": layout.line_count,
        "initial_layout_ms": round(initial_layout_ms, 3),
        "scroll_frame_ms": round(scroll_frame_ms, 3),
        "resize_schedule_ms": round(resize_schedule_ms, 3),
        "resize_ready_ms": round(resize_ready_ms, 3),
        "resize_rebuild_ms": round(resize_ready_ms, 3),
        "resize_visual_lines": resized_layout.line_count,
        "chunk_to_paint_ms": round(chunk_to_paint_ms, 3),
    }


def _populate(adapter: MiniTUIEventAdapter, size: int) -> None:
    transcript = adapter.transcript.state.transcript
    for index in range(size):
        cell_id = f"bench:{index}"
        kind = index % 4
        if kind == 0:
            cell = UserCell(id=cell_id, text=f"用户输入 {index} 🚀")
        elif kind == 1:
            cell = AssistantCell(
                id=cell_id,
                text=(
                    f"**结果 {index}** 中文与 emoji 🧪\n\n"
                    "| 项目 | 状态 |\n| --- | --- |\n| parser | ready |"
                ),
                complete=True,
            )
        elif kind == 2:
            cell = DiffCell(
                id=cell_id,
                path="demo.py",
                diff="--- a/demo.py\n+++ b/demo.py\n-old\n+new 中文 🚀",
            )
        else:
            cell = ToolCell(
                id=cell_id,
                tool_call_id=f"call-{index}",
                name="shell",
                arguments={"command": "echo benchmark"},
                output="one\ntwo\nthree\nfour\nfive",
                status=ToolCellStatus.SUCCEEDED,
                outcome=ToolOutcome(summary="Command completed"),
            )
        transcript.append(cell)
