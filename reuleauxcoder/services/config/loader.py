"""Configuration loader - loads config.yaml with global + workspace merge."""

from copy import deepcopy
from pathlib import Path
from typing import Optional
import yaml

from reuleauxcoder.compat import migrate_bash_to_shell, migrate_legacy_config
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
    ContextConfig,
    MCPServerConfig,
    ModeConfig,
    ModelProfileConfig,
    PromptConfig,
    RemoteExecConfig,
    SkillsConfig,
)
from reuleauxcoder.domain.config.schema import (
    BUILTIN_MODES,
    DEFAULTS,
    DEFAULT_ACTIVE_MODE,
)
from reuleauxcoder.infrastructure.yaml.loader import save_yaml_config, load_yaml_config


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
        For profile maps (MCP/model/mode), merge by key (override wins for same key).
        """
        result = dict(base)

        for key, value in override.items():
            if key in {"mcp", "models", "modes"} and isinstance(value, dict):
                result_section = result.get(key, {})

                # Merge scalar routing fields with override priority
                for scalar_key in ("active", "active_main", "active_sub"):
                    if scalar_key in value:
                        result_section[scalar_key] = value[scalar_key]

                # Merge profile maps by name/key, override wins for same key
                if "servers" in value and isinstance(value.get("servers"), dict):
                    base_servers = result_section.get("servers", {})
                    result_section["servers"] = {**base_servers, **value["servers"]}
                if "profiles" in value and isinstance(value.get("profiles"), dict):
                    base_profiles = result_section.get("profiles", {})
                    override_profiles = value["profiles"]
                    merged_profiles = dict(base_profiles)
                    for profile_name, profile_value in override_profiles.items():
                        if isinstance(profile_value, dict) and isinstance(
                            base_profiles.get(profile_name), dict
                        ):
                            merged_profiles[profile_name] = self._merge_dicts(
                                base_profiles[profile_name],
                                profile_value,
                            )
                        else:
                            merged_profiles[profile_name] = profile_value
                    result_section["profiles"] = merged_profiles

                result[key] = result_section
            elif (
                isinstance(value, dict)
                and key in result
                and isinstance(result[key], dict)
            ):
                # Recursively merge nested dicts
                result[key] = self._merge_dicts(result[key], value)
            else:
                # Override wins
                result[key] = value

        return result

    def load(self) -> Config:
        """Load configuration with global + workspace merge."""
        # Start with builtin mode defaults
        config_data = {
            "modes": {
                "active": DEFAULT_ACTIVE_MODE,
                "profiles": dict(BUILTIN_MODES),
            }
        }

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

        # Require explicit model/runtime config even if builtin modes are present
        has_runtime_config = any(
            key in config_data and config_data.get(key) for key in ("models", "app")
        )
        if not has_runtime_config:
            raise FileNotFoundError(
                "No config.yaml found. "
                "Create one in ~/.rcoder/ (global) or ./.rcoder/ (workspace)"
            )

        migrated_data, _ = migrate_legacy_config(config_data)
        migrated_data, _ = migrate_bash_to_shell(migrated_data)
        self._bootstrap_workspace_snapshot(migrated_data, workspace_data)

        config = self._parse_config(migrated_data)
        self._backfill_workspace_modes(config)
        return config

    def _parse_config(self, data: dict) -> Config:
        """Parse YAML data into Config model."""
        app_config = data.get("app", {})
        approval_config = data.get("approval", {})
        tool_output_config = data.get("tool_output", {})
        session_config = data.get("session", {})
        cli_config = data.get("cli", {})
        mcp_config = data.get("mcp", {})
        models_config = data.get("models", {})
        modes_config = data.get("modes", {})
        skills_config = data.get("skills", {})
        prompt_config = data.get("prompt", {})
        context_config = data.get("context", {})
        remote_exec_config = data.get("remote_exec", {})

        # Parse MCP servers
        mcp_servers = []
        servers_data = mcp_config.get("servers", {})
        for name, server_data in servers_data.items():
            mcp_servers.append(MCPServerConfig.from_dict(name, server_data))

        # Parse model profiles
        model_profiles: dict[str, ModelProfileConfig] = {}
        profiles_data = models_config.get("profiles", {})
        for name, profile_data in profiles_data.items():
            if not isinstance(profile_data, dict):
                continue
            model_profiles[name] = ModelProfileConfig.from_dict(name, profile_data)

        active_main_model_profile = models_config.get("active_main")
        if (
            not isinstance(active_main_model_profile, str)
            or active_main_model_profile not in model_profiles
        ):
            active_main_model_profile = models_config.get("active")
        if (
            not isinstance(active_main_model_profile, str)
            or active_main_model_profile not in model_profiles
        ):
            active_main_model_profile = next(iter(model_profiles.keys()), None)

        active_sub_model_profile = models_config.get("active_sub")
        if (
            not isinstance(active_sub_model_profile, str)
            or active_sub_model_profile not in model_profiles
        ):
            active_sub_model_profile = active_main_model_profile

        # Backward compatibility alias: active_model_profile tracks main profile.
        active_model_profile = active_main_model_profile

        active_profile = (
            model_profiles.get(active_main_model_profile)
            if isinstance(active_main_model_profile, str)
            else None
        )

        # Parse modes (builtin modes already merged during load())
        modes: dict[str, ModeConfig] = {}
        mode_profiles_data = modes_config.get("profiles", {})
        for name, mode_data in mode_profiles_data.items():
            if not isinstance(mode_data, dict):
                continue
            modes[name] = ModeConfig.from_dict(name, mode_data)

        active_mode = modes_config.get("active")
        if not isinstance(active_mode, str) or active_mode not in modes:
            active_mode = (
                DEFAULT_ACTIVE_MODE
                if DEFAULT_ACTIVE_MODE in modes
                else next(iter(modes.keys()), None)
            )

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
            model=(
                active_profile.model
                if active_profile is not None
                else app_config.get("model", DEFAULTS["model"])
            ),
            api_key=(
                active_profile.api_key
                if active_profile is not None
                else app_config.get("api_key", "")
            ),
            base_url=(
                active_profile.base_url
                if active_profile is not None
                else app_config.get("base_url")
            ),
            max_tokens=(
                active_profile.max_tokens
                if active_profile is not None
                else app_config.get("max_tokens", DEFAULTS["max_tokens"])
            ),
            temperature=(
                active_profile.temperature
                if active_profile is not None
                else app_config.get("temperature", DEFAULTS["temperature"])
            ),
            max_context_tokens=(
                active_profile.max_context_tokens
                if active_profile is not None
                else app_config.get(
                    "max_context_tokens", DEFAULTS["max_context_tokens"]
                )
            ),
            preserve_reasoning_content=(
                active_profile.preserve_reasoning_content
                if active_profile is not None
                else app_config.get("preserve_reasoning_content", True)
            ),
            backfill_reasoning_content_for_tool_calls=(
                active_profile.backfill_reasoning_content_for_tool_calls
                if active_profile is not None
                else app_config.get("backfill_reasoning_content_for_tool_calls", False)
            ),
            reasoning_effort=(
                active_profile.reasoning_effort
                if active_profile is not None
                else app_config.get("reasoning_effort")
            ),
            thinking_enabled=(
                active_profile.thinking_enabled
                if active_profile is not None
                else app_config.get("thinking_enabled")
            ),
            reasoning_replay_mode=(
                active_profile.reasoning_replay_mode
                if active_profile is not None
                else app_config.get("reasoning_replay_mode")
            ),
            reasoning_replay_placeholder=(
                active_profile.reasoning_replay_placeholder
                if active_profile is not None
                else app_config.get("reasoning_replay_placeholder")
            ),
            mcp_servers=mcp_servers,
            model_profiles=model_profiles,
            active_model_profile=active_model_profile,
            active_main_model_profile=active_main_model_profile,
            active_sub_model_profile=active_sub_model_profile,
            modes=modes,
            active_mode=active_mode,
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
            skills=SkillsConfig(
                enabled=skills_config.get("enabled", True),
                scan_project=skills_config.get("scan_project", True),
                scan_user=skills_config.get("scan_user", True),
                disabled=[
                    str(name)
                    for name in skills_config.get("disabled", [])
                    if str(name).strip()
                ],
            ),
            prompt=PromptConfig(
                system_append=str(prompt_config.get("system_append", "") or ""),
            ),
            context=ContextConfig(
                snip_keep_recent_tools=context_config.get(
                    "snip_keep_recent_tools", DEFAULTS["snip_keep_recent_tools"]
                ),
                snip_threshold_chars=context_config.get(
                    "snip_threshold_chars", DEFAULTS["snip_threshold_chars"]
                ),
                snip_min_lines=context_config.get(
                    "snip_min_lines", DEFAULTS["snip_min_lines"]
                ),
                summarize_keep_recent_turns=context_config.get(
                    "summarize_keep_recent_turns",
                    DEFAULTS["summarize_keep_recent_turns"],
                ),
            ),
            remote_exec=RemoteExecConfig(
                enabled=bool(remote_exec_config.get("enabled", False)),
                host_mode=bool(remote_exec_config.get("host_mode", False)),
                relay_bind=str(remote_exec_config.get("relay_bind", "127.0.0.1:8765")),
                bootstrap_access_secret=str(
                    remote_exec_config.get("bootstrap_access_secret", "")
                ),
                bootstrap_token_ttl_sec=int(
                    remote_exec_config.get("bootstrap_token_ttl_sec", 300)
                ),
                peer_token_ttl_sec=int(
                    remote_exec_config.get("peer_token_ttl_sec", 3600)
                ),
                heartbeat_interval_sec=int(
                    remote_exec_config.get("heartbeat_interval_sec", 10)
                ),
                heartbeat_timeout_sec=int(
                    remote_exec_config.get("heartbeat_timeout_sec", 30)
                ),
                default_tool_timeout_sec=int(
                    remote_exec_config.get("default_tool_timeout_sec", 30)
                ),
                shell_timeout_sec=int(remote_exec_config.get("shell_timeout_sec", 120)),
            ),
            session_auto_save=session_config.get(
                "auto_save", DEFAULTS["session_auto_save"]
            ),
            session_dir=session_config.get("dir"),
            history_file=cli_config.get("history_file"),
            llm_debug_trace=bool(
                app_config.get("llm_debug_trace", DEFAULTS["llm_debug_trace"])
            ),
        )

    def _backfill_workspace_modes(self, config: Config) -> None:
        """Backfill builtin mode defaults into workspace config for discoverability."""
        path = self.WORKSPACE_CONFIG_PATH

        try:
            workspace_data = load_yaml_config(path)
        except FileNotFoundError:
            workspace_data = {}

        modes_data = workspace_data.get("modes")
        profiles_data = (
            modes_data.get("profiles") if isinstance(modes_data, dict) else None
        )
        has_active = isinstance(modes_data, dict) and isinstance(
            modes_data.get("active"), str
        )

        needs_write = (
            not isinstance(profiles_data, dict) or not profiles_data or not has_active
        )
        if not needs_write:
            return

        workspace_data.setdefault("modes", {})["active"] = (
            config.active_mode or DEFAULT_ACTIVE_MODE
        )
        workspace_data["modes"]["profiles"] = {
            name: {
                "description": mode.description,
                "tools": list(mode.tools),
                "prompt_append": mode.prompt_append,
                "allowed_subagent_modes": list(mode.allowed_subagent_modes),
            }
            for name, mode in sorted(config.modes.items())
        }
        save_yaml_config(path, workspace_data)

    def _bootstrap_workspace_snapshot(
        self, merged_data: dict, workspace_data: dict
    ) -> None:
        """Write merged config snapshot into workspace once for single-file editing."""
        modes_data = (
            workspace_data.get("modes") if isinstance(workspace_data, dict) else None
        )
        profiles_data = (
            modes_data.get("profiles") if isinstance(modes_data, dict) else None
        )
        has_active_mode = isinstance(modes_data, dict) and isinstance(
            modes_data.get("active"), str
        )

        meta_data = (
            workspace_data.get("meta") if isinstance(workspace_data, dict) else None
        )
        bootstrapped = isinstance(meta_data, dict) and bool(
            meta_data.get("workspace_bootstrapped")
        )

        needs_bootstrap = (
            not workspace_data
            or not isinstance(profiles_data, dict)
            or not profiles_data
            or not has_active_mode
        )
        if bootstrapped or not needs_bootstrap:
            return

        snapshot = deepcopy(merged_data)
        snapshot.setdefault("meta", {})["workspace_bootstrapped"] = True
        save_yaml_config(self.WORKSPACE_CONFIG_PATH, snapshot)

    @classmethod
    def from_path(cls, path: Optional[Path] = None) -> Config:
        """Convenience method to load config from a path."""
        loader = cls(path)
        return loader.load()
