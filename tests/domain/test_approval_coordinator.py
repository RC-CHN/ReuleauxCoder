import threading

from reuleauxcoder.domain.approval import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalRequest,
    SharedApprovalProvider,
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


def test_forced_human_review_bypasses_automatic_judges() -> None:
    judge_calls = []
    presented = []

    def judge(request):
        judge_calls.append(request)
        return ApprovalDecision.allow_once("automatic approval")

    def handler(pending) -> None:
        presented.append(pending.request)
        pending.resolve(ApprovalDecision.deny_once("human rejected"))

    provider = SharedApprovalProvider(handler, judges=[judge], reviewer="auto_review")
    request = ApprovalRequest(
        tool_name="write_file",
        metadata={"force_human_review": True},
    )

    decision = provider.request_approval(request)

    assert decision.approved is False
    assert decision.reason == "human rejected"
    assert judge_calls == []
    assert presented == [request]
    assert request.metadata["reviewer"] == "user"


def test_cancel_matching_denies_focused_and_queued_requests() -> None:
    presented = []
    focused = threading.Event()
    release_handler = threading.Event()
    decisions = []

    def handler(pending) -> None:
        presented.append(pending.request.tool_name)
        focused.set()
        release_handler.wait(timeout=2)

    coordinator = ApprovalCoordinator(handler, timeout=2)

    def request(name: str, job_id: str) -> None:
        decisions.append(
            coordinator.request_approval(
                ApprovalRequest(
                    tool_name=name,
                    metadata={"subagent_job_id": job_id},
                )
            )
        )

    first = threading.Thread(target=request, args=("write_file", "sj_cancel"))
    second = threading.Thread(target=request, args=("shell", "sj_cancel"))
    first.start()
    assert focused.wait(timeout=1)
    second.start()
    for _ in range(100):
        if coordinator.pending_count == 2:
            break
        threading.Event().wait(0.005)

    request_ids = coordinator.cancel_matching(
        lambda item: item.metadata.get("subagent_job_id") == "sj_cancel",
        reason="child cancelled",
    )
    release_handler.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert len(request_ids) == 2
    assert presented == ["write_file"]
    assert all(not decision.approved for decision in decisions)
    assert all(decision.reason == "child cancelled" for decision in decisions)
    assert coordinator.pending_count == 0
