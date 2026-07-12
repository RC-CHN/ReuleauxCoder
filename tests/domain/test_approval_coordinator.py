import threading

from reuleauxcoder.domain.approval import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalRequest,
)


def test_concurrent_requests_register_but_only_head_gets_ui_focus() -> None:
    presented = []
    presented_event = threading.Event()

    def handler(pending) -> None:
        presented.append(pending)
        presented_event.set()

    coordinator = ApprovalCoordinator(handler, timeout=2)
    decisions = []

    first = threading.Thread(
        target=lambda: decisions.append(
            coordinator.request_approval(ApprovalRequest(tool_name="first"))
        )
    )
    second = threading.Thread(
        target=lambda: decisions.append(
            coordinator.request_approval(ApprovalRequest(tool_name="second"))
        )
    )
    first.start()
    assert presented_event.wait(1)
    second.start()

    for _ in range(100):
        if coordinator.pending_count == 2:
            break
        threading.Event().wait(0.005)
    assert coordinator.pending_count == 2
    assert [item.request.tool_name for item in presented] == ["first"]

    presented[0].resolve(ApprovalDecision.allow_once())
    first.join(1)
    for _ in range(100):
        if len(presented) == 2:
            break
        threading.Event().wait(0.005)
    assert [item.request.tool_name for item in presented] == ["first", "second"]
    presented[1].resolve(ApprovalDecision.deny_once())
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(decision.approved for decision in decisions) == [False, True]
    assert coordinator.pending_count == 0


def test_handler_failure_denies_and_promotes_next_request() -> None:
    calls = []

    def handler(pending) -> None:
        calls.append(pending.request.tool_name)
        if pending.request.tool_name == "broken":
            raise RuntimeError("UI unavailable")
        pending.resolve(ApprovalDecision.allow_once())

    coordinator = ApprovalCoordinator(handler)
    denied = coordinator.request_approval(ApprovalRequest(tool_name="broken"))
    allowed = coordinator.request_approval(ApprovalRequest(tool_name="next"))

    assert denied.approved is False
    assert "failed closed" in (denied.reason or "")
    assert allowed.approved is True
    assert calls == ["broken", "next"]
