from pathlib import Path

from reuleauxcoder.extensions.skills.discovery import discover_skills


def _write_skill(root: Path, folder_name: str, declared_name: str, description: str = "desc") -> None:
    skill_dir = root / folder_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {declared_name}\ndescription: {description}\n---\n\nBody for {declared_name}.\n",
        encoding="utf-8",
    )


def test_discover_skills_returns_empty_when_roots_missing(tmp_path: Path) -> None:
    skills, diagnostics, missing = discover_skills(workspace_dir=tmp_path, home_dir=tmp_path)
    assert skills == ()
    assert diagnostics == ()
    assert missing == ()


def test_discover_skills_discovers_project_and_user_skills(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    _write_skill(workspace_dir / ".rcoder" / "skills", "project-skill", "project-skill")
    _write_skill(home_dir / ".rcoder" / "skills", "user-skill", "user-skill")

    skills, diagnostics, missing = discover_skills(workspace_dir=workspace_dir, home_dir=home_dir)

    assert [skill.name for skill in skills] == ["project-skill", "user-skill"]
    assert diagnostics == ()
    assert missing == ()


def test_discover_skills_project_overrides_user_for_same_skill_name(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    _write_skill(home_dir / ".rcoder" / "skills", "same-skill", "same-skill", "user version")
    _write_skill(workspace_dir / ".rcoder" / "skills", "same-skill", "same-skill", "project version")

    skills, diagnostics, missing = discover_skills(workspace_dir=workspace_dir, home_dir=home_dir)

    assert len(skills) == 1
    assert skills[0].scope == "project"
    assert skills[0].description == "project version"
    assert any("overrides" in item.message for item in diagnostics)
    assert missing == ()


def test_discover_skills_marks_disabled_names(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    _write_skill(workspace_dir / ".rcoder" / "skills", "demo", "demo")

    skills, diagnostics, missing = discover_skills(
        workspace_dir=workspace_dir,
        home_dir=home_dir,
        disabled_names={"demo"},
    )

    assert len(skills) == 1
    assert skills[0].enabled is False
    assert diagnostics == ()
    assert missing == ()


def test_discover_skills_ignores_missing_skill_md_files(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    skill_dir = workspace_dir / ".rcoder" / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)

    skills, diagnostics, missing = discover_skills(workspace_dir=workspace_dir, home_dir=home_dir)

    assert skills == ()
    assert diagnostics == ()
    assert missing == ()
