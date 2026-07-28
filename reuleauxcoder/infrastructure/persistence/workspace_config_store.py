"""Workspace config persistence adapter backed by YAML files."""

from __future__ import annotations

from pathlib import Path

from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    MCPServerConfig,
)
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
            "reviewer": approval.reviewer,
            "auto_review_model_profile": approval.auto_review_model_profile,
            "auto_review_policy": approval.auto_review_policy,
            "auto_review_timeout_seconds": approval.auto_review_timeout_seconds,
            "rules": [self.approval_rule_to_dict(rule) for rule in approval.rules],
        }
        save_yaml_config(self._path, data)
        return self._path

    def save_active_model_profile(self, profile_name: str) -> Path:
        """Persist active main model profile into workspace ``config.yaml``."""
        try:
            data = load_yaml_config(self._path)
        except FileNotFoundError:
            data = {}

        models_data = data.setdefault("models", {})
        models_data["active"] = profile_name
        models_data["active_main"] = profile_name
        save_yaml_config(self._path, data)
        return self._path

    def save_active_sub_model_profile(self, profile_name: str) -> Path:
        """Persist active sub-agent model profile into workspace ``config.yaml``."""
        try:
            data = load_yaml_config(self._path)
        except FileNotFoundError:
            data = {}

        models_data = data.setdefault("models", {})
        models_data["active_sub"] = profile_name
        save_yaml_config(self._path, data)
        return self._path

    def save_active_mode(self, mode_name: str) -> Path:
        """Persist active mode into workspace ``config.yaml``."""
        try:
            data = load_yaml_config(self._path)
        except FileNotFoundError:
            data = {}

        modes_data = data.setdefault("modes", {})
        modes_data["active"] = mode_name
        save_yaml_config(self._path, data)
        return self._path

    def save_mcp_server_config(self, server: MCPServerConfig) -> Path:
        """Persist a single MCP server config into workspace ``config.yaml``."""
        try:
            data = load_yaml_config(self._path)
        except FileNotFoundError:
            data = {}

        mcp_data = data.setdefault("mcp", {})
        servers = mcp_data.setdefault("servers", {})
        servers[server.name] = server.to_dict()
        save_yaml_config(self._path, data)
        return self._path

    def save_mcp_server_enabled(self, server_name: str, enabled: bool) -> Path:
        """Persist only the workspace-local enabled override for an MCP server."""
        try:
            data = load_yaml_config(self._path)
        except FileNotFoundError:
            data = {}

        mcp_data = data.setdefault("mcp", {})
        servers = mcp_data.setdefault("servers", {})
        server_data = servers.setdefault(server_name, {})
        if not isinstance(server_data, dict):
            server_data = {}
            servers[server_name] = server_data
        server_data["enabled"] = enabled
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
            "pattern": rule.pattern,
            "scope_key": rule.scope_key,
            "action": rule.action,
        }
        return {k: v for k, v in data.items() if v is not None}
