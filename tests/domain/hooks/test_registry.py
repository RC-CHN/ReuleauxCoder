import pytest

from reuleauxcoder.domain.hooks.base import GuardHook, ObserverHook, TransformHook
from reuleauxcoder.domain.hooks.registry import HookExecutionError, HookRegistry
from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    GuardDecision,
    HookContext,
    HookPoint,
)
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor


class AllowGuard(GuardHook[HookContext]):
    def run(self, context: HookContext) -> GuardDecision:
        return GuardDecision.allow()


class DenyGuard(GuardHook[HookContext]):
    def run(self, context: HookContext) -> GuardDecision:
        return GuardDecision.deny("blocked")


class FailingGuard(GuardHook[HookContext]):
    def run(self, context: HookContext) -> GuardDecision:
        raise RuntimeError("guard-secret-must-not-leak")


class RecordingGuard(GuardHook[HookContext]):
    def __init__(self, *, name: str, bucket: list[str], priority: int = 0):
        super().__init__(name=name, priority=priority)
        self.bucket = bucket

    def run(self, context: HookContext) -> GuardDecision:
        self.bucket.append(self.name)
        return GuardDecision.allow()


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


class FailingTransform(TransformHook[HookContext]):
    def run(self, context: HookContext) -> HookContext:
        raise RuntimeError("transform-secret-must-not-leak")


class RecordingObserver(ObserverHook[HookContext]):
    def __init__(self, *, name: str, bucket: list[str]):
        super().__init__(name=name)
        self.bucket = bucket

    def run(self, context: HookContext) -> None:
        self.bucket.append(self.name)


class FailingObserver(ObserverHook[HookContext]):
    def run(self, context: HookContext) -> None:
        raise RuntimeError("observer-secret-must-not-leak")


class SafeFactObserverError(RuntimeError):
    def __init__(self) -> None:
        self.phase = "stat"
        self.error_type = "PermissionError"
        self.code = "context_read_failed"
        self.ref = "AGENTS.md"
        super().__init__("safe-fact-secret-must-not-leak")


class SafeFactObserver(ObserverHook[HookContext]):
    def run(self, context: HookContext) -> None:
        raise SafeFactObserverError


class BrokenPerformanceMonitor:
    def record(self, *args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("performance-secret-must-not-leak")


class BrokenSnapshotMetadata(dict):
    def items(self):
        raise RuntimeError("snapshot-secret-must-not-leak")


class MutatingObserver(ObserverHook[HookContext]):
    def run(self, context) -> None:
        context.metadata["mutated"] = True


class MutatingTupleToolObserver(ObserverHook[HookContext]):
    def run(self, context) -> None:
        context.payload["request_params"]["tools"][0]["function"]["name"] = "mutated"


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
    effects: list[str] = []
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE,
        FailingGuard(name="failing", priority=10),
    )
    registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE,
        RecordingGuard(name="must_not_run", bucket=effects, priority=0),
    )

    decisions = registry.run_guards(
        HookPoint.BEFORE_TOOL_EXECUTE,
        HookContext(hook_point=HookPoint.BEFORE_TOOL_EXECUTE),
    )

    assert effects == []
    assert len(decisions) == 1
    assert decisions[0].allowed is False
    reason = decisions[0].reason or ""
    assert "phase=before_tool_execute" in reason
    assert "hook=failing" in reason
    assert "hook_kind=guard" in reason
    assert "error_type=RuntimeError" in reason
    assert "guard-secret-must-not-leak" not in reason
    diagnostic = registry.drain_diagnostics()[0]
    assert diagnostic.phase == "before_tool_execute"
    assert diagnostic.error_type == "RuntimeError"


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


def test_hook_registry_records_each_hook_timing() -> None:
    monitor = RuntimePerformanceMonitor()
    registry = HookRegistry(performance_monitor=monitor)
    registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        MetadataTransform(name="timed", priority=1, key="a", value="1"),
    )

    registry.run_transforms(
        HookPoint.AFTER_TOOL_EXECUTE,
        HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE),
    )

    sample = monitor.snapshot()[-1]
    assert sample.category == "hook"
    assert sample.name == "after_tool_execute:timed"
    assert sample.attribute_map()["hook_kind"] == "transform"


def test_performance_failure_is_secondary_and_safely_observable() -> None:
    registry = HookRegistry(
        performance_monitor=BrokenPerformanceMonitor(),  # type: ignore[arg-type]
    )
    registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        MetadataTransform(name="timed", priority=1, key="effect", value="kept"),
    )
    context = HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE)

    result = registry.run_transforms(HookPoint.AFTER_TOOL_EXECUTE, context)

    assert result.metadata == {"effect": "kept"}
    diagnostics = registry.drain_diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0].phase == "performance_measurement"
    assert diagnostics[0].hook_name == "timed"
    assert diagnostics[0].error_type == "RuntimeError"
    assert "performance-secret-must-not-leak" not in diagnostics[0].message


