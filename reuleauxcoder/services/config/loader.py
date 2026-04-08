"""Configuration loader - loads config.yaml with global + workspace merge."""

from pathlib import Path
from typing import Optional
import yaml

from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
    MCPServerConfig,
)
from reuleauxcoder.domain.config.schema import DEFAULTS


class ConfigLoader:
    """Loads configuration from config.yaml.

    Configuration priority (later overrides earlier):
    1. Global config: ~/.rcoder/config.yaml
    2. Workspace config: ./.rcoder/config.yaml
    3. Explicit path: --config argument
    """

    GLOBAL_CONFIG_PATH = Path.home() / ".rcoder" / "config.yaml"
    WORKSPACE_CONFIG_PATH = Path.cwd() / ".rcoder" / "config.yaml"

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path

    def _load_yaml(self, path: Path) -> dict:
        """Load YAML file, return empty dict if not exists or invalid."""
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
                return data if data else {}
        except (yaml.YAMLError, IOError):
            return {}

    def _merge_dicts(self, base: dict, override: dict) -> dict:
        """Merge two dicts, override takes priority.
        
        For nested dicts, merge recursively.
        For MCP servers, merge by name (override wins for same name).
        """
        result = dict(base)
        
        for key, value in override.items():
            if key == "mcp" and "servers" in value:
                # Special handling for MCP servers: merge by name
                result_mcp = result.get("mcp", {})
                result_servers = result_mcp.get("servers", {})
                override_servers = value.get("servers", {})
                # Merge servers, override wins for same name
                merged_servers = {**result_servers, **override_servers}
                result["mcp"] = {"servers": merged_servers}
            elif isinstance(value, dict) and key in result and isinstance(result[key], dict):
                # Recursively merge nested dicts
                result[key] = self._merge_dicts(result[key], value)
            else:
                # Override wins
                result[key] = value
        
        return result

    def load(self) -> Config:
        """Load configuration with global + workspace merge."""
        # Start with defaults
        config_data = {}
        
        # Load global config
        global_data = self._load_yaml(self.GLOBAL_CONFIG_PATH)
        if global_data:
            config_data = self._merge_dicts(config_data, global_data)
        
        # Load workspace config (overrides global)
        workspace_data = self._load_yaml(self.WORKSPACE_CONFIG_PATH)
        if workspace_data:
            config_data = self._merge_dicts(config_data, workspace_data)
        
        # Load explicit config path (overrides everything)
        if self.config_path:
            explicit_data = self._load_yaml(self.config_path)
            if explicit_data:
                config_data = self._merge_dicts(config_data, explicit_data)
        
        # Check if we have any config at all
        if not config_data:
            raise FileNotFoundError(
                "No config.yaml found. "
                "Create one in ~/.rcoder/ (global) or ./.rcoder/ (workspace)"
            )

        return self._parse_config(config_data)

    def _parse_config(self, data: dict) -> Config:
        """Parse YAML data into Config model."""
        app_config = data.get("app", {})
        approval_config = data.get("approval", {})
        tool_output_config = data.get("tool_output", {})
        session_config = data.get("session", {})
        cli_config = data.get("cli", {})
        mcp_config = data.get("mcp", {})

        # Parse MCP servers
        mcp_servers = []
        servers_data = mcp_config.get("servers", {})
        for name, server_data in servers_data.items():
            mcp_servers.append(MCPServerConfig.from_dict(name, server_data))

        approval_rules = [
            ApprovalRuleConfig(
                tool_name=rule.get("tool_name"),
                tool_source=rule.get("tool_source"),
                mcp_server=rule.get("mcp_server"),
                effect_class=rule.get("effect_class"),
                profile=rule.get("profile"),
                action=rule.get("action", "require_approval"),
            )
            for rule in approval_config.get("rules", DEFAULTS["approval_rules"])
        ]

        return Config(
            model=app_config.get("model", DEFAULTS["model"]),
            api_key=app_config.get("api_key", ""),
            base_url=app_config.get("base_url"),
            max_tokens=app_config.get("max_tokens", DEFAULTS["max_tokens"]),
            temperature=app_config.get("temperature", DEFAULTS["temperature"]),
            max_context_tokens=app_config.get(
                "max_context_tokens", DEFAULTS["max_context_tokens"]
            ),
            mcp_servers=mcp_servers,
            tool_output_max_chars=tool_output_config.get(
                "max_chars", DEFAULTS["tool_output_max_chars"]
            ),
            tool_output_max_lines=tool_output_config.get(
                "max_lines", DEFAULTS["tool_output_max_lines"]
            ),
            tool_output_store_full=tool_output_config.get(
                "store_full_output", DEFAULTS["tool_output_store_full"]
            ),
            tool_output_store_dir=tool_output_config.get(
                "store_dir", DEFAULTS["tool_output_store_dir"]
            ),
            approval=ApprovalConfig(
                default_mode=approval_config.get(
                    "default_mode", DEFAULTS["approval_default_mode"]
                ),
                rules=approval_rules,
            ),
            session_auto_save=session_config.get(
                "auto_save", DEFAULTS["session_auto_save"]
            ),
            session_dir=session_config.get("dir"),
            history_file=cli_config.get("history_file"),
        )

    @classmethod
    def from_path(cls, path: Optional[Path] = None) -> Config:
        """Convenience method to load config from a path."""
        loader = cls(path)
        return loader.load()
