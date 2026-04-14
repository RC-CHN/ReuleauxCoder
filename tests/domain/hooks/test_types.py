from reuleauxcoder.domain.hooks.types import GuardDecision


def test_guard_decision_allow() -> None:
    decision = GuardDecision.allow()
    assert decision.allowed is True
    assert decision.reason is None
    assert decision.warning is None
    assert decision.requires_approval is False


def test_guard_decision_deny() -> None:
    decision = GuardDecision.deny("blocked")
    assert decision.allowed is False
    assert decision.reason == "blocked"
    assert decision.requires_approval is False


def test_guard_decision_warn() -> None:
    decision = GuardDecision.warn("careful")
    assert decision.allowed is True
    assert decision.warning == "careful"
    assert decision.requires_approval is False


def test_guard_decision_require_approval() -> None:
    decision = GuardDecision.require_approval("confirm first")
    assert decision.allowed is True
    assert decision.reason == "confirm first"
    assert decision.requires_approval is True
