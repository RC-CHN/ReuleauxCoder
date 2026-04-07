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
    "session_auto_save": True,
    "session_dir": None,  # Will be computed at runtime
    "history_file": None,  # Will be computed at runtime
}