def test_performance_failure_does_not_replace_transform_primary_failure() -> None:
    registry = HookRegistry(
        performance_monitor=BrokenPerformanceMonitor(),  # type: ignore[arg-type]
    )
    registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        FailingTransform(name="failing_transform"),
    )

    with pytest.raises(HookExecutionError) as raised:
        registry.run_transforms(
            HookPoint.AFTER_TOOL_EXECUTE,
            HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE),
        )

    assert raised.value.error_type == "RuntimeError"
    assert "performance-secret-must-not-leak" not in str(raised.value)
    assert "transform-secret-must-not-leak" not in str(raised.value)
    assert [item.phase for item in registry.drain_diagnostics()] == [
        "performance_measurement",
        "after_tool_execute",
    ]


def test_hook_registry_run_transforms_rejects_none_result() -> None:
    registry = HookRegistry()
    registry.register(HookPoint.AFTER_TOOL_EXECUTE, NoneTransform(name="none"))

    with pytest.raises(HookExecutionError) as raised:
        registry.run_transforms(
            HookPoint.AFTER_TOOL_EXECUTE,
            HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE),
        )
    assert raised.value.phase == "after_tool_execute"
    assert raised.value.hook_name == "none"
    assert raised.value.error_type == "TypeError"
    diagnostic = registry.drain_diagnostics()[0]
    assert diagnostic.hook_name == "none"
    assert diagnostic.hook_kind.value == "transform"
    assert diagnostic.severity == "error"


def test_hook_registry_run_transforms_rejects_wrong_type() -> None:
    registry = HookRegistry()
    registry.register(HookPoint.AFTER_TOOL_EXECUTE, WrongTypeTransform(name="wrong"))

    with pytest.raises(HookExecutionError):
        registry.run_transforms(
            HookPoint.AFTER_TOOL_EXECUTE,
            HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE),
        )


def test_transform_primary_failure_is_safe_terminal_without_exception_context() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        FailingTransform(name="failing_transform"),
    )

    with pytest.raises(HookExecutionError) as raised:
        registry.run_transforms(
            HookPoint.AFTER_TOOL_EXECUTE,
            HookContext(hook_point=HookPoint.AFTER_TOOL_EXECUTE),
        )

    error = raised.value
    assert error.phase == "after_tool_execute"
    assert error.hook_name == "failing_transform"
    assert error.hook_kind == "transform"
    assert error.error_type == "RuntimeError"
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "transform-secret-must-not-leak" not in str(error)


def test_hook_registry_run_observers_fail_open() -> None:
    registry = HookRegistry()
    bucket: list[str] = []
    registry.register(HookPoint.AFTER_LLM_RESPONSE, FailingObserver(name="bad"))
    registry.register(
        HookPoint.AFTER_LLM_RESPONSE, RecordingObserver(name="good", bucket=bucket)
    )

    diagnostics = registry.run_observers(
        HookPoint.AFTER_LLM_RESPONSE,
        HookContext(hook_point=HookPoint.AFTER_LLM_RESPONSE),
    )

    assert bucket == ["good"]
    assert len(diagnostics) == 1
    assert diagnostics[0].hook_name == "bad"
    assert diagnostics[0].phase == "after_llm_response"
    assert diagnostics[0].error_type == "RuntimeError"
    assert "observer-secret-must-not-leak" not in diagnostics[0].message


def test_observer_snapshot_failure_is_isolated_and_safely_observable() -> None:
    effects: list[str] = []
    registry = HookRegistry()
    registry.register(
        HookPoint.AFTER_LLM_RESPONSE,
        RecordingObserver(name="must_not_run", bucket=effects),
    )

    diagnostics = registry.run_observers(
        HookPoint.AFTER_LLM_RESPONSE,
        HookContext(
            hook_point=HookPoint.AFTER_LLM_RESPONSE,
            metadata=BrokenSnapshotMetadata(),
        ),
    )

    assert effects == []
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.hook_name == "observer_snapshot"
    assert diagnostic.phase == "observer_snapshot"
    assert diagnostic.error_type == "RuntimeError"
    assert "snapshot-secret-must-not-leak" not in diagnostic.message
    assert registry.drain_diagnostics() == diagnostics


def test_observer_preserves_only_allowlisted_safe_failure_facts() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.RUNNER_STARTUP,
        SafeFactObserver(name="safe_fact_observer"),
    )

    diagnostics = registry.run_observers(
        HookPoint.RUNNER_STARTUP,
        HookContext(hook_point=HookPoint.RUNNER_STARTUP),
    )

    diagnostic = diagnostics[0]
    assert diagnostic.phase == "stat"
    assert diagnostic.error_type == "PermissionError"
    assert diagnostic.code == "context_read_failed"
    assert diagnostic.ref == "AGENTS.md"
    assert "phase=stat" in diagnostic.message
    assert "error_type=PermissionError" in diagnostic.message
    assert "code=context_read_failed" in diagnostic.message
    assert "ref=AGENTS.md" in diagnostic.message
    assert "safe-fact-secret-must-not-leak" not in diagnostic.message


