"""Base class and backend dispatch helpers for tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any, final

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)


ToolResult = str | ToolOutcome
BackendHandler = Callable[..., ToolResult]


@dataclass(frozen=True, slots=True)
class _ArgumentIssue:
    code: str
    message: str
    path: str = "arguments"
    missing: tuple[str, ...] = ()


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, Mapping)
    return True


def _json_equal(left: Any, right: Any) -> bool:
    """Compare enum values without treating JSON booleans as numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _schema_issue(schema: Any, value: Any, *, path: str) -> _ArgumentIssue | None:
    """Validate the JSON Schema subset used by built-in and common MCP tools."""
    if schema is False:
        return _ArgumentIssue("schema_rejected", f"{path} is not allowed", path)
    if schema is True or not isinstance(schema, Mapping):
        return None

    declared_type = schema.get("type")
    expected_types: tuple[str, ...] = ()
    if isinstance(declared_type, str):
        expected_types = (declared_type,)
    elif isinstance(declared_type, Sequence) and not isinstance(
        declared_type, (str, bytes)
    ):
        expected_types = tuple(item for item in declared_type if isinstance(item, str))
    if expected_types and not any(
        _json_type_matches(value, expected) for expected in expected_types
    ):
        expected = " or ".join(expected_types)
        return _ArgumentIssue(
            "invalid_type",
            f"{path} must be {expected}, got {type(value).__name__}",
            path,
        )

    enum = schema.get("enum")
    if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes)):
        if not any(_json_equal(value, candidate) for candidate in enum):
            return _ArgumentIssue(
                "invalid_enum",
                f"{path} must be one of {list(enum)!r}",
                path,
            )

    if isinstance(value, Mapping):
        required = schema.get("required")
        if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
            missing = tuple(
                str(name)
                for name in required
                if isinstance(name, str) and name not in value
            )
            if missing:
                return _ArgumentIssue(
                    "missing_required_arguments",
                    f"{path} is missing required fields: {', '.join(missing)}",
                    path,
                    missing,
                )
        properties = schema.get("properties")
        declared_properties = properties if isinstance(properties, Mapping) else {}
        for name, item in value.items():
            child_path = f"{path}.{name}"
            if name in declared_properties:
                issue = _schema_issue(declared_properties[name], item, path=child_path)
                if issue is not None:
                    return issue
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                return _ArgumentIssue(
                    "unexpected_argument",
                    f"{child_path} is not accepted by this tool",
                    child_path,
                )
            if isinstance(additional, Mapping) or isinstance(additional, bool):
                issue = _schema_issue(additional, item, path=child_path)
                if issue is not None:
                    return issue

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            return _ArgumentIssue(
                "array_too_short",
                f"{path} must contain at least {minimum} items",
                path,
            )
        if isinstance(maximum, int) and len(value) > maximum:
            return _ArgumentIssue(
                "array_too_long",
                f"{path} must contain at most {maximum} items",
                path,
            )
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                issue = _schema_issue(item_schema, item, path=f"{path}[{index}]")
                if issue is not None:
                    return issue

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            if minimum == 1:
                message = f"{path} must be a non-empty string"
            else:
                message = f"{path} must contain at least {minimum} characters"
            return _ArgumentIssue(
                "string_too_short",
                message,
                path,
            )
        if isinstance(maximum, int) and len(value) > maximum:
            return _ArgumentIssue(
                "string_too_long",
                f"{path} must contain at most {maximum} characters",
                path,
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matches = re.search(pattern, value) is not None
            except re.error:
                matches = True
            if not matches:
                return _ArgumentIssue(
                    "pattern_mismatch",
                    f"{path} must match pattern {pattern!r}",
                    path,
                )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            if minimum == 1 and "integer" in expected_types:
                message = f"{path} must be a positive integer"
            else:
                message = f"{path} must be at least {minimum}"
            return _ArgumentIssue(
                "number_too_small",
                message,
                path,
            )
        if isinstance(maximum, (int, float)) and value > maximum:
            return _ArgumentIssue(
                "number_too_large",
                f"{path} must be at most {maximum}",
                path,
            )
    return None


def backend_handler(backend_id: str) -> Callable[[BackendHandler], BackendHandler]:
    """Mark a tool method as the implementation for a specific backend."""

    def decorator(func: BackendHandler) -> BackendHandler:
        setattr(func, "_tool_backend_id", backend_id)
        return func

    return decorator


class Tool(ABC):
    """Minimal tool interface with backend-aware dispatch helpers."""

    name: str
    description: str
    parameters: dict
    _backend_handlers: dict[str, str] = {}
    _agent_config: Any = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        handlers: dict[str, str] = {}
        for base in reversed(cls.__mro__[1:]):
            handlers.update(getattr(base, "_backend_handlers", {}))
        for attr_name, value in cls.__dict__.items():
            backend_id = getattr(value, "_tool_backend_id", None)
            if backend_id:
                handlers[backend_id] = attr_name
        cls._backend_handlers = handlers

    def __init__(self, backend: Any = None):
        self.backend = backend

    @final
    def preflight_validate(
        self,
        arguments: Mapping[str, Any],
        *,
        schema_only: bool = False,
    ) -> ToolOutcome | None:
        """Validate one invocation before approval or execution.

        Declared JSON Schema constraints are enforced for every tool. Subclasses
        may add environment-aware checks through ``_preflight_validate``.
        """
        issue = _schema_issue(self.parameters, arguments, path="arguments")
        if issue is not None:
            return self._argument_failure(issue)
        if schema_only:
            return None
        try:
            failure = self._preflight_validate(**dict(arguments))
        except TypeError as error:
            issue = _ArgumentIssue(
                "invalid_arguments",
                f"arguments could not be bound to '{self.name}': {error}",
            )
            return self._argument_failure(issue)
        if failure is None or isinstance(failure, ToolOutcome):
            return failure
        return ToolOutcome(
            status=ToolOutcomeStatus.FAILED,
            summary=f"Preflight rejected {self.name}",
            content=str(failure),
            model_content=(
                f"Tool call rejected [preflight_failed]: {failure}\n"
                "Correct the arguments using current conversation context and retry "
                "once. Do not repeat the unchanged tool call or invent unknown values."
            ),
            error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            metadata={"preflight_code": "domain_validation_failed"},
        )

    def _preflight_validate(self, **kwargs) -> str | ToolOutcome | None:
        """Optionally perform environment-aware, tool-specific validation."""
        return None

    def _argument_failure(self, issue: _ArgumentIssue) -> ToolOutcome:
        missing = f" Missing: {list(issue.missing)!r}." if issue.missing else ""
        return ToolOutcome(
            status=ToolOutcomeStatus.FAILED,
            summary=f"Invalid arguments for {self.name}",
            content=f"Invalid arguments for '{self.name}': {issue.message}",
            model_content=(
                f"Tool call rejected [invalid_arguments]: {issue.message}.{missing}\n"
                "Retry once with arguments that match the declared tool schema. "
                "Do not repeat the unchanged tool call or invent unknown values; "
                "ask the user when a required value cannot be determined."
            ),
            error_kind=ToolErrorKind.INVALID_ARGUMENTS,
            metadata={
                "preflight_code": issue.code,
                "argument_path": issue.path,
                "missing_arguments": issue.missing,
            },
        )

    @property
    def backend_id(self) -> str:
        return getattr(self.backend, "backend_id", "local")

    def run_backend(self, *args, **kwargs) -> ToolResult:
        """Dispatch to a tool-local implementation for the active backend."""
        handler_name = self._backend_handlers.get(self.backend_id)
        if handler_name is None:
            handler_name = self._backend_handlers.get("local")
        if handler_name is None:
            raise RuntimeError(
                f"Tool '{self.name}' has no handler for backend '{self.backend_id}' and no local fallback"
            )
        handler = getattr(self, handler_name)
        return handler(*args, **kwargs)

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """Run the tool and return a structured or legacy text result."""
        ...

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def clone_for_scope(self, scope: str) -> "Tool":
        """Build a fresh Tool and backend for one child Agent scope."""
        clone_backend = getattr(self.backend, "clone_for_scope", None)
        if not callable(clone_backend):
            raise TypeError(
                f"Tool '{self.name}' backend does not support scoped materialization"
            )
        cloned = type(self)(backend=clone_backend(scope))
        if self._agent_config is not None:
            cloned._agent_config = self._agent_config
        return cloned
