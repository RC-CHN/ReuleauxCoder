"""Resolved model identities and referenced-policy protection for management."""

from dataclasses import asdict
import hashlib
import json

from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.domain.config.models import ModelProfileConfig
from reuleauxcoder.services.config.loader import ConfigLoader


def model_targets(config) -> list[dict]:
    if config is None:
        return []
    targets = []
    for name, settings in config.model_profiles.items():
        values = asdict(settings)
        values.pop("name")
        values.pop("context")
        values["request_mode"] = settings.request_mode or (
            "messages" if settings.provider == "anthropic" else "chat-completions"
        )
        roles = []
        if name == config.active_main_model_profile:
            roles.append("main")
        if name == config.active_sub_model_profile:
            roles.append("subagent")
        if config.approval.reviewer == "auto_review" and name == config.approval.auto_review_model_profile:
            roles.append("reviewer")
        targets.append({
            "profile": name, "roles": roles,
            "fingerprint": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
            **{field: values[field] for field in (
                "model", "provider", "request_mode", "base_url", "max_tokens",
                "reasoning_effort", "thinking_enabled", "reasoning_effort_param", "reasoning_effort_values",
            )},
        })
    return targets


def changed_targets(before, after) -> list[dict]:
    previous = {item["profile"]: item for item in model_targets(before)}
    return [item for item in model_targets(after) if (
        item["profile"] not in previous
        or item["fingerprint"] != previous[item["profile"]]["fingerprint"]
        or set(item["roles"]) - set(previous[item["profile"]]["roles"])
    )]


def _reviewer(layers):
    loader = ConfigLoader()
    merged = {}
    for _, values in layers:
        merged = loader._merge_dicts(merged, values)
    merged, _ = loader._migrate_config(merged)
    policy = merged.get("approval", {})
    if policy.get("reviewer", "user") != "auto_review":
        return None
    name = policy.get("auto_review_model_profile")
    raw = merged.get("models", {}).get("profiles", {}).get(name)
    if not isinstance(raw, dict):
        raise ValueError("Missing reviewer profile")
    defaults = {key: value for key, value in merged.get("app", {}).items() if key in loader._LLM_PARAM_FIELDS}
    return name, asdict(ModelProfileConfig.from_dict(name, loader._merge_dicts(defaults, raw)))


def protect_reviewer(before_layers, after_layers) -> None:
    """Compare the effective referenced model, even across different YAML files."""
    try:
        unchanged = _reviewer(before_layers) == _reviewer(after_layers)
    except (ValueError, TypeError, AttributeError, KeyError):
        unchanged = False
    if not unchanged:
        raise ConfigOperationError(
            "user_required",
            "The effective automatic reviewer model may only be changed through a human management interface.",
        )
