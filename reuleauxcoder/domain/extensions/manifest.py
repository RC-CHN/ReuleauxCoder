"""Versioned extension metadata with explicit ordering and scope policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


EXTENSION_API_VERSION = 1


class ExtensionScope(str, Enum):
    RUNNER = "runner"
    SESSION = "session"
    AGENT = "agent"
    SUBAGENT = "subagent"


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    extension_id: str
    version: str
    api_version: int = EXTENSION_API_VERSION
    requires: frozenset[str] = field(default_factory=frozenset)
    before: frozenset[str] = field(default_factory=frozenset)
    after: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[ExtensionScope] = field(
        default_factory=lambda: frozenset({ExtensionScope.SESSION})
    )
    config_namespace: str | None = None

    def __post_init__(self) -> None:
        if not self.extension_id or self.extension_id.strip() != self.extension_id:
            raise ValueError("extension_id must be a non-empty normalized string")
        if not self.version:
            raise ValueError("extension version is required")
        if self.api_version < 1:
            raise ValueError("api_version must be positive")
        if self.extension_id in self.requires:
            raise ValueError("an extension cannot require itself")
        if self.extension_id in self.before or self.extension_id in self.after:
            raise ValueError("an extension cannot order itself")
        if self.before & self.after:
            raise ValueError("the same extension cannot appear in before and after")
        if not self.scopes:
            raise ValueError("at least one extension scope is required")
