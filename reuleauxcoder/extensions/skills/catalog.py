"""Stable catalog rendering for skills."""

from __future__ import annotations

import json
from collections.abc import Mapping
from xml.sax.saxutils import escape

from reuleauxcoder.extensions.skills.models import Skill

_INSTRUCTIONS = """# Skills
The following skills provide specialized instructions for specific tasks.
When a task matches a skill's description, use the read_file tool to load
the SKILL.md at the listed location before proceeding.
When a skill references relative paths, resolve them against the skill's
root directory and prefer absolute paths in tool calls.
"""


def build_skills_catalog(
    skills: tuple[Skill, ...], *, runtime: Mapping[str, str | None] | None = None
) -> str:
    """Build stable prompt text for active skills."""
    if not skills:
        return ""

    lines = [
        _INSTRUCTIONS.rstrip(),
        "",
        "<available_skills>",
    ]
    for skill in sorted(skills, key=lambda s: s.name):
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description)}</description>",
                f"    <location>{escape(skill.location)}</location>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    if runtime:
        lines.extend([
            "",
            "Core launch environment (JSON; paths belong to the core host):",
            "<skill_runtime>",
            escape(json.dumps(dict(runtime), ensure_ascii=False)),
            "</skill_runtime>",
        ])
    return "\n".join(lines)
