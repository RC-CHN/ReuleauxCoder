"""Approval rule persistence helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


def get_workspace_config_path() -> Path:
    """Return the writable workspace config path."""
    return ConfigLoader.WORKSPACE_CONFIG_PATH


def approval_rule_to_dict(rule: ApprovalRuleConfig) -> dict:
    """Serialize an approval rule, dropping empty fields."""
    data = {
        "tool_name": rule.tool_name,
        "tool_source": rule.tool_source,
        "mcp_server": rule.mcp_server,
        "effect_class": rule.effect_class,
        "profile": rule.profile,
        "action": rule.action,
    }
    return {k: v for k, v in data.items() if v is not None}


def save_approval_config(approval: ApprovalConfig, path: Optional[Path] = None) -> Path:
    """Persist approval config into workspace config.yaml."""
    config_path = path or get_workspace_config_path()
    try:
        data = load_yaml_config(config_path)
    except FileNotFoundError:
        data = {}

    data["approval"] = {
        "default_mode": approval.default_mode,
        "rules": [approval_rule_to_dict(rule) for rule in approval.rules],
    }
    save_yaml_config(config_path, data)
    return config_path
