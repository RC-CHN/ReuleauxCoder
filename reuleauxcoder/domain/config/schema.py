"""Configuration defaults and builtin mode profiles."""

# Default values for configuration
BUILTIN_MODES = {
    "coder": {
        "description": "Default coding mode with full tool access.",
        "tools": ["*"],
        "prompt_append": (
            "Prioritize making concrete code changes and verifying them with commands/tests "
            "when appropriate."
        ),
        "allowed_subagent_modes": ["explore", "execute", "verify"],
    },
    "planner": {
        "description": "Planning-first mode; focus on analysis and implementation plans.",
        "tools": [
            "read_file",
            "glob",
            "grep",
            "history_search",
            "history_read",
            "artifact_read",
            "update_plan",
            "report_progress",
            "spawn_agent",
            "send_message",
            "list_agents",
            "wait_agent",
            "interrupt_agent",
        ],
        "prompt_append": (
            "Focus on analysis, architecture, and step-by-step plans. Avoid file mutations "
            "unless explicitly requested."
        ),
        "allowed_subagent_modes": ["explore"],
    },
    "debugger": {
        "description": "Debugging mode focused on diagnosis and verification.",
        "tools": [
            "read_file",
            "glob",
            "grep",
            "history_search",
            "history_read",
            "artifact_read",
            "update_plan",
            "report_progress",
            "shell",
            "spawn_agent",
            "send_message",
            "list_agents",
            "wait_agent",
            "interrupt_agent",
        ],
        "prompt_append": (
            "Focus on root-cause analysis, minimal repro steps, and targeted fixes with "
            "clear verification."
        ),
        "allowed_subagent_modes": ["explore", "verify"],
    },
}

DEFAULT_ACTIVE_MODE = "coder"


# Default values for configuration
DEFAULTS = {
    "model": "gpt-4o",
    "provider": "openai-compatible",
    "max_tokens": 4096,
    "temperature": 0.0,
    "max_context_tokens": 128_000,
    "approval_default_mode": "require_approval",
    "approval_rules": [
        {"tool_name": "read_file", "action": "allow"},
        {"tool_name": "write_note", "action": "allow"},
        {"tool_name": "edit_note", "action": "allow"},
        {"tool_name": "delete_note", "action": "allow"},
        {"tool_name": "glob", "action": "allow"},
        {"tool_name": "grep", "action": "allow"},
        {"tool_name": "list_file", "action": "allow"},
        {"tool_name": "history_search", "action": "allow"},
        {"tool_name": "history_read", "action": "allow"},
        {"tool_name": "artifact_read", "action": "allow"},
        {"tool_name": "lsp", "action": "allow"},
        {"tool_name": "lsp_status", "action": "allow"},
        {"tool_name": "lsp_diagnostics", "action": "allow"},
        {"tool_name": "lsp_restart", "action": "allow"},
        {"tool_name": "web_fetch", "action": "warn"},
        {"tool_name": "web_search", "action": "warn"},
        {"tool_name": "write_file", "action": "require_approval"},
        {"tool_name": "edit_file", "action": "require_approval"},
        {"tool_name": "shell", "action": "require_approval"},
        {"tool_name": "spawn_agent", "action": "require_approval"},
        {"tool_source": "mcp", "mcp_server": "filesystem", "action": "warn"},
        {"tool_source": "mcp", "action": "require_approval"},
    ],
    "tool_output_max_chars": 12_000,
    "tool_output_max_lines": 120,
    "tool_output_store_full": True,
    "tool_output_store_dir": None,
    "web_enabled": True,
    "web_search_provider": "auto",
    "web_allow_private_networks": True,
    "shell_rtk": "off",  # "auto" | "on" | "off"
    "notes_workspace_max": 30,
    "notes_global_max": 20,
    "notes_inject": True,
    "session_auto_save": True,
    "session_dir": None,  # Will be computed at runtime
    "history_file": None,  # Will be computed at runtime
    "llm_debug_trace": False,
    "ui_verbosity": "compact",
    "ui_tool_output": "summary",
    "ui_max_preview_lines": 20,
    "ui_max_preview_chars": 1_200,
    "ui_show_tool_args": True,
    "ui_reasoning_display": "indicator",
    "ui_notification_threshold": "info",
    "snip_keep_recent_tools": 2,
    "snip_threshold_chars": 1500,
    "snip_min_lines": 6,
    "summarize_keep_recent_turns": 5,
    "token_fudge_factor": 1.1,
    "reserved_output_tokens": 8192,
    "fixed_prompt_tokens": 0,
    "tool_schema_tokens": 0,
    "safety_margin_tokens": 2048,
}
