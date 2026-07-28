from reuleauxcoder.extensions.command.builtin import (
    builtin_command_panel_specs,
    create_builtin_command_panel_registry,
)


def test_builtin_command_features_contribute_all_interactive_panels_in_order() -> None:
    expected = (
        "approval_rules",
        "mcp_servers",
        "mode_profiles",
        "model_profiles",
        "process_sessions",
        "sessions",
        "skills",
        "subagent_jobs",
        "thinking_effort",
    )

    specs = builtin_command_panel_specs()
    registry = create_builtin_command_panel_registry()

    assert tuple(spec.view_type for spec in specs) == expected
    assert registry.view_types() == expected
    assert all(spec.view_model_type is not object for spec in specs)
