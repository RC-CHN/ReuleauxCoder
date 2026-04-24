"""Skills service."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from reuleauxcoder.extensions.skills.catalog import build_skills_catalog
from reuleauxcoder.extensions.skills.discovery import discover_skills
from reuleauxcoder.extensions.skills.models import (
    Skill,
    SkillReloadResult,
    SkillToggleResult,
    SkillsViewModel,
)
from reuleauxcoder.infrastructure.persistence.skills_config_store import (
    SkillsConfigStore,
)


class SkillsService:
    """Manage discovered skills, prompt catalog, and enable/disable state."""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        home_dir: Path,
        enabled: bool = True,
        scan_project: bool = True,
        scan_user: bool = True,
        disabled_names: list[str] | None = None,
        config_store: SkillsConfigStore | None = None,
    ):
        self.workspace_dir = workspace_dir
        self.home_dir = home_dir
        self.enabled = enabled
        self.scan_project = scan_project
        self.scan_user = scan_user
        self._disabled_names: set[str] = set(disabled_names or [])
        self._config_store = config_store or SkillsConfigStore()

        self._skills: dict[str, Skill] = {}
        self._active_skills: tuple[Skill, ...] = ()
        self._catalog = ""
        self._catalog_signature = ""
        self._last_reload = SkillReloadResult()

    def discover(self) -> list[Skill]:
        return list(self.reload().all_skills)

    def reload(self) -> SkillReloadResult:
        previous = self._skills
        previous_names = set(previous)

        if not self.enabled:
            self._skills = {}
            self._active_skills = ()
            changed = bool(previous_names) or bool(self._catalog)
            self._catalog = ""
            self._catalog_signature = self._signature(self._catalog)
            self._last_reload = SkillReloadResult(changed=changed)
            return self._last_reload

        discovered, diagnostics, missing = discover_skills(
            workspace_dir=self.workspace_dir,
            home_dir=self.home_dir,
            scan_project=self.scan_project,
            scan_user=self.scan_user,
            disabled_names=self._disabled_names,
        )
        current = {skill.name: skill for skill in discovered}
        current_names = set(current)

        added = sorted(current_names - previous_names)
        removed = sorted(previous_names - current_names)
        updated = sorted(
            name
            for name in current_names & previous_names
            if self._skill_fingerprint(current[name])
            != self._skill_fingerprint(previous[name])
        )

        active = tuple(skill for skill in discovered if skill.enabled)
        catalog = build_skills_catalog(active)
        signature = self._signature(catalog)
        changed = bool(
            added or removed or updated or signature != self._catalog_signature
        )

        self._skills = current
        self._active_skills = active
        self._catalog = catalog
        self._catalog_signature = signature
        self._last_reload = SkillReloadResult(
            all_skills=tuple(sorted(current.values(), key=lambda s: s.name)),
            active_skills=active,
            added=tuple(added),
            updated=tuple(updated),
            removed=tuple(removed),
            missing=missing,
            diagnostics=diagnostics,
            catalog=catalog,
            changed=changed,
        )
        return self._last_reload

    def set_enabled(self, name: str, enabled: bool) -> SkillToggleResult:
        skill = self._skills.get(name)
        if skill is None:
            return SkillToggleResult(
                name=name,
                enabled=enabled,
                found=False,
                changed=False,
                message=f"Skill '{name}' not found.",
            )

        changed = False
        if enabled:
            if name in self._disabled_names:
                self._disabled_names.remove(name)
                changed = True
        else:
            if name not in self._disabled_names:
                self._disabled_names.add(name)
                changed = True

        saved_path = None
        if changed:
            saved_path = str(
                self._config_store.save_disabled_skills(sorted(self._disabled_names))
            )
            self.reload()

        return SkillToggleResult(
            name=name,
            enabled=enabled,
            found=True,
            changed=changed,
            saved_path=saved_path,
            message=(
                f"Skill '{name}' {'enabled' if enabled else 'disabled'}."
                if changed
                else f"Skill '{name}' already {'enabled' if enabled else 'disabled'}."
            ),
        )

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return [self._skills[name] for name in sorted(self._skills)]

    def active(self) -> list[Skill]:
        return list(self._active_skills)

    def build_catalog(self) -> str:
        return self._catalog

    def build_view(self) -> SkillsViewModel:
        skills = self.all()
        lines: list[str] = ["# Skills"]
        items: list[dict] = []
        summary = {
            "discovered": len(skills),
            "active": len(self._active_skills),
            "disabled": len([s for s in skills if not s.enabled]),
            "config_enabled": self.enabled,
            "scan_project": self.scan_project,
            "scan_user": self.scan_user,
            "catalog_loaded": bool(self._catalog),
        }

        lines.append(
            f"Discovered: **{summary['discovered']}** · Active: **{summary['active']}** · Disabled: **{summary['disabled']}**"
        )
        lines.append(
            f"Config: enabled=`{self.enabled}` project_scan=`{self.scan_project}` user_scan=`{self.scan_user}`"
        )
        lines.append("")

        last = self._last_reload
        if any((last.added, last.updated, last.removed, last.missing)):
            lines.append("## Last reload")
            if last.added:
                lines.append(f"- Added: {', '.join(last.added)}")
            if last.updated:
                lines.append(f"- Updated: {', '.join(last.updated)}")
            if last.removed:
                lines.append(f"- Removed: {', '.join(last.removed)}")
            if last.missing:
                lines.append(f"- Missing: {', '.join(last.missing)}")
            lines.append("")

        if not skills:
            lines.append("No skills discovered.")
        else:
            lines.append("## Skill list")
            for skill in skills:
                status = "enabled" if skill.enabled else "disabled"
                scope = skill.scope
                lines.append(f"- **{skill.name}** · `{status}` · `{scope}`")
                lines.append(f"  - {skill.description}")
                lines.append(f"  - location: `{skill.location}`")
                items.append(
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "scope": scope,
                        "enabled": skill.enabled,
                        "status": status,
                        "location": skill.location,
                    }
                )

        if last.diagnostics:
            lines.append("")
            lines.append("## Diagnostics")
            for diagnostic in last.diagnostics:
                prefix = "warning" if diagnostic.level == "warning" else "error"
                lines.append(f"- `{prefix}` {diagnostic.message}")

        return SkillsViewModel(
            markdown="\n".join(lines),
            skills=tuple(items),
            summary=summary,
        )

    @property
    def disabled_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._disabled_names))

    @property
    def last_reload(self) -> SkillReloadResult:
        return self._last_reload

    @staticmethod
    def _signature(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _skill_fingerprint(skill: Skill) -> tuple[str, str, str, str, bool]:
        return (
            skill.description,
            skill.location,
            skill.body,
            skill.scope,
            skill.enabled,
        )
