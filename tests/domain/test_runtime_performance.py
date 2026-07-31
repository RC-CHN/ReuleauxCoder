from __future__ import annotations

import pytest

from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor


def test_monitor_retains_a_bounded_chronological_window() -> None:
    monitor = RuntimePerformanceMonitor(capacity=16)

    for index in range(20):
        monitor.record("test", f"sample-{index}", index)

    samples = monitor.snapshot()
    assert len(samples) == 16
    assert samples[0].name == "sample-4"
    assert samples[-1].name == "sample-19"
    assert monitor.dropped == 4
    assert monitor.snapshot(limit=0) == ()


def test_measure_records_failure_without_swallowing_it() -> None:
    monitor = RuntimePerformanceMonitor()

    with pytest.raises(RuntimeError, match="boom"):
        with monitor.measure("test", "failing", attributes={"safe": True}):
            raise RuntimeError("boom")

    sample = monitor.snapshot()[-1]
    assert sample.status == "error"
    assert sample.name == "failing"
    assert sample.attribute_map() == {"safe": True}
