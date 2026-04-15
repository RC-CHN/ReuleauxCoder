"""Compatibility layer - license notice and migration helpers."""

from reuleauxcoder.compat.config_migration import migrate_bash_to_shell, migrate_legacy_config

__all__ = ["migrate_legacy_config", "migrate_bash_to_shell"]
