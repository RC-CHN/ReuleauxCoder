from pathlib import Path

import pytest
import yaml

from reuleauxcoder.app.rpc.codec import decode, encode
from reuleauxcoder.extensions.command.builtin.skills import command_panel_spec
from reuleauxcoder.extensions.skills.models import SkillDisplay
from reuleauxcoder.extensions.skills.parser import parse_skill_file
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.infrastructure.persistence.skills_config_store import SkillsConfigStore


def write_skill(root: Path, name: str = "example", **fields) -> Path:
    file = root / ".rcoder" / "skills" / name / "SKILL.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("---\n" + yaml.safe_dump({
        "name": name, "description": "Model-facing trigger", **fields,
    }, allow_unicode=True) + "---\nInstructions", encoding="utf-8")
    return file


@pytest.mark.parametrize("metadata", [None, {}, "third-party metadata", [], 17, {
    "rcoder.display-name": [], "rcoder.summary": False, "rcoder.icon": "https://example.test/icon.svg",
    "rcoder.category": {}, "rcoder.display-name.zh_CN": "Bad locale",
}])
def test_optional_metadata_never_prevents_loading_or_toggling(tmp_path, metadata):
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    write_skill(workspace, metadata=metadata, scope="builtin")
    service = SkillsService(workspace_dir=workspace, home_dir=home,
                            config_store=SkillsConfigStore(workspace / ".rcoder/config.yaml"))
    result = service.reload()
    skill = service.get("example")
    assert skill is not None and skill.scope == "project" and skill.enabled
    assert skill.display == SkillDisplay()
    assert all(issue.level == "warning" for issue in result.diagnostics)
    assert "Model-facing trigger" in result.catalog
    service.set_enabled("example", False)
    assert not service.get("example").enabled
    service.set_enabled("example", True)
    assert service.get("example").enabled


def test_partial_localization_unknown_symbols_and_unrelated_metadata(tmp_path):
    file = write_skill(tmp_path, metadata={
        "vendor": {"arbitrary": "preserved by the skill file"},
        "builtin": True, "rcoder.display-name.zh-CN": "示例技能",
        "rcoder.summary": "A short UI summary", "rcoder.icon": "future-icon",
        "rcoder.category": "future-category", "rcoder.summary.fr": "x" * 501,
    })
    skill, diagnostics = parse_skill_file(file, scope="user")
    assert skill.scope == "user"
    assert dict(skill.display.titles) == {"zh-cn": "示例技能"}
    assert dict(skill.display.summaries) == {"en": "A short UI summary"}
    assert skill.display.icon == "future-icon"
    assert len(diagnostics) == 1 and diagnostics[0].level == "warning"


def test_metadata_reload_override_and_rpc_projection_preserve_identity(tmp_path):
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    file = write_skill(workspace, metadata={"rcoder.display-name": "First title"})
    service = SkillsService(workspace_dir=workspace, home_dir=home)
    first = service.reload()
    file.write_text(file.read_text().replace("First title", "New title"))
    changed = service.reload()
    assert changed.changed and changed.updated == ("example",)
    assert changed.catalog == first.catalog  # Presentation does not enter the LLM catalog.
    model = service.build_view()
    panel = command_panel_spec().build(model, "Skills")
    restored = decode(encode(panel))
    row = next(row for row in restored.items if row.id == "example")
    assert row.label == "New title" and row.details.source == "project"
    assert row.action.command["skill_name"] == "example"
    assert row.details.description == "Model-facing trigger"
    assert decode(encode(model)) == model

    override = write_skill(workspace, "pptx")  # Standard skill without display metadata.
    service.reload()
    assert service.get("pptx").scope == "project"
    assert service.get("pptx").display == SkillDisplay()  # Do not inherit builtin branding.
    override.unlink()
    service.reload()
    assert service.get("pptx").scope == "builtin"
    assert dict(service.get("pptx").display.titles)["zh-cn"] == "演示文稿"


def test_invalid_yaml_and_required_fields_remain_errors(tmp_path):
    file = write_skill(tmp_path)
    file.write_text("---\nname: example\ndescription: ok\nmetadata: [\n---\nBody")
    skill, diagnostics = parse_skill_file(file, scope="project")
    assert skill is None and diagnostics[0].level == "error"


def test_failed_toggle_save_can_be_retried_without_runtime_drift(tmp_path, monkeypatch):
    write_skill(tmp_path)
    store = SkillsConfigStore(tmp_path / ".rcoder/config.yaml")
    service = SkillsService(workspace_dir=tmp_path, home_dir=tmp_path / "home", config_store=store)
    service.reload()
    with monkeypatch.context() as patch:
        def fail(_names):
            raise OSError("Read-only configuration")
        patch.setattr(store, "save_disabled_skills", fail)
        with pytest.raises(OSError):
            service.set_enabled("example", False)
        assert service.get("example").enabled
        assert "example" not in service.disabled_names
    assert service.set_enabled("example", False).changed
    assert not service.get("example").enabled
    assert yaml.safe_load(store.path.read_text())["skills"]["disabled"] == ["example"]
