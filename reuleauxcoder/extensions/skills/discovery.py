"""Skill discovery helpers."""

from __future__ import annotations

from pathlib import Path

from reuleauxcoder.extensions.skills.models import Skill, SkillDiagnostic
from reuleauxcoder.extensions.skills.parser import parse_skill_file


def discover_skills(
    *,
    workspace_dir: Path,
    home_dir: Path,
    scan_project: bool = True,
    scan_user: bool = True,
    disabled_names: set[str] | None = None,
) -> tuple[tuple[Skill, ...], tuple[SkillDiagnostic, ...], tuple[str, ...]]:
    """Discover skills from configured roots."""
    disabled_names = set(disabled_names or set())
    diagnostics: list[SkillDiagnostic] = []
    missing: list[str] = []

    discovered: dict[str, Skill] = {}
    roots: list[tuple[str, Path]] = []
    if scan_user:
        roots.append(("user", home_dir / ".rcoder" / "skills"))
    if scan_project:
        roots.append(("project", workspace_dir / ".rcoder" / "skills"))

    # Avoid scanning the same physical path twice (e.g. workspace == home).
    unique_roots: list[tuple[str, Path]] = []
    seen_roots: set[str] = set()
    for scope, root in roots:
        try:
            root_key = str(root.resolve(strict=False))
        except OSError:
            root_key = str(root)
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        unique_roots.append((scope, root))

    for scope, root in unique_roots:
        if not root.exists() or not root.is_dir():
            continue

        try:
            entries = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
        except OSError as exc:
            diagnostics.append(
                SkillDiagnostic(level="warning", message=f"Failed to scan {root}: {exc}", path=str(root))
            )
            continue

        for skill_dir in entries:
            skill_md_path = skill_dir / "SKILL.md"
            if not skill_md_path.exists():
                continue
            if not skill_md_path.is_file():
                missing.append(skill_dir.name)
                diagnostics.append(
                    SkillDiagnostic(
                        level="warning",
                        message="Skill SKILL.md not found or is not a file.",
                        skill_name=skill_dir.name,
                        path=str(skill_md_path),
                    )
                )
                continue

            skill, skill_diagnostics = parse_skill_file(
                skill_md_path,
                scope=scope,
                enabled=skill_dir.name not in disabled_names,
            )
            diagnostics.extend(skill_diagnostics)
            if skill is None:
                continue

            enabled = skill.name not in disabled_names
            if skill.name in discovered:
                previous = discovered[skill.name]
                diagnostics.append(
                    SkillDiagnostic(
                        level="warning",
                        message=(
                            f"Skill '{skill.name}' from {skill.location} overrides {previous.location}."
                        ),
                        skill_name=skill.name,
                        path=skill.location,
                    )
                )
            discovered[skill.name] = Skill(
                name=skill.name,
                description=skill.description,
                location=skill.location,
                skill_dir=skill.skill_dir,
                body=skill.body,
                scope=scope,
                enabled=enabled,
            )

    skills = tuple(sorted(discovered.values(), key=lambda s: s.name))
    return skills, tuple(diagnostics), tuple(sorted(set(missing)))
