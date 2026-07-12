from types import SimpleNamespace

from reuleauxcoder.domain.approval import ApprovalDecision, ApprovalRequest
from reuleauxcoder.extensions.subagent.approval import build_subagent_approval_provider


class _ParentProvider:
    def __init__(self) -> None:
        self.request = None

    def request_approval(self, request):
        self.request = request
        return ApprovalDecision.allow_once("policy allowed")


def test_subagent_approval_bubbles_with_attribution() -> None:
    parent_provider = _ParentProvider()
    parent = SimpleNamespace(approval_provider=parent_provider)
    provider = build_subagent_approval_provider(parent, "execute", "edit tests")
    decision = provider.request_approval(ApprovalRequest(tool_name="edit_file"))
    assert decision.approved is True
    assert parent_provider.request.metadata == {
        "is_subagent": True,
        "subagent_mode": "execute",
        "subagent_task": "edit tests",
        "approval_route": "bubble_to_parent",
    }
