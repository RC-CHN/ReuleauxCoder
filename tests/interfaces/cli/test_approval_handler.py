from reuleauxcoder.domain.approval import (
    ApprovalPreview,
    ApprovalRequest,
    ApprovalSection,
    ApprovalSectionKind,
    PendingApproval,
)
from reuleauxcoder.interfaces.approval import make_approval_handler
from reuleauxcoder.interfaces.interactions import ReviewResponse


class _ReviewInteractor:
    def __init__(self) -> None:
        self.request = None

    def review(self, request):
        self.request = request
        return ReviewResponse(approved=True)


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
