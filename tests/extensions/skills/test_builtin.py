import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
import yaml

from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.skills.discovery import BUILTIN_SKILLS_DIR, discover_skills
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.infrastructure.persistence.skills_config_store import SkillsConfigStore
from reuleauxcoder.interfaces.entrypoint.dependencies import AppOptions
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner
from reuleauxcoder.services.config.definition import config_schema
from reuleauxcoder.services.config.validation import resolve_layers
from reuleauxcoder.services.llm.providers import _responses_request
from reuleauxcoder.services.llm.request_parameters import model_request_parameters
from reuleauxcoder.services.llm.sanitizer import sanitize_messages_for_llm


SKILL_DIR = BUILTIN_SKILLS_DIR / "rcoder-config"


def test_fresh_install_discovers_builtin_without_creating_user_files(tmp_path):
    service = SkillsService(workspace_dir=tmp_path / "project", home_dir=tmp_path / "home")
    loaded = service.reload()
    assert [(skill.name, skill.scope) for skill in loaded.active_skills] == [("rcoder-config", "builtin")]
    assert not loaded.diagnostics
    assert not loaded.missing
    assert "rcoder-config" in loaded.catalog
    assert "先定位，再修改" not in loaded.catalog  # Body is loaded on demand.
    assert not list(tmp_path.iterdir())
    assert not service.reload().changed


@pytest.mark.parametrize("scope", ["user", "project"])
def test_custom_skill_overrides_builtin_and_removal_restores_it(tmp_path, scope):
    workspace, home = tmp_path / "project", tmp_path / "home"
    root = workspace if scope == "project" else home
    file = root / ".rcoder/skills/rcoder-config/SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text("---\nname: rcoder-config\ndescription: Custom config\n---\nCustom body", encoding="utf-8")
    service = SkillsService(workspace_dir=workspace, home_dir=home)
    assert service.reload().active_skills[0].scope == scope
    file.unlink()
    loaded = service.reload()
    assert loaded.updated == ("rcoder-config",)
    assert loaded.active_skills[0].scope == "builtin"


def test_builtin_respects_disable_and_existing_toggle_persistence(tmp_path):
    workspace, home = tmp_path / "project", tmp_path / "home"
    path = workspace / ".rcoder/config.yaml"
    service = SkillsService(
        workspace_dir=workspace, home_dir=home, scan_user=False, scan_project=False,
        config_store=SkillsConfigStore(path),
    )
    assert service.reload().active_skills  # Scan flags do not disable packaged skills.
    assert service.set_enabled("rcoder-config", False).changed
    assert not service.active()
    assert not service.build_catalog()
    assert yaml.safe_load(path.read_text())["skills"]["disabled"] == ["rcoder-config"]
    assert service.build_view().skills[0].scope == "builtin"
    assert not service.build_view().skills[0].enabled
    assert service.set_enabled("rcoder-config", True).changed
    assert service.active()
    assert not (workspace / ".rcoder/skills").exists()
    assert not home.exists()
    assert not SkillsService(workspace_dir=workspace, home_dir=home, enabled=False).reload().catalog


def test_disabled_name_applies_after_user_override(tmp_path):
    file = tmp_path / ".rcoder/skills/config-alias/SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text("---\nname: rcoder-config\ndescription: Custom\n---\nBody", encoding="utf-8")
    skills, _, _ = discover_skills(workspace_dir=tmp_path, home_dir=tmp_path, disabled_names={"rcoder-config"})
    assert len(skills) == 1
    assert not skills[0].enabled
    assert skills[0].scope == "user"


def test_runner_catalog_records_actual_core_and_explicit_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Avoid any real user's skills. Include characters that require XML escaping.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    config_path = Path("配置 & special.yaml")
    runner = AppRunner(options=AppOptions(config_path=config_path))
    agent = SimpleNamespace()
    bus = SimpleNamespace(info=lambda *args, **kwargs: None)
    service = runner._init_skills(Config(), agent, bus)
    xml = agent.skills_catalog.split("<skill_runtime>", 1)[1].split("</skill_runtime>", 1)[0]
    value = json.loads(ElementTree.fromstring(f"<runtime>{xml}</runtime>").text)
    assert value == {"python": sys.executable, "workspace": str(tmp_path), "config": str(tmp_path / config_path)}
    assert service.reload().catalog == agent.skills_catalog


