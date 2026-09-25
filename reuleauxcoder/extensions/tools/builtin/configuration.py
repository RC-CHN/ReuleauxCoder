"""Root-owned configuration tools; all mutations use the shared service."""

import json

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.approval import (
    ApprovalPreview,
    ApprovalSection,
    ApprovalSectionKind,
)
from reuleauxcoder.domain.config.management import (
    ConfigurationPort,
    ConfigOperationError,
)
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.builtin.control import _invalid


class _ConfigurationTool(Tool):
    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._configuration: ConfigurationPort | None = None

    def bind_configuration(self, service: ConfigurationPort):
        self._configuration = service

    @backend_handler("local")
    def _execute_local(self, **arguments):
        if self._configuration is None:
            return _invalid(
                "Configuration management is only available on the owning workspace host."
            )
        operation = arguments.pop("operation", self.name.removeprefix("config_"))
        allowed = {
            "config_read": {"describe", "inspect", "history"},
            "config_prepare": {"prepare", "revert"},
            "config_validate": {"validate"},
            "config_apply": {"apply"},
        }[self.name]
        if operation not in allowed:
            return _invalid("Unsupported configuration operation.")
        try:
            result = self._configuration.execute(operation, arguments, actor="model")
            return ToolOutcome(
                summary=f"Configuration {operation}",
                content=json.dumps(result, ensure_ascii=False),
            )
        except ConfigOperationError as error:
            return _invalid(f"{error.code}: {error}")
        except (ValueError, OSError):
            return _invalid(
                "Configuration operation failed; inspect the configuration before retrying."
            )

    @backend_handler("remote_relay")
    def _execute_remote(self, **arguments):
        # Configuration belongs to the host, never to the remote filesystem peer.
        return self._execute_local(**arguments)


class ConfigReadTool(_ConfigurationTool):
    name = "config_read"
    effect_class = "read_only_internal"
    parallel_safe = True
    description = "Inspect rcoder's configuration schema, redacted values and sources, or change history. Read describe before proposing edits. Never infer secrets from redacted placeholders."
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["describe", "inspect", "history"]},
            "section": {
                "type": "string",
                "description": "Optional top-level section for describe, such as models or lsp.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    def execute(self, operation: str, **parameters):
        return self.run_backend(operation=operation, **parameters)


class ConfigPrepareTool(_ConfigurationTool):
    name = "config_prepare"
    effect_class = "control_plane_internal"
    description = "Prepare a configuration candidate or a revert candidate without changing active files or the running agent. Use JSON-pointer field paths and a revision from config_read. Credentials and permission policy are owned by the user. Apply separately after validation."
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["prepare", "revert"]},
            "scope": {"type": "string", "enum": ["workspace", "user", "explicit"]},
            "base_revision": {"type": "string"},
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "value": {},
                        "remove": {"type": "boolean"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
            "change_id": {"type": "string"},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    def execute(self, operation: str, **parameters):
        return self.run_backend(operation=operation, **parameters)


class ConfigValidateTool(_ConfigurationTool):
    name = "config_validate"
    effect_class = "configuration_probe"
    description = "Validate a candidate or current rcoder configuration. static is offline; startup constructs the provider in an isolated process without tasks/hooks; model sends one bounded request and may consume tokens. Report unknown connectivity separately from invalid configuration."
    parameters = {
        "type": "object",
        "properties": {
            "change_id": {"type": "string"},
            "checks": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string", "enum": ["static", "startup", "model"]},
            },
            "profiles": {
                "type": "array", "minItems": 1, "maxItems": 8,
                "items": {"type": "string"},
                "description": "Profile names for model checks. Defaults to affected profiles for a candidate, or the current main profile.",
            },
        },
        "additionalProperties": False,
    }

    def execute(self, **parameters):
        return self.run_backend(**parameters)

    def approval_preview(self, arguments):
        if self._configuration is None:
            return None
        if change_id := arguments.get("change_id"):
            candidate = self._configuration.review(change_id, actor="model")
            targets = candidate["available_model_targets"]
            required = [item["profile"] for item in candidate["model_targets"]]
            changes = candidate["diff"]
        else:
            state = self._configuration.execute("inspect", {}, actor="model")
            targets = state["model_targets"]
            required = []
            changes = []
        names = arguments.get("profiles", required or [item["profile"] for item in targets if "main" in item["roles"]])
        return ApprovalPreview(
            sections=(
                ApprovalSection(
                    id="configuration_validation",
                    title="Configuration validation",
                    kind=ApprovalSectionKind.JSON,
                    content={
                        "checks": arguments.get("checks", ["static"]),
                        "models": [item for item in targets if item["profile"] in names],
                        "coverage": "Configured text request parameters; tools, images, MCP and LSP are not checked.",
                        "changes": changes,
                    },
                ),
            )
        )


class ConfigApplyTool(_ConfigurationTool):
    name = "config_apply"
    effect_class = "configuration_write"
    description = "Apply a prepared rcoder configuration candidate after validation and tool authorization. Active model changes require a recent successful model probe. Persistent changes activate on the next core start; the current task keeps its working configuration. Stale candidates must be prepared again."
    parameters = {
        "type": "object",
        "properties": {"change_id": {"type": "string"}},
        "required": ["change_id"],
        "additionalProperties": False,
    }

    def execute(self, change_id: str):
        return self.run_backend(change_id=change_id)

    def approval_subjects(self, arguments):
        return ("configuration:" + arguments["change_id"],)

    def approval_preview(self, arguments):
        if self._configuration is None:
            return None
        candidate = self._configuration.review(arguments["change_id"], actor="model")
        return ApprovalPreview(
            sections=(
                ApprovalSection(
                    id="configuration",
                    title="Configuration changes",
                    kind=ApprovalSectionKind.JSON,
                    content={
                        "scope": candidate["scope"],
                        "activation": candidate["activation"],
                        "target": candidate["target_path"],
                        "changes": candidate["diff"],
                        "checks": candidate["checks"],
                        "required_models": candidate["model_targets"],
                    },
                ),
            )
        )
