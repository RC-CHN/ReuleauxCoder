"""Configuration schema - YAML structure definition."""

# Expected YAML structure for config.yaml
CONFIG_SCHEMA = {
    "models": {
        "active": "string (optional, legacy alias of active_main, defaults to first profile key)",
        "active_main": "string (optional, defaults to active or first profile key)",
        "active_sub": "string (optional, defaults to active_main)",
        "profiles": {
            "profile_name": {
                "model": "string (required)",
                "api_key": "string (required)",
                "base_url": "string (optional)",
                "max_tokens": "int (default: 4096)",
                "temperature": "float (default: 0.0)",
                "max_context_tokens": "int (default: 128000)",
                "preserve_reasoning_content": "bool (default: true, persist/round-trip provider reasoning_content)",
                "backfill_reasoning_content_for_tool_calls": "bool (default: false, inject empty reasoning_content for assistant tool calls when missing)",
                "thinking_enabled": "bool (optional, enable provider thinking/reasoning mode for this profile)",
                "reasoning_effort": "string (optional, provider-specific reasoning effort, e.g. high/max)",
                "reasoning_replay_mode": "string (optional, one of: none, tool_calls; controls which historical assistant reasoning_content is replayed)",
                "reasoning_replay_placeholder": "string (optional, placeholder text injected when backfilling missing reasoning_content; default: [PLACE_HOLDER])",
            }
        },
    },
    "modes": {
        "active": "string (optional, default: coder)",
        "profiles": {
            "mode_name": {
                "description": "string (optional)",
                "tools": "list of strings (optional, default: all tools)",
                "prompt_append": "string (optional)",
                "allowed_subagent_modes": "list of strings (optional)",
            }
        },
    },
    "app": {
        "model": "string (legacy, auto-migrated)",
        "api_key": "string (legacy, auto-migrated)",
        "base_url": "string (legacy, auto-migrated)",
        "max_tokens": "int (legacy, auto-migrated)",
        "temperature": "float (legacy, auto-migrated)",
        "max_context_tokens": "int (legacy, auto-migrated)",
    },
    "approval": {
        "default_mode": "string (default: require_approval, one of allow/warn/require_approval/deny)",
        "rules": [
            {
                "tool_name": "string (optional)",
                "tool_source": "string (optional)",
                "mcp_server": "string (optional)",
                "effect_class": "string (optional)",
                "profile": "string (optional)",
                "action": "string (required, one of allow/warn/require_approval/deny)",
            }
        ],
    },
    "tool_output": {
        "max_chars": "int (default: 12000)",
        "max_lines": "int (default: 120)",
        "store_full_output": "bool (default: true)",
        "store_dir": "string (optional, default: ./.rcoder/tool-outputs, fallback ~/.rcoder/tool-outputs)",
    },
    "session": {
        "auto_save": "bool (default: true)",
        "dir": "string (optional, default: ./.rcoder/sessions, fallback ~/.rcoder/sessions)",
    },
    "cli": {
        "history_file": "string (optional, default: ~/.rcoder/history)",
    },
    "skills": {
        "enabled": "bool (default: true)",
        "scan_project": "bool (default: true)",
        "scan_user": "bool (default: true)",
        "disabled": ["skill-name", "..."],
    },
    "prompt": {
        "system_append": "string (optional, appended to system prompt as user/workspace instructions)",
    },
    "context": {
        "snip_keep_recent_tools": "int (default: 5, number of recent tool calls to protect from snipping)",
        "snip_threshold_chars": "int (default: 1500, min content length to trigger snip)",
        "snip_min_lines": "int (default: 6, min line count to trigger snip)",
        "summarize_keep_recent_turns": "int (default: 5, number of recent user turns to protect during summarize)",
    },
    "mcp": {
        "servers": {
            "server_name": {
                "command": "string (required)",
                "args": "list of strings (optional)",
                "env": "dict of strings (optional)",
                "cwd": "string (optional)",
            }
        }
    },
}

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
        "tools": ["read_file", "glob", "grep"],
        "prompt_append": (
            "Focus on analysis, architecture, and step-by-step plans. Avoid file mutations "
            "unless explicitly requested."
        ),
        "allowed_subagent_modes": ["explore"],
    },
    "debugger": {
        "description": "Debugging mode focused on diagnosis and verification.",
        "tools": ["read_file", "glob", "grep", "shell"],
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
    "max_tokens": 4096,
    "temperature": 0.0,
    "max_context_tokens": 128_000,
    "approval_default_mode": "require_approval",
    "approval_rules": [
        {"tool_name": "read_file", "action": "allow"},
        {"tool_name": "glob", "action": "allow"},
        {"tool_name": "grep", "action": "allow"},
        {"tool_name": "write_file", "action": "require_approval"},
        {"tool_name": "edit_file", "action": "require_approval"},
        {"tool_name": "shell", "action": "require_approval"},
        {"tool_name": "agent", "action": "require_approval"},
        {"tool_source": "mcp", "mcp_server": "filesystem", "action": "warn"},
        {"tool_source": "mcp", "action": "require_approval"},
    ],
    "tool_output_max_chars": 12_000,
    "tool_output_max_lines": 120,
    "tool_output_store_full": True,
    "tool_output_store_dir": None,
    "session_auto_save": True,
    "session_dir": None,  # Will be computed at runtime
    "history_file": None,  # Will be computed at runtime
    "llm_debug_trace": False,
    "snip_keep_recent_tools": 5,
    "snip_threshold_chars": 1500,
    "snip_min_lines": 6,
    "summarize_keep_recent_turns": 5,
}
