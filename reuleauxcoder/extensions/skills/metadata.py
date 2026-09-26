"""Parse optional, namespaced display hints without rejecting usable skills."""

import re

from reuleauxcoder.extensions.skills.models import SkillDiagnostic, SkillDisplay


def parse_display(data: object, *, name: str, path: str) -> tuple[SkillDisplay, tuple[SkillDiagnostic, ...]]:
    warnings: list[SkillDiagnostic] = []

    def warn(key: str) -> None:
        warnings.append(SkillDiagnostic(
            "warning", f"Invalid optional skill metadata '{key}'; using the default.", name, path,
        ))

    if data is None:
        return SkillDisplay(), ()
    if not isinstance(data, dict):
        warn("metadata")
        return SkillDisplay(), tuple(warnings)

    def localized(key: str, limit: int) -> tuple[tuple[str, str], ...]:
        values: dict[str, str] = {}
        for field, value in data.items():
            if not isinstance(field, str) or not (field == key or field.startswith(key + ".")):
                continue
            locale = "en" if field == key else field[len(key) + 1:].lower()
            if (not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", locale)
                    or not isinstance(value, str) or not value.strip() or len(value) > limit):
                warn(field)
                continue
            values[locale] = value.strip()
        return tuple(sorted(values.items()))

    def symbol(key: str) -> str:
        value = data.get(key, "")
        if value == "":
            return ""
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value):
            warn(key)
            return ""
        return value

    result = SkillDisplay(
        titles=localized("rcoder.display-name", 120),
        summaries=localized("rcoder.summary", 500),
        icon=symbol("rcoder.icon"), category=symbol("rcoder.category"),
    )
    return result, tuple(warnings)
