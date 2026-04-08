"""Workspace config persistence adapter backed by YAML files."""

from __future__ import annotations

from pathlib import Path

from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


class WorkspaceConfigStore:
    """File-backed store for writable workspace configuration."""

    def __init__(self, path: Path | None = None):
        self._path = path or ConfigLoader.WORKSPACE_CONFIG_PATH

    @property
    def path(self) -> Path:
        """Return the writable workspace config path."""
        return self._path

    def save_approval_config(self, approval: ApprovalConfig) -> Path:
        """Persist approval config into workspace ``config.yaml``."""
        try:
            data = load_yaml_config(self._path)
        except FileNotFoundError:
            data = {}

        data["approval"] = {
            "default_mode": approval.default_mode,
            "rules": [self.approval_rule_to_dict(rule) for rule in approval.rules],
        }
        save_yaml_config(self._path, data)
        return self._path

    @staticmethod
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
