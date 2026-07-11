"""Versioned extension metadata with explicit ordering and scope policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


EXTENSION_API_VERSION = 1


class ExtensionScope(str, Enum):
    RUNNER = "runner"
    SESSION = "session"
    AGENT = "agent"
    SUBAGENT = "subagent"


class ExtensionPhase(IntEnum):
    AUTHORIZATION = 10
    CONTEXT = 20
    OUTCOME = 30
    OBSERVATION = 40
    LIFECYCLE = 50


class SubagentPolicy(str, Enum):
    OMIT = "omit"
    REBUILD = "rebuild"


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
    phase: ExtensionPhase = ExtensionPhase.LIFECYCLE
    subagent_policy: SubagentPolicy = SubagentPolicy.OMIT
    remote_compatible: bool = False
    thread_safe: bool = False

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
        if (
            ExtensionScope.SUBAGENT in self.scopes
            and self.subagent_policy is SubagentPolicy.OMIT
        ):
            raise ValueError(
                "a subagent-scoped extension must use subagent_policy='rebuild'"
            )
