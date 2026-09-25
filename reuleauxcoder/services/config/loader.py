"""Configuration loader - loads config.yaml with global + workspace merge."""

from copy import deepcopy
from pathlib import Path
from typing import Literal, Optional, cast

from reuleauxcoder.compat import migrate_bash_to_shell, migrate_legacy_config
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
    ConfigDiagnostic,
    ContextConfig,
    MCPServerConfig,
    ModeConfig,
    ModelProfileConfig,
    PromptConfig,
    RemoteExecConfig,
    SkillsConfig,
    UIConfig,
)
from reuleauxcoder.domain.config.schema import (
    BUILTIN_MODES,
    DEFAULTS,
    DEFAULT_ACTIVE_MODE,
)
from reuleauxcoder.domain.images import ImageConfig


class ConfigLoader:
    """Loads configuration from config.yaml.

    Configuration priority (later overrides earlier):
    1. Global config: ~/.rcoder/config.yaml
    2. Workspace config: ./.rcoder/config.yaml
    3. Explicit path: --config argument
    """

    GLOBAL_CONFIG_PATH = Path.home() / ".rcoder" / "config.yaml"
    WORKSPACE_CONFIG_PATH = Path.cwd() / ".rcoder" / "config.yaml"

    # LLM params that support active_profile → app_config → DEFAULTS priority.
    _LLM_PARAM_FIELDS = [
        "model",
        "api_key",
        "provider",
        "request_mode",
        "responses",
        "support_modal",
        "base_url",
        "max_tokens",
        "temperature",
        "max_context_tokens",
        "preserve_reasoning_content",
        "backfill_reasoning_content_for_tool_calls",
        "reasoning_effort",
        "thinking_enabled",
        "reasoning_replay_mode",
        "reasoning_replay_placeholder",
        "reasoning_effort_values",
        "reasoning_effort_param",
    ]

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path
        self._effective_sources: dict[str, str] = {}

    def _record_sources(self, value: object, source: str, prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                self._record_sources(child, source, path)
            return
        if prefix:
            self._effective_sources[prefix] = source

    def _resolve_llm_params(self, active_profile, app_config: dict) -> dict:
        """Use the same resolved profile for startup, probes and session changes.

        Omitted profile fields inherit app before constructing defaults. An
        explicit null on a nullable field deliberately clears that inheritance.
        """
        profile = active_profile or ModelProfileConfig.from_dict("default", app_config)
        return {name: getattr(profile, name) for name in self._LLM_PARAM_FIELDS}

    def _load_yaml(self, path: Path) -> dict:
        """Distinguish missing files from invalid configuration; never erase errors."""
        from reuleauxcoder.domain.config.management import ConfigOperationError
        from reuleauxcoder.services.config.validation import MAX_CONFIG_BYTES, parse_document

        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise ConfigOperationError("unreadable_config", f"Cannot read configuration: {path}") from error
        data, issues = parse_document(content, str(path))
        if issues:
            issue = issues[0]
            location = f"{path}:{issue.line}" if issue.line else str(path)
            raise ConfigOperationError(issue.code, f"{location}: {issue.message} Run rcoder config check to diagnose or repair it.")
        return data

    def _merge_dicts(self, base: dict, override: dict) -> dict:
        """Merge two dicts, override takes priority.

        For nested dicts, merge recursively.
        For profile maps (MCP/model/mode), merge by key (override wins for same key).
        """
        result = deepcopy(base)

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
                    override_servers = value["servers"]
                    merged_servers = dict(base_servers)
                    for server_name, server_value in override_servers.items():
                        if isinstance(server_value, dict) and isinstance(
                            base_servers.get(server_name), dict
                        ):
                            merged_servers[server_name] = self._merge_dicts(
                                base_servers[server_name], server_value
                            )
                        else:
                            merged_servers[server_name] = server_value
                    result_section["servers"] = merged_servers
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

    def parse_layers(self, layers: list[tuple[str, dict]]) -> Config:
        """Resolve explicit input layers without reading, generating or writing files."""
        data = {"modes": {"active": DEFAULT_ACTIVE_MODE, "profiles": deepcopy(BUILTIN_MODES)}}
        self._effective_sources = {}
        for source, values in layers:
            self._record_sources(values, source)
            data = self._merge_dicts(data, values)
        migrated, diagnostics = self._migrate_config(data)
        migrated, _ = migrate_bash_to_shell(migrated)
        config = self._parse_config(migrated)
        config.diagnostics[:0] = diagnostics
        return config

    def load(self) -> Config:
        """Read and validate the same layered configuration as CLI and editor checks."""
        from reuleauxcoder.domain.config.management import ConfigOperationError
        from reuleauxcoder.services.config.validation import resolve_layers

        layers = [(name, self._load_yaml(path)) for name, path in (
            ("user", self.GLOBAL_CONFIG_PATH), ("workspace", self.WORKSPACE_CONFIG_PATH),
            *([("explicit", self.config_path)] if self.config_path else []),
        )]
        config, issues = resolve_layers(layers)
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            issue = errors[0]
            raise ConfigOperationError(issue.code, f"{issue.source or 'configuration'}{issue.path}: {issue.message} Run rcoder config check for details.")
        return config

    def _migrate_config(self, data: dict) -> tuple[dict, list[ConfigDiagnostic]]:
        raw_models = data.get("models")
        models: dict = raw_models if isinstance(raw_models, dict) else {}
        uses_legacy_active = "active" in models and "active_main" not in models
        migrated, _ = migrate_legacy_config(data)
        diagnostics = []
        if uses_legacy_active:
            diagnostics.append(
                ConfigDiagnostic(
                    code="legacy_config_alias",
                    path="models.active",
                    message="models.active was migrated to models.active_main.",
                    severity="info",
                    source=self._effective_sources.get("models.active"),
                )
            )
        return migrated, diagnostics

    def _parse_config(self, data: dict) -> Config:
        """Parse YAML data into Config model."""
        app_config = data.get("app", {})
        approval_config = data.get("approval", {})
        tool_output_config = data.get("tool_output", {})
        session_config = data.get("session", {})
        shell_config = data.get("shell", {})
        cli_config = data.get("cli", {})
        ui_config = data.get("ui", {})
        mcp_config = data.get("mcp", {})
        models_config = data.get("models", {})
        modes_config = data.get("modes", {})
        skills_config = data.get("skills", {})
        prompt_config = data.get("prompt", {})
        context_config = data.get("context", {})
        remote_exec_config = data.get("remote_exec", {})
        lsp_config = data.get("lsp", {})
        web_config = data.get("web", {})
        diagnostics: list[ConfigDiagnostic] = []

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
            defaults = {
                key: value for key, value in app_config.items()
                if key in self._LLM_PARAM_FIELDS
            }
            model_profiles[name] = ModelProfileConfig.from_dict(
                name, self._merge_dicts(defaults, profile_data)
            )

        requested_main = models_config.get("active_main")
        active_main_model_profile = requested_main
        if (
            not isinstance(active_main_model_profile, str)
            or active_main_model_profile not in model_profiles
        ):
            if requested_main is not None:
                diagnostics.append(
                    ConfigDiagnostic(
                        code="invalid_model_profile",
                        path="models.active_main",
                        message=(
                            f"Unknown main model profile '{requested_main}'; "
                            "using a compatible fallback."
                        ),
                        source=self._effective_sources.get("models.active_main"),
                    )
                )
        if (
            not isinstance(active_main_model_profile, str)
            or active_main_model_profile not in model_profiles
        ):
            active_main_model_profile = next(iter(model_profiles.keys()), None)

        requested_sub = models_config.get("active_sub")
        active_sub_model_profile = requested_sub
        if (
            not isinstance(active_sub_model_profile, str)
            or active_sub_model_profile not in model_profiles
        ):
            if requested_sub is not None:
                diagnostics.append(
                    ConfigDiagnostic(
                        code="invalid_model_profile",
                        path="models.active_sub",
                        message=(
                            f"Unknown subagent model profile '{requested_sub}'; "
                            "using the effective main profile."
                        ),
                        source=self._effective_sources.get("models.active_sub"),
                    )
                )
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
            ApprovalRuleConfig.from_dict(rule)
            for rule in approval_config.get("rules", DEFAULTS["approval_rules"])
        ]

        llm_params = self._resolve_llm_params(active_profile, app_config)

        return Config(
            **llm_params,
            image=ImageConfig(**data.get("attachments", {}).get("image", {})),
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
            web_enabled=bool(web_config.get("enabled", DEFAULTS["web_enabled"])),
            web_proxy=web_config.get("proxy", DEFAULTS["web_proxy"]),
            web_search_provider=str(
                web_config.get("search_provider", DEFAULTS["web_search_provider"])
            ),
            web_allow_private_networks=bool(
                web_config.get(
                    "allow_private_networks",
                    DEFAULTS["web_allow_private_networks"],
                )
            ),
            approval=ApprovalConfig(
                default_mode=approval_config.get(
                    "default_mode", DEFAULTS["approval_default_mode"]
                ),
                rules=approval_rules,
                reviewer=cast(
                    Literal["user", "auto_review"],
                    str(approval_config.get("reviewer", "user")),
                ),
                auto_review_model_profile=approval_config.get(
                    "auto_review_model_profile"
                ),
                auto_review_policy=str(
                    approval_config.get("auto_review_policy", "") or ""
                ),
                auto_review_timeout_seconds=int(
                    approval_config.get("auto_review_timeout_seconds", 15)
                ),
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
                image_retention=context_config.get("image_retention", "history"),
                auto_snip=bool(context_config.get("auto_snip", DEFAULTS["auto_snip"])),
                auto_summarize=bool(
                    context_config.get(
                        "auto_summarize", DEFAULTS["auto_summarize"]
                    )
                ),
                auto_collapse=bool(
                    context_config.get(
                        "auto_collapse", DEFAULTS["auto_collapse"]
                    )
                ),
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
                token_fudge_factor=context_config.get(
                    "token_fudge_factor", DEFAULTS["token_fudge_factor"]
                ),
                reserved_output_tokens=context_config.get(
                    "reserved_output_tokens", DEFAULTS["reserved_output_tokens"]
                ),
                fixed_prompt_tokens=context_config.get(
                    "fixed_prompt_tokens", DEFAULTS["fixed_prompt_tokens"]
                ),
                tool_schema_tokens=context_config.get(
                    "tool_schema_tokens", DEFAULTS["tool_schema_tokens"]
                ),
                safety_margin_tokens=context_config.get(
                    "safety_margin_tokens", DEFAULTS["safety_margin_tokens"]
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
            goal_default_token_budget=(data.get("goal") or {}).get(
                "default_token_budget"
            ),
            session_dir=session_config.get("dir"),
            history_file=cli_config.get("history_file"),
            llm_debug_trace=bool(
                app_config.get("llm_debug_trace", DEFAULTS["llm_debug_trace"])
            ),
            ui=UIConfig(
                verbosity=ui_config.get("verbosity", DEFAULTS["ui_verbosity"]),
                tool_output=ui_config.get("tool_output", DEFAULTS["ui_tool_output"]),
                max_preview_lines=int(
                    ui_config.get("max_preview_lines", DEFAULTS["ui_max_preview_lines"])
                ),
                max_preview_chars=int(
                    ui_config.get("max_preview_chars", DEFAULTS["ui_max_preview_chars"])
                ),
                show_tool_args=bool(
                    ui_config.get("show_tool_args", DEFAULTS["ui_show_tool_args"])
                ),
                reasoning_display=ui_config.get(
                    "reasoning_display", DEFAULTS["ui_reasoning_display"]
                ),
                notification_threshold=ui_config.get(
                    "notification_threshold",
                    DEFAULTS["ui_notification_threshold"],
                ),
            ),
            lsp=lsp_config if lsp_config else None,
            diagnostics=diagnostics,
            effective_sources=dict(self._effective_sources),
            shell_rtk=shell_config.get("rtk", DEFAULTS["shell_rtk"]),
        )

    @staticmethod
    def _is_example_config(global_data: dict) -> bool:
        """Check whether the global config is the unedited example template."""
        if not isinstance(global_data, dict):
            return False
        meta = global_data.get("meta")
        return isinstance(meta, dict) and bool(meta.get("example"))

    @classmethod
    def from_path(cls, path: Optional[Path] = None) -> Config:
        """Convenience method to load config from a path."""
        loader = cls(path)
        return loader.load()
