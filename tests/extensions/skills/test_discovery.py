from pathlib import Path

from reuleauxcoder.extensions.skills.discovery import discover_skills


def _write_skill(
    root: Path, folder_name: str, declared_name: str, description: str = "desc"
) -> None:
    skill_dir = root / folder_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {declared_name}\ndescription: {description}\n---\n\nBody for {declared_name}.\n",
        encoding="utf-8",
    )


def test_discover_skills_returns_empty_when_roots_missing(tmp_path: Path) -> None:
    skills, diagnostics, missing = discover_skills(
        workspace_dir=tmp_path, home_dir=tmp_path
    )
    assert skills == ()
    assert diagnostics == ()
    assert missing == ()


def test_installed_skill_can_be_discovered_read_and_check_configuration_capabilities(tmp_path):
    import json

    from reuleauxcoder.app.configuration import ConfigurationService
    from reuleauxcoder.extensions.skills.service import SkillsService
    from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
    from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
    from reuleauxcoder.extensions.tools.builtin.configuration import ConfigReadTool

    project = tmp_path / "project"
    home = tmp_path / "home"
    _write_skill(project / ".rcoder/skills", "config-fixture", "config-fixture")
    service = SkillsService(workspace_dir=project, home_dir=home)
    loaded = service.reload()
    assert len(loaded.active_skills) == 1
    skill = loaded.active_skills[0]
    backend = LocalToolBackend(ExecutionContext(cwd=str(project), workspace_root=str(project)))
    content = ReadFileTool(backend).execute(skill.location).model_text
    assert "Body for config-fixture" in content
    config = ConfigReadTool()
    config.bind_configuration(ConfigurationService.for_workspace(project, home=home))
    capabilities = json.loads(config.execute("describe").content)["capabilities"]
    assert "profile_probes" in capabilities and "reviewer_protection" in capabilities


def test_discover_skills_discovers_project_and_user_skills(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    _write_skill(workspace_dir / ".rcoder" / "skills", "project-skill", "project-skill")
    _write_skill(home_dir / ".rcoder" / "skills", "user-skill", "user-skill")

    skills, diagnostics, missing = discover_skills(
        workspace_dir=workspace_dir, home_dir=home_dir
    )

    assert [skill.name for skill in skills] == ["project-skill", "user-skill"]
    assert diagnostics == ()
    assert missing == ()


def test_discover_skills_project_overrides_user_for_same_skill_name(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    _write_skill(
        home_dir / ".rcoder" / "skills", "same-skill", "same-skill", "user version"
    )
    _write_skill(
        workspace_dir / ".rcoder" / "skills",
        "same-skill",
        "same-skill",
        "project version",
    )

    skills, diagnostics, missing = discover_skills(
        workspace_dir=workspace_dir, home_dir=home_dir
    )

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


def test_discover_skills_deduplicates_identical_user_and_project_roots(
    tmp_path: Path,
) -> None:
    # Simulate environments where workspace and home resolve to the same location.
    shared_root = tmp_path
    _write_skill(
        shared_root / ".rcoder" / "skills", "same-root-skill", "same-root-skill"
    )

    skills, diagnostics, missing = discover_skills(
        workspace_dir=shared_root,
        home_dir=shared_root,
        scan_user=True,
        scan_project=True,
    )

    assert [skill.name for skill in skills] == ["same-root-skill"]
    assert diagnostics == ()
    assert missing == ()


def test_discover_skills_ignores_missing_skill_md_files(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"
    skill_dir = workspace_dir / ".rcoder" / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)

    skills, diagnostics, missing = discover_skills(
        workspace_dir=workspace_dir, home_dir=home_dir
    )

    assert skills == ()
    assert diagnostics == ()
    assert missing == ()
