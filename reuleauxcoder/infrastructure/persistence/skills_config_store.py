"""Workspace persistence for skills configuration."""

from __future__ import annotations

from pathlib import Path

from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


class SkillsConfigStore:
    """Persist skills runtime config into workspace config.yaml."""

    def __init__(self, path: Path | None = None):
        self._path = path or ConfigLoader.WORKSPACE_CONFIG_PATH

    @property
    def path(self) -> Path:
        return self._path

    def save_disabled_skills(self, names: list[str]) -> Path:
        try:
            data = load_yaml_config(self._path)
        except FileNotFoundError:
            data = {}

        skills = data.setdefault("skills", {})
        skills["disabled"] = sorted(set(names))
        save_yaml_config(self._path, data)
        return self._path