def _schema_fields(schema, prefix=""):
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            if variant.get("type") != "null":
                yield from _schema_fields(variant, prefix)
    elif "properties" in schema:
        for name, child in schema["properties"].items():
            yield from _schema_fields(child, prefix + "/" + name)
    elif schema.get("additionalProperties", {}).get("properties"):
        yield from _schema_fields(schema["additionalProperties"], prefix + "/{name}")
    elif schema.get("items", {}).get("properties"):
        yield from _schema_fields(schema["items"], prefix + "/[]")
    else:
        yield prefix


def test_reference_documents_every_public_configuration_field():
    reference = (SKILL_DIR / "references/configuration.md").read_text(encoding="utf-8")
    documented = re.findall(r"^\| `(/[^`]+)` \|", reference, re.MULTILINE)
    expected = set(_schema_fields(config_schema()))
    assert set(documented) == expected
    assert len(documented) == len(expected)  # Each canonical field is documented once.


def test_all_skill_reference_links_resolve_within_package():
    visited = set()
    pending = [SKILL_DIR / "SKILL.md"]
    while pending:
        file = pending.pop().resolve()
        if file in visited:
            continue
        visited.add(file)
        file.relative_to(SKILL_DIR.resolve())
        content = file.read_text(encoding="utf-8")
        for relative in re.findall(r"\]\(([^)]+\.md)\)", content):
            pending.append(file.parent / relative)
    assert len(visited) == 4


def _examples():
    text = (SKILL_DIR / "references/workflows.md").read_text(encoding="utf-8")
    return {name: yaml.safe_load(source) for name, source in re.findall(
        r"<!-- example: ([\w-]+) -->\s*```yaml\n(.*?)```", text, re.DOTALL
    )}


@pytest.mark.parametrize("name", ["base", "hidden-ui", "effort", "thinking-replay", "responses", "context", "disabled-skill"])
def test_documented_examples_resolve_and_produce_intended_behavior(name):
    examples = _examples()
    config, issues = resolve_layers([("user", examples["base"]), ("workspace", examples[name])])
    assert not issues
    assert config is not None
    profile = config.model_profiles["main"]
    params = model_request_parameters(profile, [])
    assert profile.api_key == "example-key-not-a-real-credential"
    if name == "hidden-ui":
        assert config.ui.reasoning_display == "hidden"
        assert profile.preserve_reasoning_content
        assert "reasoning_effort" not in params
    elif name == "effort":
        assert params["reasoning_effort"] == "high"
        assert "extra_body" not in params
    elif name == "responses":
        wire = _responses_request(params, cache_mode=profile.responses.cache.mode)
        assert wire["reasoning"] == {"effort": "high"}
        assert wire["include"] == ["reasoning.encrypted_content"]
        assert wire["store"] is False
    elif name == "thinking-replay":
        assert params["extra_body"] == {"thinking": {"type": "enabled"}}
        history = sanitize_messages_for_llm([
            {"role": "user", "content": "Read"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "read", "content": "Result"},
        ], preserve_reasoning_content=profile.preserve_reasoning_content,
            reasoning_replay_mode=profile.reasoning_replay_mode,
            reasoning_replay_placeholder=profile.reasoning_replay_placeholder,
            thinking_enabled=profile.thinking_enabled)
        assert history[1]["reasoning_content"] == "[PLACE_HOLDER]"
    elif name == "context":
        assert profile.context.auto_snip is None
        assert profile.context.auto_summarize is False
        assert config.context.auto_snip
    elif name == "disabled-skill":
        assert config.skills.disabled == ["rcoder-config"]


def test_thinking_recipe_clears_shared_effort_instead_of_being_shadowed():
    examples = _examples()
    config, issues = resolve_layers([
        ("user", {**examples["base"], "app": {"reasoning_effort": "high"}}),
        ("workspace", examples["thinking-replay"]),
    ])
    assert not issues
    params = model_request_parameters(config.model_profiles["main"], [])
    assert "reasoning_effort" not in params
    assert params["extra_body"]["thinking"]["type"] == "enabled"
