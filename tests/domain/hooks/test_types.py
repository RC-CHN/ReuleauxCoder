from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    GuardDecision,
    HookPoint,
)


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


def test_before_llm_dispatch_callbacks_run_once_at_commit() -> None:
    context = BeforeLLMRequestContext(hook_point=HookPoint.BEFORE_LLM_REQUEST)
    calls: list[str] = []
    context.defer_until_dispatch(lambda _context: calls.append("first"))
    context.defer_until_dispatch(lambda _context: calls.append("second"))

    assert calls == []
    assert context._commit_dispatch_callbacks() == ()
    assert calls == ["first", "second"]

    assert context._commit_dispatch_callbacks() == ()
    assert calls == ["first", "second"]


def test_before_llm_dispatch_callback_failure_does_not_stop_commit() -> None:
    context = BeforeLLMRequestContext(hook_point=HookPoint.BEFORE_LLM_REQUEST)
    calls: list[str] = []

    def fail(_context: BeforeLLMRequestContext) -> None:
        calls.append("failed")
        raise RuntimeError("callback failed")

    context.defer_until_dispatch(fail)
    context.defer_until_dispatch(lambda _context: calls.append("completed"))

    failures = context._commit_dispatch_callbacks()

    assert calls == ["failed", "completed"]
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert str(failures[0]) == "callback failed"
