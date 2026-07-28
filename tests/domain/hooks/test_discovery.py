from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.hooks.builtin import builtin_hook_specs
from reuleauxcoder.domain.hooks.discovery import (
    discover_hook_specs,
    instantiate_hooks,
)


EXPECTED_BUILTIN_HOOKS = (
    ("ToolOutputTruncationHook", "after_tool_execute", 0),
    ("ToolPolicyGuardHook", "before_tool_execute", 100),
    ("ProjectContextHook", "before_llm_request", 50),
    ("ProjectContextStartupNotifier", "runner_startup", 0),
    ("LspEditObserverHook", "after_tool_execute", 200),
    ("LspDiagnosticsInjectorHook", "before_llm_request", 100),
    ("GitStateInjectorHook", "before_llm_request", 90),
    ("ProcessSessionInjectorHook", "before_llm_request", 80),
)


def test_builtin_hook_contributions_have_stable_explicit_order() -> None:
    specs = builtin_hook_specs()

    assert tuple(discover_hook_specs()) == specs
    assert tuple(
        (spec.hook_class.__name__, spec.hook_point.value, spec.priority)
        for spec in specs
    ) == EXPECTED_BUILTIN_HOOKS
    assert len({spec.hook_class for spec in specs}) == len(specs)


def test_builtin_hook_instances_preserve_declared_points_and_priorities() -> None:
    instances = instantiate_hooks(discover_hook_specs(), Config())

    assert tuple(
        (hook.__class__.__name__, point.value, hook.priority)
        for point, hook in instances
    ) == EXPECTED_BUILTIN_HOOKS
