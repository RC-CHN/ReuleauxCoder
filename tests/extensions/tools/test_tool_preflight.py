from reuleauxcoder.domain.agent.tool_outcome import ToolErrorKind
from reuleauxcoder.extensions.tools.base import Tool


class _RecordingTool(Tool):
    name = "recording_tool"
    description = "Tool used to exercise base preflight validation."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "count": {"type": "integer", "minimum": 1},
            "mode": {"type": "string", "enum": ["safe", "fast"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                    },
                    "required": ["value"],
                },
            },
        },
        "required": ["name", "count"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        super().__init__()
        self.domain_calls = 0

    def _preflight_validate(self, **kwargs):
        self.domain_calls += 1
        if kwargs["name"] == "blocked":
            return "name is blocked by the recording tool"
        return None

    def execute(self, **kwargs):
        return str(kwargs)


def test_base_preflight_rejects_missing_required_arguments() -> None:
    tool = _RecordingTool()

    failure = tool.preflight_validate({"name": "demo"})

    assert failure is not None
    assert failure.error_kind is ToolErrorKind.INVALID_ARGUMENTS
    assert failure.metadata["preflight_code"] == "missing_required_arguments"
    assert failure.metadata["missing_arguments"] == ("count",)
    assert "Retry once" in failure.model_text
    assert tool.domain_calls == 0


def test_base_preflight_validates_nested_arguments_and_json_types() -> None:
    tool = _RecordingTool()

    boolean_integer = tool.preflight_validate({"name": "demo", "count": True})
    nested_missing = tool.preflight_validate(
        {"name": "demo", "count": 1, "items": [{}]}
    )

    assert boolean_integer is not None
    assert boolean_integer.metadata["preflight_code"] == "invalid_type"
    assert nested_missing is not None
    assert nested_missing.metadata["argument_path"] == "arguments.items[0]"
    assert nested_missing.metadata["missing_arguments"] == ("value",)
    assert tool.domain_calls == 0


def test_schema_only_skips_domain_preflight() -> None:
    tool = _RecordingTool()

    failure = tool.preflight_validate(
        {"name": "blocked", "count": 1},
        schema_only=True,
    )

    assert failure is None
    assert tool.domain_calls == 0


def test_domain_preflight_is_wrapped_with_model_guidance() -> None:
    tool = _RecordingTool()

    failure = tool.preflight_validate({"name": "blocked", "count": 1})

    assert failure is not None
    assert failure.content == "name is blocked by the recording tool"
    assert failure.metadata["preflight_code"] == "domain_validation_failed"
    assert "Do not repeat the unchanged tool call" in failure.model_text
    assert tool.domain_calls == 1
