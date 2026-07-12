from reuleauxcoder.presentation.semantics import describe_tool_invocation


def test_shell_display_promotes_command_to_subject() -> None:
    display = describe_tool_invocation(
        "shell", {"command": "pytest -q", "timeout": 120}
    )

    assert display.action == "RUN"
    assert display.subject == "pytest -q"
    assert display.detail == "timeout=120"


def test_file_display_honours_hidden_arguments() -> None:
    display = describe_tool_invocation(
        "read_file", {"path": "src/main.py", "offset": 40}, show_arguments=False
    )

    assert display.action == "READ"
    assert display.subject == ""
    assert display.detail == ""


def test_unknown_tool_has_stable_fallback_copy() -> None:
    display = describe_tool_invocation("custom_tool", {"value": "ok"})

    assert display.action == "CUSTOM TOOL"
    assert display.subject == ""
    assert display.detail == "value='ok'"
