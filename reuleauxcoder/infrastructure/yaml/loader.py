"""YAML loader - loads YAML configuration files."""

from pathlib import Path
import yaml

from reuleauxcoder.infrastructure.fs.atomic import atomic_write


def load_yaml_config(path: Path) -> dict:
    """Load a YAML configuration file."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    return data or {}


def save_yaml_config(path: Path, data: dict) -> None:
    """Save data to a YAML configuration file."""
    # Preserve symlink semantics for existing command stores; management APIs
    # explicitly reject symlink replacement and require an explicit target.
    target = path.resolve() if path.is_symlink() else path
    atomic_write(target, yaml.safe_dump(data, default_flow_style=False, allow_unicode=True).encode("utf-8"))


def merge_yaml_configs(base: dict, override: dict) -> dict:
    """Merge two YAML configurations, with override taking precedence."""
    result = base.copy()

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_yaml_configs(result[key], value)
        else:
            result[key] = value

    return result
