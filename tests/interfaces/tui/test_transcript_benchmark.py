from reuleauxcoder.interfaces.tui.transcript_benchmark import benchmark_transcript


def test_transcript_benchmark_reports_required_metrics() -> None:
    results = benchmark_transcript(sizes=(100,), iterations=2)

    assert len(results) == 1
    result = results[0]
    assert result["cells"] == 100
    assert result["visual_lines"] >= 100
    assert result["initial_layout_ms"] >= 0
    assert result["scroll_frame_ms"] >= 0
    assert result["resize_schedule_ms"] >= 0
    assert result["resize_ready_ms"] >= result["resize_schedule_ms"]
    assert result["resize_rebuild_ms"] >= 0
    assert result["resize_visual_lines"] >= 100
    assert result["chunk_to_paint_ms"] >= 0
