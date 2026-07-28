import threading

from reuleauxcoder.domain.approval import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalGrantCandidate,
    ApprovalRequest,
    SharedApprovalProvider,
    approval_grant_covers_request,
)
from reuleauxcoder.domain.config.models import ApprovalRuleConfig


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


def _file_grant(*patterns: str) -> ApprovalGrantCandidate:
    return ApprovalGrantCandidate(
        id="exact",
        label="These files",
        description=", ".join(patterns),
        scope_key="workspace-1",
        proposed_rules=tuple(
            ApprovalRuleConfig(
                tool_name="edit_file",
                tool_source="builtin",
                pattern=pattern,
                scope_key="workspace-1",
                action="allow",
            )
            for pattern in patterns
        ),
    )


def test_grant_coverage_requires_all_subjects_and_same_environment() -> None:
    grant = _file_grant("src/one.py", "src/two.py")

    assert approval_grant_covers_request(
        grant,
        ApprovalRequest(
            tool_name="edit_file",
            tool_source="builtin",
            subjects=("src/one.py", "src/two.py"),
            scope_key="workspace-1",
        ),
    )
    assert not approval_grant_covers_request(
        grant,
        ApprovalRequest(
            tool_name="edit_file",
            tool_source="builtin",
            subjects=("src/one.py", "src/three.py"),
            scope_key="workspace-1",
        ),
    )
    assert not approval_grant_covers_request(
        grant,
        ApprovalRequest(
            tool_name="edit_file",
            tool_source="builtin",
            subjects=("src/one.py",),
            scope_key="workspace-2",
        ),
    )


def test_session_grant_installs_before_releasing_covered_queued_request() -> None:
    grant = _file_grant("src/**")
    presented = []
    first_presented = threading.Event()
    release_first = threading.Event()
    installed = threading.Event()
    decisions = {}

    def handler(pending) -> None:
        presented.append(pending.request.request_id)
        if pending.request.request_id == "first":
            first_presented.set()
            release_first.wait(timeout=2)
            pending.resolve(
                ApprovalDecision.allow_session(
                    grant,
                    "approved for this session",
                    reviewed=True,
                )
            )
            return
        pending.resolve(ApprovalDecision.deny_once("unexpected prompt"))

    def install(request, selected_grant) -> None:
        assert request.request_id == "first"
        assert selected_grant is grant
        installed.set()

    coordinator = ApprovalCoordinator(
        handler,
        timeout=2,
        on_session_grant=install,
    )

    def request(request_id: str, subject: str) -> None:
        decisions[request_id] = coordinator.request_approval(
            ApprovalRequest(
                request_id=request_id,
                tool_name="edit_file",
                tool_source="builtin",
                subjects=(subject,),
                scope_key="workspace-1",
            )
        )

    first = threading.Thread(target=request, args=("first", "src/one.py"))
    covered = threading.Thread(target=request, args=("covered", "src/two.py"))
    first.start()
    assert first_presented.wait(timeout=1)
    covered.start()
    for _ in range(100):
        if coordinator.pending_count == 2:
            break
        threading.Event().wait(0.005)

    release_first.set()
    first.join(timeout=1)
    covered.join(timeout=1)

    assert installed.is_set()
    assert presented == ["first"]
    assert decisions["first"].mode == "allow_session"
    assert decisions["first"].released_request_ids == ("covered",)
    assert decisions["covered"].mode == "allow_session"
    assert decisions["covered"].reviewed is False
    assert coordinator.pending_count == 0


def test_session_grant_failure_fails_closed_and_does_not_release_queue() -> None:
    grant = _file_grant("src/**")
    presented = []

    def handler(pending) -> None:
        presented.append(pending.request.request_id)
        if pending.request.request_id == "first":
            pending.resolve(ApprovalDecision.allow_session(grant))
        else:
            pending.resolve(ApprovalDecision.deny_once("reviewed separately"))

    coordinator = ApprovalCoordinator(
        handler,
        on_session_grant=lambda request, selected: (_ for _ in ()).throw(
            RuntimeError("store unavailable")
        ),
    )
    first = coordinator.request_approval(
        ApprovalRequest(
            request_id="first",
            tool_name="edit_file",
            tool_source="builtin",
            subjects=("src/one.py",),
            scope_key="workspace-1",
        )
    )
    second = coordinator.request_approval(
        ApprovalRequest(
            request_id="second",
            tool_name="edit_file",
            tool_source="builtin",
            subjects=("src/two.py",),
            scope_key="workspace-1",
        )
    )

    assert first.approved is False
    assert "failed closed" in (first.reason or "")
    assert second.reason == "reviewed separately"
    assert presented == ["first", "second"]
