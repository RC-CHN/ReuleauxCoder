"""Skills data models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Skill:
    """Discovered skill metadata and instruction body."""

    name: str
    description: str
    location: str
    skill_dir: str
    body: str
    scope: str = "project"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    """Non-fatal issue encountered during scan/parse."""

    level: str
    message: str
    skill_name: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class SkillReloadResult:
    """Result of reloading skills from disk."""

    all_skills: tuple[Skill, ...] = ()
    active_skills: tuple[Skill, ...] = ()
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    catalog: str = ""
    changed: bool = False


@dataclass(frozen=True, slots=True)
class SkillToggleResult:
    """Result of enabling/disabling one skill."""

    name: str
    enabled: bool
    found: bool
    changed: bool
    saved_path: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class SkillsViewModel:
    """Structured payload for `/skills` views."""

    markdown: str
    skills: tuple[dict, ...] = field(default_factory=tuple)
    summary: dict = field(default_factory=dict)
