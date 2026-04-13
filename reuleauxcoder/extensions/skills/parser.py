"""SKILL.md parser."""

from __future__ import annotations

from pathlib import Path

import yaml

from reuleauxcoder.extensions.skills.models import Skill, SkillDiagnostic


class SkillParseError(RuntimeError):
    """Raised when a skill file cannot be parsed."""


def parse_skill_file(skill_md_path: Path, *, scope: str, enabled: bool = True) -> tuple[Skill | None, tuple[SkillDiagnostic, ...]]:
    """Parse one SKILL.md file into a Skill model."""
    diagnostics: list[SkillDiagnostic] = []

    try:
        raw = skill_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.append(
            SkillDiagnostic(
                level="error",
                message=f"Failed to read SKILL.md: {exc}",
                path=str(skill_md_path),
            )
        )
        return None, tuple(diagnostics)

    if not raw.startswith("---"):
        diagnostics.append(
            SkillDiagnostic(
                level="error",
                message="SKILL.md is missing YAML frontmatter.",
                path=str(skill_md_path),
            )
        )
        return None, tuple(diagnostics)

    try:
        _, frontmatter, body = raw.split("---", 2)
    except ValueError:
        diagnostics.append(
            SkillDiagnostic(
                level="error",
                message="SKILL.md frontmatter is malformed.",
                path=str(skill_md_path),
            )
        )
        return None, tuple(diagnostics)

    try:
        data = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as exc:
        diagnostics.append(
            SkillDiagnostic(
                level="error",
                message=f"Invalid YAML frontmatter: {exc}",
                path=str(skill_md_path),
            )
        )
        return None, tuple(diagnostics)

    if not isinstance(data, dict):
        diagnostics.append(
            SkillDiagnostic(
                level="error",
                message="SKILL.md frontmatter must be a mapping.",
                path=str(skill_md_path),
            )
        )
        return None, tuple(diagnostics)

    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not name.strip():
        diagnostics.append(
            SkillDiagnostic(
                level="error",
                message="Skill missing required frontmatter field: name.",
                path=str(skill_md_path),
            )
        )
        return None, tuple(diagnostics)

    if not isinstance(description, str) or not description.strip():
        diagnostics.append(
            SkillDiagnostic(
                level="error",
                message="Skill missing required frontmatter field: description.",
                skill_name=name.strip(),
                path=str(skill_md_path),
            )
        )
        return None, tuple(diagnostics)

    name = name.strip()
    description = description.strip()
    skill_dir = skill_md_path.parent
    parent_name = skill_dir.name
    if parent_name != name:
        diagnostics.append(
            SkillDiagnostic(
                level="warning",
                message=f"Skill name '{name}' does not match parent directory '{parent_name}'.",
                skill_name=name,
                path=str(skill_md_path),
            )
        )

    skill = Skill(
        name=name,
        description=description,
        location=str(skill_md_path.resolve()),
        skill_dir=str(skill_dir.resolve()),
        body=body.strip(),
        scope=scope,
        enabled=enabled,
    )
    return skill, tuple(diagnostics)
