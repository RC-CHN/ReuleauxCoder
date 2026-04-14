from pathlib import Path

from reuleauxcoder.extensions.skills.parser import parse_skill_file


def test_parse_skill_file_valid_minimal_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: Demo description\n---\n\nUse this skill carefully.\n",
        encoding="utf-8",
    )

    skill, diagnostics = parse_skill_file(skill_file, scope="project")

    assert skill is not None
    assert skill.name == "demo-skill"
    assert skill.description == "Demo description"
    assert skill.body == "Use this skill carefully."
    assert skill.scope == "project"
    assert diagnostics == ()


def test_parse_skill_file_missing_frontmatter_returns_error(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("no frontmatter here", encoding="utf-8")

    skill, diagnostics = parse_skill_file(skill_file, scope="project")

    assert skill is None
    assert diagnostics
    assert diagnostics[0].level == "error"


def test_parse_skill_file_missing_required_name_returns_error(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\ndescription: Demo description\n---\n\nBody\n",
        encoding="utf-8",
    )

    skill, diagnostics = parse_skill_file(skill_file, scope="project")

    assert skill is None
    assert any("name" in item.message for item in diagnostics)


def test_parse_skill_file_name_directory_mismatch_emits_warning(tmp_path: Path) -> None:
    skill_dir = tmp_path / "folder-name"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: declared-name\ndescription: Demo description\n---\n\nBody\n",
        encoding="utf-8",
    )

    skill, diagnostics = parse_skill_file(skill_file, scope="user", enabled=False)

    assert skill is not None
    assert skill.enabled is False
    assert any(item.level == "warning" for item in diagnostics)


def test_parse_skill_file_invalid_yaml_returns_error(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: [unterminated\ndescription: Demo\n---\n\nBody\n",
        encoding="utf-8",
    )

    skill, diagnostics = parse_skill_file(skill_file, scope="project")

    assert skill is None
    assert diagnostics
    assert diagnostics[0].level == "error"