def test_diagnostic_sink_failure_is_recorded_without_escaping() -> None:
    def fail_sink(_diagnostic) -> None:
        raise RuntimeError("sink-secret-must-not-leak")

    registry = HookRegistry(diagnostic_sink=fail_sink)
    registry.register(
        HookPoint.AFTER_LLM_RESPONSE,
        FailingObserver(name="bad"),
    )

    diagnostics = registry.run_observers(
        HookPoint.AFTER_LLM_RESPONSE,
        HookContext(hook_point=HookPoint.AFTER_LLM_RESPONSE),
    )

    assert len(diagnostics) == 1
    stored = registry.drain_diagnostics()
    assert [diagnostic.hook_name for diagnostic in stored] == [
        "bad",
        "diagnostic_sink",
    ]
    assert stored[-1].phase == "diagnostic_sink"
    assert stored[-1].error_type == "RuntimeError"
    assert "sink-secret-must-not-leak" not in stored[-1].message
    assert "observer-secret-must-not-leak" not in "\n".join(
        diagnostic.message for diagnostic in stored
    )


def test_observer_receives_immutable_snapshot_and_failure_is_observable() -> None:
    emitted = []
    registry = HookRegistry(diagnostic_sink=emitted.append)
    registry.register(
        HookPoint.AFTER_LLM_RESPONSE,
        MutatingObserver(name="mutator"),
    )
    context = HookContext(
        hook_point=HookPoint.AFTER_LLM_RESPONSE,
        metadata={"stable": True},
    )

    diagnostics = registry.run_observers(HookPoint.AFTER_LLM_RESPONSE, context)

    assert context.metadata == {"stable": True}
    assert diagnostics == tuple(emitted)
    assert diagnostics[0].hook_name == "mutator"
    assert registry.drain_diagnostics() == diagnostics
    assert registry.drain_diagnostics() == ()


def test_observer_cannot_mutate_tool_dict_nested_in_tuple() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        MutatingTupleToolObserver(name="tuple_mutator"),
    )
    tool = {"function": {"name": "stable"}}
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        request_params={"tools": (tool,)},
    )

    diagnostics = registry.run_observers(HookPoint.BEFORE_LLM_REQUEST, context)

    assert tool["function"]["name"] == "stable"
    assert len(diagnostics) == 1
    assert diagnostics[0].hook_name == "tuple_mutator"


def test_replacement_transform_preserves_deferred_dispatch_callbacks() -> None:
    calls: list[tuple[str, BeforeLLMRequestContext]] = []

    class Defer(TransformHook[BeforeLLMRequestContext]):
        def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
            context.defer_until_dispatch(
                lambda dispatched: calls.append(("deferred", dispatched))
            )
            return context

    class Replace(TransformHook[BeforeLLMRequestContext]):
        def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
            replacement = BeforeLLMRequestContext(
                hook_point=context.hook_point,
                request_params=dict(context.request_params),
                messages=[dict(message) for message in context.messages],
            )
            replacement.defer_until_dispatch(
                lambda dispatched: calls.append(("replacement", dispatched))
            )
            return replacement

    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        Defer(name="defer", priority=10),
    )
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        Replace(name="replace", priority=0),
    )
    original = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[{"role": "user", "content": "payload"}],
    )

    transformed = registry.run_transforms(HookPoint.BEFORE_LLM_REQUEST, original)

    assert transformed is not original
    assert transformed._commit_dispatch_callbacks() == ()
    assert calls == [("deferred", transformed), ("replacement", transformed)]
    assert original._commit_dispatch_callbacks() == ()


def test_hook_registry_clone_is_isolated_copy() -> None:
    class CloneableGuard(AllowGuard):
        def clone_for_scope(self, scope: str):
            del scope
            return CloneableGuard(name=self.name, priority=self.priority)

    registry = HookRegistry()
    registry.register(HookPoint.BEFORE_TOOL_EXECUTE, CloneableGuard(name="allow"))

    cloned = registry.clone()
    cloned.unregister(HookPoint.BEFORE_TOOL_EXECUTE, "allow")

    assert registry.list_hooks(HookPoint.BEFORE_TOOL_EXECUTE) == {
        "before_tool_execute": ["allow"]
    }
    assert cloned.list_hooks(HookPoint.BEFORE_TOOL_EXECUTE) == {
        "before_tool_execute": []
    }


def test_hook_registry_rejects_implicit_scope_clone() -> None:
    registry = HookRegistry()
    registry.register(HookPoint.BEFORE_TOOL_EXECUTE, AllowGuard(name="allow"))

    with pytest.raises(TypeError, match="must declare explicit clone_for_scope"):
        registry.clone(scope="subagent")


def test_registry_passes_explicit_clone_scope_to_hooks() -> None:
    seen = []

    class ScopedGuard(AllowGuard):
        def clone_for_scope(self, scope: str):
            seen.append(scope)
            return ScopedGuard(name=self.name)

    registry = HookRegistry()
    registry.register(HookPoint.BEFORE_TOOL_EXECUTE, ScopedGuard(name="scoped"))

    registry.clone(scope="subagent")

    assert seen == ["subagent"]
