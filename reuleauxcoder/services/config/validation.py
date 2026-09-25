"""Side-effect-free configuration decoding, layer resolution and validation."""

from __future__ import annotations

from dataclasses import replace
import json
from urllib.parse import urlsplit

import yaml

from reuleauxcoder.domain.config.management import ConfigIssue
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.services.config.definition import config_schema, shape_issues
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.infrastructure.persistence.config_files import MAX_CONFIG_BYTES


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, "Keys must be unique strings", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _bounded_json(value, depth=0, budget=None):
    budget = budget if budget is not None else [10000]
    budget[0] -= 1
    if depth > 32 or budget[0] < 0:
        raise ValueError("Configuration nesting or size exceeds the limit")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Configuration keys must be strings")
        for item in value.values():
            _bounded_json(item, depth + 1, budget)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _bounded_json(item, depth + 1, budget)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("Configuration values must be JSON-compatible")


def json_document(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object")
    _bounded_json(value)
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(text.encode()) > MAX_CONFIG_BYTES:
        raise ValueError("Configuration exceeds 1 MiB")
    return json.loads(text)


def parse_document(
    content: bytes | None, source: str
) -> tuple[dict, list[ConfigIssue]]:
    if content is None:
        return {}, []
    if len(content) > MAX_CONFIG_BYTES:
        return {}, [
            ConfigIssue("too_large", "", "Configuration exceeds 1 MiB.", source=source)
        ]
    try:
        data = yaml.load(content.decode("utf-8-sig"), Loader=_UniqueLoader)
        return json_document({} if data is None else data), []
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        return {}, [
            ConfigIssue(
                "invalid_yaml",
                "",
                "Invalid YAML or duplicate/non-string keys.",
                source=source,
                line=mark.line + 1 if mark else None,
            )
        ]
    except (ValueError, TypeError, RecursionError):
        return {}, [
            ConfigIssue(
                "invalid_document",
                "",
                "Expected a bounded UTF-8 YAML object with JSON-compatible values.",
                source=source,
            )
        ]


def resolve_layers(
    layers: list[tuple[str, dict]], *, strict=True
) -> tuple[Config | None, list[ConfigIssue]]:
    schema = config_schema()
    issues = []
    for source, data in layers:
        issues.extend(
            replace(issue, source=source)
            for issue in shape_issues(
                data, schema, unknown="error" if strict else "warning"
            )
        )
    if any(issue.severity == "error" for issue in issues):
        return None, issues
    loader = ConfigLoader()
    merged = {}
    for _, data in layers:
        merged = loader._merge_dicts(merged, data)
    # Reject invalid explicit references before the legacy parser can select a fallback.
    models = merged.get("models", {})
    for key in (("active_main", "active_sub") if "active_main" in models else ("active", "active_sub")):
        if models.get(key) is not None and models[key] not in models.get(
            "profiles", {}
        ):
            issues.append(
                ConfigIssue(
                    "missing_profile",
                    f"/models/{key}",
                    "Selected model profile does not exist.",
                )
            )
    modes = merged.get("modes", {})
    from reuleauxcoder.domain.config.schema import BUILTIN_MODES

    if modes.get("active") is not None and modes["active"] not in (
        BUILTIN_MODES | modes.get("profiles", {})
    ):
        issues.append(
            ConfigIssue(
                "missing_mode", "/modes/active", "Selected mode does not exist."
            )
        )
    try:
        config = loader.parse_layers(layers)
        for message in config.validate():
            issues.append(ConfigIssue("invalid_config", "", message))
        LspConfig.from_config(config)
        for name, settings in [
            ("/app", config),
            *[
                (f"/models/profiles/{name}", profile)
                for name, profile in config.model_profiles.items()
            ],
        ]:
            if not settings.model.strip():
                issues.append(
                    ConfigIssue(
                        "missing_model",
                        name + "/model",
                        "Model name must not be empty.",
                    )
                )
            if settings.max_context_tokens < 1:
                issues.append(
                    ConfigIssue(
                        "invalid_limit",
                        name + "/max_context_tokens",
                        "Context capacity must be positive.",
                    )
                )
            if settings.base_url:
                parsed = urlsplit(settings.base_url)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    issues.append(
                        ConfigIssue(
                            "invalid_endpoint",
                            name + "/base_url",
                            "Use an HTTP(S) endpoint without embedded credentials, query or fragment.",
                        )
                    )
        for server in config.mcp_servers:
            if server.enabled and not server.command.strip():
                issues.append(
                    ConfigIssue(
                        "missing_command",
                        f"/mcp/servers/{server.name}/command",
                        "Enabled MCP server requires a command.",
                    )
                )
        for field in (
            "snip_keep_recent_tools",
            "snip_threshold_chars",
            "snip_min_lines",
            "summarize_keep_recent_turns",
        ):
            if getattr(config.context, field) < 0:
                issues.append(
                    ConfigIssue(
                        "invalid_limit",
                        f"/context/{field}",
                        "Value must not be negative.",
                    )
                )
        if config.context.token_fudge_factor <= 0:
            issues.append(
                ConfigIssue(
                    "invalid_limit",
                    "/context/token_fudge_factor",
                    "Value must be positive.",
                )
            )
        present = [values for _, values in layers if values]
        if present and all(
            ConfigLoader._is_example_config(values) for values in present
        ):
            issues.append(
                ConfigIssue(
                    "example_config",
                    "/meta/example",
                    "Remove the example marker before activating this configuration.",
                )
            )
    except (TypeError, ValueError, AttributeError, KeyError, OverflowError):
        issues.append(
            ConfigIssue(
                "invalid_config",
                "",
                "Configuration could not be constructed; check field types and limits.",
            )
        )
        config = None
    return config, issues
