from types import SimpleNamespace

from reuleauxcoder.interfaces.entrypoint.runner import AppRunner


def test_startup_progress_failure_is_buffered_then_reported_to_agent() -> None:
    secret = "SENTINEL_STARTUP_PROGRESS"

    def fail_progress(_message: str) -> None:
        raise ValueError(secret)

    runner = AppRunner(startup_progress=fail_progress)
    runner._report_startup("loading")
    runner._report_startup("still loading")
    observed: list[tuple[str, str, str, int]] = []
    agent = SimpleNamespace(
        record_runtime_issue=lambda phase, error_type, ref, count=1: (
            observed.append((phase, error_type, ref, count)) or True
        )
    )

    runner._flush_startup_progress_issues(agent)

    assert observed == [("startup_progress", "ValueError", "callback", 2)]
    assert runner._startup_progress_issues == {}
    assert secret not in repr(runner._startup_progress_issues)


def test_startup_progress_failure_after_agent_attach_is_reported_immediately() -> None:
    observed: list[tuple[str, str, str, int]] = []
    agent = SimpleNamespace(
        record_runtime_issue=lambda phase, error_type, ref, count=1: (
            observed.append((phase, error_type, ref, count)) or True
        )
    )
    runner = AppRunner(
        startup_progress=lambda _message: (_ for _ in ()).throw(OSError("closed"))
    )
    runner._agent = agent

    runner._report_startup("ready")

    assert observed == [("startup_progress", "OSError", "callback", 1)]


def test_startup_progress_issue_stays_buffered_when_agent_sink_fails() -> None:
    runner = AppRunner(startup_progress=lambda _message: None)
    original = ("startup_progress", "ValueError", "callback")
    runner._startup_progress_issues[original] = 3
    agent = SimpleNamespace(
        record_runtime_issue=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("sink unavailable")
        )
    )

    runner._flush_startup_progress_issues(agent)

    assert runner._startup_progress_issues == {
        original: 3,
        ("startup_progress_sink", "RuntimeError", "delivery"): 1,
    }
