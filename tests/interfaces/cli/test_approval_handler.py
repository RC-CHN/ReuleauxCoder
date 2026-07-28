from reuleauxcoder.domain.approval import (
    ApprovalGrantCandidate,
    ApprovalPreview,
    ApprovalRequest,
    ApprovalSection,
    ApprovalSectionKind,
    PendingApproval,
)
from reuleauxcoder.domain.config.models import ApprovalRuleConfig
from reuleauxcoder.interfaces.approval import make_approval_handler
from reuleauxcoder.interfaces.interactions import ReviewResponse


class _ReviewInteractor:
    def __init__(self, response=None) -> None:
        self.request = None
        self.response = response or ReviewResponse(approved=True)

    def review(self, request):
        self.request = request
        return self.response


def test_cli_handler_forwards_shared_typed_preview_without_rebuilding() -> None:
    section = ApprovalSection(
        id="diff",
        title="Proposed edit diff",
        kind=ApprovalSectionKind.DIFF,
        content="--- a/demo\n+++ b/demo\n",
    )
    pending = PendingApproval(
        ApprovalRequest(
            tool_name="edit_file",
            tool_source="builtin",
            preview=ApprovalPreview(sections=(section,)),
        )
    )
    interactor = _ReviewInteractor()

    make_approval_handler(interactor)(pending)

    assert interactor.request.sections == (section,)
    assert interactor.request.context.tool_name == "edit_file"
    assert pending.decision is not None and pending.decision.approved is True
    assert pending.decision.reviewed is True


def test_cli_handler_maps_opaque_session_scope_back_to_domain_grant() -> None:
    grant = ApprovalGrantCandidate(
        id="exact",
        label="This file",
        description="src/app.py",
        proposed_rules=(
            ApprovalRuleConfig(
                tool_name="edit_file",
                pattern="src/app.py",
                action="allow",
            ),
        ),
        scope_key="scope",
    )
    pending = PendingApproval(
        ApprovalRequest(
            tool_name="edit_file",
            grant_candidates=(grant,),
        )
    )
    interactor = _ReviewInteractor(
        ReviewResponse(
            approved=True,
            action="allow_session",
            selected_id="exact",
        )
    )

    make_approval_handler(interactor)(pending)

    assert interactor.request.grant_options[0].id == "exact"
    assert pending.decision is not None
    assert pending.decision.mode == "allow_session"
    assert pending.decision.grant is grant
    assert pending.decision.reviewed is True


def test_cli_handler_fails_closed_for_unknown_session_scope() -> None:
    pending = PendingApproval(ApprovalRequest(tool_name="edit_file"))
    interactor = _ReviewInteractor(
        ReviewResponse(
            approved=True,
            action="allow_session",
            selected_id="forged",
        )
    )

    make_approval_handler(interactor)(pending)

    assert pending.decision is not None
    assert pending.decision.approved is False
    assert "invalid session approval scope" in (pending.decision.reason or "")


def test_cli_handler_records_direct_denial_as_human_review() -> None:
    pending = PendingApproval(ApprovalRequest(tool_name="shell"))
    interactor = _ReviewInteractor(
        ReviewResponse(
            approved=False,
            action="deny",
            reason="Use the existing build task instead.",
        )
    )

    make_approval_handler(interactor)(pending)

    assert pending.decision is not None
    assert pending.decision.mode == "deny_once"
    assert pending.decision.reason == "Use the existing build task instead."
    assert pending.decision.reviewed is True
