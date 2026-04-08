"""Configuration schema - YAML structure definition."""

# Expected YAML structure for config.yaml
CONFIG_SCHEMA = {
    "app": {
        "model": "string (default: gpt-4o)",
        "api_key": "string (required)",
        "base_url": "string (optional)",
        "max_tokens": "int (default: 4096)",
        "temperature": "float (default: 0.0)",
        "max_context_tokens": "int (default: 128000)",
    },
    "approval": {
        "default_mode": "string (default: require_approval, one of allow/warn/require_approval/deny)",
        "rules": [
            {
                "tool_name": "string (optional)",
                "tool_source": "string (optional)",
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
        {"tool_name": "bash", "action": "require_approval"},
        {"tool_name": "agent", "action": "require_approval"},
        {"tool_source": "mcp", "action": "require_approval"},
    ],
    "tool_output_max_chars": 12_000,
    "tool_output_max_lines": 120,
    "tool_output_store_full": True,
    "tool_output_store_dir": None,
    "session_auto_save": True,
    "session_dir": None,  # Will be computed at runtime
    "history_file": None,  # Will be computed at runtime
}
