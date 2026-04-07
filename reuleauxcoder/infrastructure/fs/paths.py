"""Filesystem paths - standard paths for ReuleauxCoder."""

from pathlib import Path


def get_sessions_dir() -> Path:
    """Get the default sessions directory.
    
    Default: current working directory / .reuleauxcoder / sessions
    Falls back to user home directory if cwd is not writable.
    """
    cwd_sessions = Path.cwd() / ".reuleauxcoder" / "sessions"
    # Check if we can write to cwd
    cwd_reuleaux = Path.cwd() / ".reuleauxcoder"
    try:
        cwd_reuleaux.mkdir(parents=True, exist_ok=True)
        return cwd_sessions
    except (PermissionError, OSError):
        # Fall back to home directory
        return Path.home() / ".reuleauxcoder" / "sessions"


def get_history_file() -> Path:
    """Get the default history file path."""
    return Path.home() / ".reuleauxcoder" / "history"


def get_user_config_dir() -> Path:
    """Get the user config directory."""
    return Path.home() / ".reuleauxcoder"


def ensure_user_dirs() -> None:
    """Ensure all user directories exist."""
    get_user_config_dir().mkdir(parents=True, exist_ok=True)
    get_sessions_dir().mkdir(parents=True, exist_ok=True)
