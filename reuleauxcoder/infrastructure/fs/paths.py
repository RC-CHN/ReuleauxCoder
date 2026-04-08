"""Filesystem paths - standard paths for ReuleauxCoder."""

from pathlib import Path


def get_sessions_dir() -> Path:
    """Get the default sessions directory.
    
    Default: current working directory / .rcoder / sessions
    Falls back to user home directory if cwd is not writable.
    """
    cwd_sessions = Path.cwd() / ".rcoder" / "sessions"
    # Check if we can write to cwd
    cwd_rcoder = Path.cwd() / ".rcoder"
    try:
        cwd_rcoder.mkdir(parents=True, exist_ok=True)
        return cwd_sessions
    except (PermissionError, OSError):
        # Fall back to home directory
        return Path.home() / ".rcoder" / "sessions"


def get_history_file() -> Path:
    """Get the default history file path.
    
    Default: current working directory / .rcoder / history
    """
    return Path.cwd() / ".rcoder" / "history"


def get_user_config_dir() -> Path:
    """Get the user config directory."""
    return Path.home() / ".rcoder"


def get_tool_outputs_dir(configured_dir: str | None = None) -> Path:
    """Get the tool output archive directory.

    Default: current working directory / .rcoder / tool-outputs
    Falls back to user home directory if cwd is not writable.
    """
    if configured_dir:
        path = Path(configured_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    cwd_outputs = Path.cwd() / ".rcoder" / "tool-outputs"
    cwd_rcoder = Path.cwd() / ".rcoder"
    try:
        cwd_rcoder.mkdir(parents=True, exist_ok=True)
        cwd_outputs.mkdir(parents=True, exist_ok=True)
        return cwd_outputs
    except (PermissionError, OSError):
        home_outputs = Path.home() / ".rcoder" / "tool-outputs"
        home_outputs.mkdir(parents=True, exist_ok=True)
        return home_outputs


def ensure_user_dirs() -> None:
    """Ensure all user directories exist."""
    get_user_config_dir().mkdir(parents=True, exist_ok=True)
    get_sessions_dir().mkdir(parents=True, exist_ok=True)
    get_tool_outputs_dir().mkdir(parents=True, exist_ok=True)
