"""Editable configuration schema, strict shape checks and secret-safe projections."""

from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
import math
import types
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit, urlunsplit

from reuleauxcoder.domain.config.management import ConfigIssue
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ContextConfig,
    MCPServerConfig,
    ModelProfileConfig,
    ModeConfig,
    PromptConfig,
    RemoteExecConfig,
    SkillsConfig,
    UIConfig,
)
from reuleauxcoder.domain.images import ImageConfig
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _type_schema(annotation) -> dict:
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return {"anyOf": [_type_schema(item) for item in arguments]}
    if origin is Literal:
        return {"enum": list(arguments)}
    if is_dataclass(annotation):
        return dataclass_schema(annotation)
    if origin in (list, tuple):
        return {"type": "array", "items": _type_schema(arguments[0])}
    if origin is dict:
        return {"type": "object", "additionalProperties": _type_schema(arguments[1])}
    if annotation in (Any, object):
        return {}
    return {
        "type": {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            type(None): "null",
        }[annotation]
    }


def dataclass_schema(cls, *, exclude=()) -> dict:
    hints = get_type_hints(cls)
    properties = {}
    for field in fields(cls):
        if field.name in exclude:
            continue
        value = _type_schema(hints[field.name])
        if field.name in {
            "max_tokens",
            "max_context_tokens",
            "max_preview_lines",
            "max_preview_chars",
            "auto_review_timeout_seconds",
            "max_edge_px",
            "normal_max_bytes",
            "detail_max_base64_bytes",
            "originals_cache_max_bytes",
            "import_max_bytes",
            "max_pixels",
        }:
            value["minimum"] = 1
        if field.name == "temperature":
            value.update(minimum=0, maximum=2)
        choices = {
            "provider": ["openai-compatible", "anthropic"],
            "verbosity": ["compact", "standard", "debug"],
            "tool_output": ["errors", "summary", "preview", "full"],
            "reasoning_display": ["hidden", "indicator", "inline"],
            "notification_threshold": ["debug", "info", "warning", "error"],
            "typescript_mode": ["auto", "native", "legacy"],
        }
        if field.name in choices:
            value["enum"] = choices[field.name]
        if sensitive_path((field.name,)):
            value["writeOnly"] = True
        if field.default is not MISSING:
            value["default"] = field.default
        properties[field.name] = value
    return object_schema(properties)


def config_schema() -> dict:
    profile = dataclass_schema(ModelProfileConfig, exclude=("name",))
    app = {
        name: spec
        for name, spec in profile["properties"].items()
        if name not in ("context", "reasoning_effort_values", "reasoning_effort_param")
    }
    app["llm_debug_trace"] = {"type": "boolean", "default": False}
    lsp = dataclass_schema(LspConfig, exclude=("server_overrides",))
    lsp["properties"]["servers"] = {
        "type": "object",
        "additionalProperties": dataclass_schema(
            LspServerOverride, exclude=("language",)
        ),
    }
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    schema = object_schema(
        {
            "app": object_schema(app),
            "models": object_schema(
                {
                    **{
                        key: nullable_string
                        for key in ("active", "active_main", "active_sub")
                    },
                    "profiles": {"type": "object", "additionalProperties": profile},
                }
            ),
            "modes": object_schema(
                {
                    "active": nullable_string,
                    "profiles": {
                        "type": "object",
                        "additionalProperties": dataclass_schema(
                            ModeConfig, exclude=("name",)
                        ),
                    },
                }
            ),
            "mcp": object_schema(
                {
                    "servers": {
                        "type": "object",
                        "additionalProperties": dataclass_schema(
                            MCPServerConfig, exclude=("name",)
                        ),
                    }
                }
            ),
            "approval": dataclass_schema(ApprovalConfig),
            "skills": dataclass_schema(SkillsConfig),
            "prompt": dataclass_schema(PromptConfig),
            "context": dataclass_schema(ContextConfig),
            "remote_exec": dataclass_schema(RemoteExecConfig),
            "ui": dataclass_schema(UIConfig),
            "attachments": object_schema({"image": dataclass_schema(ImageConfig)}),
            "lsp": lsp,
            "web": object_schema(
                {
                    "enabled": {"type": "boolean"},
                    "proxy": {"type": "string"},
                    "search_provider": {"enum": ["auto", "exa", "parallel"]},
                    "allow_private_networks": {"type": "boolean"},
                }
            ),
            "shell": object_schema({"rtk": {"enum": ["auto", "on", "off"]}}),
            "session": object_schema(
                {"auto_save": {"type": "boolean"}, "dir": nullable_string}
            ),
            "cli": object_schema({"history_file": nullable_string}),
            "goal": object_schema(
                {
                    "default_token_budget": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}]
                    }
                }
            ),
            "tool_output": object_schema(
                {
                    "max_chars": {"type": "integer"},
                    "max_lines": {"type": "integer"},
                    "store_full_output": {"type": "boolean"},
                    "store_dir": nullable_string,
                }
            ),
            "meta": {"type": "object", "additionalProperties": {}},
        }
    )
    return schema


def pointer(parts) -> str:
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def shape_issues(value, schema, path="", *, unknown="error") -> list[ConfigIssue]:
    if "anyOf" in schema:
        alternatives = [
            shape_issues(value, part, path, unknown=unknown) for part in schema["anyOf"]
        ]
        return min(alternatives, key=len)
    if "enum" in schema and (
        value not in schema["enum"] or isinstance(value, (dict, list))
    ):
        return [
            ConfigIssue(
                "invalid_value", path, "Value must be one of the documented choices."
            )
        ]
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, (list, tuple)),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if kind and not valid[kind]:
        return [ConfigIssue("invalid_type", path, f"Expected {kind}.")]
    if type(value) in (int, float) and (
        value < schema.get("minimum", -math.inf)
        or value > schema.get("maximum", math.inf)
    ):
        return [
            ConfigIssue("invalid_range", path, "Value is outside the documented range.")
        ]
    result = []
    if isinstance(value, dict) and kind == "object":
        for key, item in value.items():
            child_path = path + pointer((key,))
            spec = schema.get("properties", {}).get(
                key, schema.get("additionalProperties", {})
            )
            if spec is False:
                result.append(
                    ConfigIssue(
                        "unknown_field",
                        child_path,
                        "Unknown configuration field.",
                        unknown,
                    )
                )
            else:
                result.extend(shape_issues(item, spec, child_path, unknown=unknown))
    elif isinstance(value, (list, tuple)) and kind == "array":
        for index, item in enumerate(value):
            result.extend(
                shape_issues(
                    item, schema.get("items", {}), path + f"/{index}", unknown=unknown
                )
            )
    return result


def sensitive_path(parts) -> bool:
    return any(
        str(part).lower()
        in {
            "api_key",
            "password",
            "secret",
            "token",
            "env",
            "args",
            "bootstrap_access_secret",
        }
        or any(word in str(part).lower() for word in ("password", "secret", "api_key"))
        for part in parts
    )


def redact(value, parts=()):
    if sensitive_path(parts):
        return "[configured]" if value else value
    if isinstance(value, dict):
        return {key: redact(item, (*parts, key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, (*parts, str(index))) for index, item in enumerate(value)]
    if isinstance(value, str) and "://" in value:
        try:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                host = parsed.netloc.rsplit("@", 1)[-1]
                return urlunsplit(
                    (
                        parsed.scheme,
                        host,
                        parsed.path,
                        "[redacted]" if parsed.query else "",
                        "",
                    )
                )
        except ValueError:
            return "[redacted]"
    return value
