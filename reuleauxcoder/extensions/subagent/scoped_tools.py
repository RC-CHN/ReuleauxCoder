"""Subagent-only capability views over shared tool implementations."""

from __future__ import annotations

from copy import deepcopy

from reuleauxcoder.extensions.tools.base import Tool, ToolResult


READ_ONLY_BASELINE = frozenset({"read_file", "list_file", "glob", "grep", "lsp"})
EFFECTFUL_REASON_TOOLS = frozenset({"write_file", "edit_file", "shell"})
EFFECT_CLASSES = {
    "write_file": "workspace_write",
    "edit_file": "workspace_write",
    "shell": "process_execution",
}


class ScopedSubagentTool(Tool):
    """Expose a cloned tool through a child-specific schema and policy scope."""

    def __init__(
        self,
        inner: Tool,
        *,
        require_reason: bool,
        effect_class: str | None = None,
    ):
        super().__init__(getattr(inner, "backend", None))
        self._inner = inner
        self.name = inner.name
        self.description = inner.description
        self.parameters = deepcopy(inner.parameters)
        self.tool_source = getattr(inner, "tool_source", "builtin")
        self.server_name = getattr(inner, "server_name", None)
        self.approval_profile = getattr(inner, "approval_profile", None)
        self.effect_class = effect_class or getattr(inner, "effect_class", None)
        self._require_reason = require_reason
        if require_reason:
            properties = self.parameters.setdefault("properties", {})
            properties["reason"] = {
                "type": "string",
                "description": "Why this delegated side effect is required.",
                "minLength": 1,
            }
            required = list(self.parameters.get("required") or ())
            if "reason" not in required:
                required.append("reason")
            self.parameters["required"] = required

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def preflight_validate(self, **kwargs) -> str | None:
        arguments = dict(kwargs)
        reason = arguments.pop("reason", None)
        if self._require_reason and (not isinstance(reason, str) or not reason.strip()):
            return f"Error: child tool '{self.name}' requires a non-empty reason."
        return self._inner.preflight_validate(**arguments)

    def execute(self, **kwargs) -> ToolResult:
        arguments = dict(kwargs)
        reason = arguments.pop("reason", None)
        if self._require_reason and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError(f"child tool '{self.name}' requires a non-empty reason")
        return self._inner.execute(**arguments)

    def bind_agent(self, agent) -> None:
        bind = getattr(self._inner, "bind_agent", None)
        if callable(bind):
            bind(agent)

    def bind_execution(self, *, tool_call_id: str, session_generation: int) -> None:
        bind = getattr(self._inner, "bind_execution", None)
        if callable(bind):
            bind(
                tool_call_id=tool_call_id,
                session_generation=session_generation,
            )


def materialize_subagent_tool(tool: Tool) -> ScopedSubagentTool:
    """Clone one implementation and apply the child capability contract."""
    clone = getattr(tool, "clone_for_scope", None)
    if not callable(clone):
        raise TypeError(f"Tool '{tool.name}' does not support scoped materialization")
    inner = clone("subagent")
    read_only = tool.name in READ_ONLY_BASELINE
    return ScopedSubagentTool(
        inner,
        require_reason=tool.name in EFFECTFUL_REASON_TOOLS,
        effect_class=(
            "read_only_internal" if read_only else EFFECT_CLASSES.get(tool.name)
        ),
    )
