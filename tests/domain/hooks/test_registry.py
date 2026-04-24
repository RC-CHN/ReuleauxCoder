import pytest

from reuleauxcoder.domain.hooks.base import GuardHook, ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import GuardDecision, HookContext, HookPoint


class AllowGuard(GuardHook[HookContext]):
    def run(self, context: HookContext) -> GuardDecision:
        return GuardDecision.allow()


class DenyGuard(GuardHook[HookContext]):
    def run(self, context: HookContext) -> GuardDecision:
        return GuardDecision.deny("blocked")


class FailingGuard(GuardHook[HookContext]):
    def run(self, context: HookContext) -> GuardDecision:
        raise RuntimeError("boom")


class MetadataTransform(TransformHook[HookContext]):
    def __init__(self, *, name: str, priority: int, key: str, value: str):
        super().__init__(name=name, priority=priority)
        self.key = key
        self.value = value

    def run(self, context: HookContext) -> HookContext:
        context.metadata[self.key] = self.value
        return context


class NoneTransform(TransformHook[HookContext]):
    def run(self, context: HookContext) -> HookContext:
        return None  # type: ignore[return-value]


class WrongTypeTransform(TransformHook[HookContext]):
    def run(self, context: HookContext):
        return object()


class RecordingObserver(ObserverHook[HookContext]):
    def __init__(self, *, name: str, bucket: list[str]):
        super().__init__(name=name)
        self.bucket = bucket

    def run(self, context: HookContext) -> None:
        self.bucket.append(self.name)


class FailingObserver(ObserverHook[HookContext]):
    def run(self, context: HookContext) -> None:
        raise RuntimeError("boom")


def test_hook_registry_register_list_and_unregister() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE, AllowGuard(name="allow", priority=1)
    )

    assert registry.list_hooks(HookPoint.BEFORE_TOOL_EXECUTE) == {
        "before_tool_execute": ["allow"]
    }

    registry.unregister(HookPoint.BEFORE_TOOL_EXECUTE, "allow")
    assert registry.list_hooks(HookPoint.BEFORE_TOOL_EXECUTE) == {
        "before_tool_execute": []
    }


def test_hook_registry_run_guards_stops_on_deny() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE, AllowGuard(name="allow", priority=10)
    )
    registry.register(HookPoint.BEFORE_TOOL_EXECUTE, DenyGuard(name="deny", priority=5))
    registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE, AllowGuard(name="later", priority=1)
    )

    decisions = registry.run_guards(
        HookPoint.BEFORE_TOOL_EXECUTE,
        HookContext(hook_point=HookPoint.BEFORE_TOOL_EXECUTE),
    )

    assert [decision.allowed for decision in decisions] == [True, False]
    assert decisions[-1].reason == "blocked"


def test_hook_registry_run_guards_fail_closed_on_exception() -> None:
    registry = HookRegistry()
    registry.register(HookPoint.BEFORE_TOOL_EXECUTE, FailingGuard(name="failing"))

    decisions = registry.run_guards(
        HookPoint.BEFORE_TOOL_EXECUTE,
        HookContext(hook_point=HookPoint.BEFORE_TOOL_EXECUTE),
    )

    assert len(decisions) == 1
    assert decisions[0].allowed is False
    assert "guard hook 'failing' failed" in (decisions[0].reason or "")


def test_hook_registry_run_transforms_applies_priority_order() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        MetadataTransform(name="first", priority=10, key="a", value="1"),
    )
    registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        MetadataTransform(name="second", priority=5, key="b", value="2"),
    )

    context = HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE)
    result = registry.run_transforms(HookPoint.AFTER_TOOL_EXECUTE, context)

    assert result.metadata == {"a": "1", "b": "2"}


def test_hook_registry_run_transforms_rejects_none_result() -> None:
    registry = HookRegistry()
    registry.register(HookPoint.AFTER_TOOL_EXECUTE, NoneTransform(name="none"))

    with pytest.raises(TypeError):
        registry.run_transforms(
            HookPoint.AFTER_TOOL_EXECUTE,
            HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE),
        )


def test_hook_registry_run_transforms_rejects_wrong_type() -> None:
    registry = HookRegistry()
    registry.register(HookPoint.AFTER_TOOL_EXECUTE, WrongTypeTransform(name="wrong"))

    with pytest.raises(TypeError):
        registry.run_transforms(
            HookPoint.AFTER_TOOL_EXECUTE,
            HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE),
        )


def test_hook_registry_run_observers_fail_open() -> None:
    registry = HookRegistry()
    bucket: list[str] = []
    registry.register(HookPoint.AFTER_LLM_RESPONSE, FailingObserver(name="bad"))
    registry.register(
        HookPoint.AFTER_LLM_RESPONSE, RecordingObserver(name="good", bucket=bucket)
    )

    registry.run_observers(
        HookPoint.AFTER_LLM_RESPONSE,
        HookContext(hook_point=HookPoint.AFTER_LLM_RESPONSE),
    )

    assert bucket == ["good"]


def test_hook_registry_clone_is_isolated_copy() -> None:
    registry = HookRegistry()
    registry.register(HookPoint.BEFORE_TOOL_EXECUTE, AllowGuard(name="allow"))

    cloned = registry.clone()
    cloned.unregister(HookPoint.BEFORE_TOOL_EXECUTE, "allow")

    assert registry.list_hooks(HookPoint.BEFORE_TOOL_EXECUTE) == {
        "before_tool_execute": ["allow"]
    }
    assert cloned.list_hooks(HookPoint.BEFORE_TOOL_EXECUTE) == {
        "before_tool_execute": []
    }
