"""Skills extension support."""

from reuleauxcoder.extensions.skills.models import (
    Skill,
    SkillReloadResult,
    SkillToggleResult,
)

__all__ = ["Skill", "SkillReloadResult", "SkillToggleResult", "SkillsService"]


def __getattr__(name):
    if name == "SkillsService":
        from reuleauxcoder.extensions.skills.service import SkillsService

        return SkillsService
    raise AttributeError(name)
