"""Skills data models."""

from __future__ import annotations

from dataclasses import dataclass


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
class SkillViewItem:
    name: str
    description: str
    scope: str
    enabled: bool
    location: str


@dataclass(frozen=True, slots=True)
class SkillsSummary:
    discovered: int
    active: int
    disabled: int
    config_enabled: bool
    scan_project: bool
    scan_user: bool
    catalog_loaded: bool


@dataclass(frozen=True, slots=True)
class SkillsViewModel:
    """Structured payload for `/skills` views."""

    skills: tuple[SkillViewItem, ...]
    summary: SkillsSummary
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    view_type: str = "skills"

    def to_payload(self) -> dict[str, object]:
        return {
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.scope,
                    "enabled": skill.enabled,
                    "status": "enabled" if skill.enabled else "disabled",
                    "location": skill.location,
                }
                for skill in self.skills
            ],
            "summary": {
                "discovered": self.summary.discovered,
                "active": self.summary.active,
                "disabled": self.summary.disabled,
                "config_enabled": self.summary.config_enabled,
                "scan_project": self.summary.scan_project,
                "scan_user": self.summary.scan_user,
                "catalog_loaded": self.summary.catalog_loaded,
            },
            "diagnostics": [
                {
                    "level": item.level,
                    "message": item.message,
                    "skill_name": item.skill_name,
                    "path": item.path,
                }
                for item in self.diagnostics
            ],
            "added": list(self.added),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "missing": list(self.missing),
        }
