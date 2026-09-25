"""The file, CLI and editor paths share read-only configuration checks."""

import json

import pytest
import yaml

from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.services.config.loader import ConfigLoader

SECRET = "synthetic-private-key"


@pytest.fixture
def service(tmp_path):
    path = tmp_path / ".rcoder/config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump({"app": {"api_key": SECRET, "model": "test"}}), encoding="utf-8")
    service = ConfigurationService.for_workspace(tmp_path, home=tmp_path / "home")
    service.probe = lambda layers, check, **kwargs: {"check": check, "status": "passed", "code": "ok"}
    return service


def test_only_three_read_only_operations_and_no_state_directory(service, tmp_path):
    before = service.files.paths.workspace.read_bytes()
    for operation in ("describe", "inspect", "check"):
        result = service.execute(operation, {})
        assert SECRET not in json.dumps(result)
    assert service.describe()["operations"] == ["describe", "inspect", "check"]
    assert service.describe()["api_version"] == 2
    assert service.files.paths.workspace.read_bytes() == before
    assert not (tmp_path / "home").exists()
    for operation in ("prepare", "apply", "validate", "history", "revert", "recover"):
        with pytest.raises(ConfigOperationError, match="describe, inspect and check"):
            service.execute(operation, {})
    with pytest.raises(ConfigOperationError):
        service.execute("check", {"actor": "user"})


@pytest.mark.parametrize("text,code", [
    ("app: [broken", "invalid_yaml"), ("app: {}\napp: {}", "invalid_yaml"),
    ("[]", "invalid_document"),
    ('app: {api_key: synthetic}\nui: {verbositty: debug}', "unknown_field"),
    ('models: {active_main: absent, profiles: {main: {api_key: synthetic}}}', "missing_profile"),
    ('app: {api_key: synthetic}\ncontext: {fixed_prompt_tokens: -1}', "invalid_range"),
])
def test_startup_inspect_and_check_reject_identical_invalid_files(service, monkeypatch, text, code):
    path = service.files.paths.workspace
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", service.files.paths.user)
    monkeypatch.setattr(ConfigLoader, "WORKSPACE_CONFIG_PATH", path)
    for result in (service.inspect(), service.check()):
        assert not result["valid"]
        assert result["diagnostics"][0]["code"] == code
    with pytest.raises(ConfigOperationError) as caught:
        ConfigLoader().load()
    assert caught.value.code == code
    assert path.read_text(encoding="utf-8") == text


def test_startup_does_not_backfill_or_generate_files(service, tmp_path, monkeypatch):
    path = service.files.paths.workspace
    before = path.read_bytes()
    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", service.files.paths.user)
    monkeypatch.setattr(ConfigLoader, "WORKSPACE_CONFIG_PATH", path)
    assert ConfigLoader().load().model == service.inspect()["next_start"]["model"]
    assert before == path.read_bytes()
    path.unlink()
    with pytest.raises(ConfigOperationError):
        ConfigLoader().load()
    assert not path.exists()
    assert not (tmp_path / "home").exists()


def test_buffers_override_only_in_memory_and_follow_physical_aliases(service):
    path = service.files.paths.workspace
    before = path.read_bytes()
    path.write_text("app: [broken", encoding="utf-8")
    alias = ConfigurationService.for_workspace(path.parent.parent, home=path.parent.parent / "home", explicit=path)
    alias.probe = service.probe
    result = alias.check(documents=[{"scope": "explicit", "content": before.decode()}])
    assert result["valid"] and result["buffer_check"]
    assert path.read_text(encoding="utf-8") == "app: [broken"
    assert not alias.inspect()["valid"]
    with pytest.raises(ConfigOperationError, match="Conflicting buffers"):
        alias.check(documents=[{"scope": "workspace", "content": before.decode()}, {"scope": "explicit", "content": "different"}])


def test_external_edits_in_any_layer_invalidate_a_check(service):
    revision = service.inspect()["revision"]
    user = service.files.paths.user
    user.parent.mkdir(parents=True)
    user.write_text("ui: {verbosity: debug}", encoding="utf-8")
    with pytest.raises(ConfigOperationError, match="changed on disk"):
        service.check(base_revision=revision)

    def probe(layers, check, **kwargs):
        user.write_text("ui: {verbosity: standard}", encoding="utf-8")
        return {"check": check, "status": "passed", "code": "ok"}
    service.probe = probe
    with pytest.raises(ConfigOperationError, match="during the check"):
        service.check()


def test_connection_failure_is_separate_from_configuration_validity(service):
    service.probe = lambda layers, check, **kwargs: {"check": check, "status": "unknown", "code": "timeout"}
    result = service.check(checks=["model"])
    assert result["valid"]
    assert result["checks"][-1]["status"] == "unknown"
    assert result["checks"][-1]["profile"] == "default"
    assert "images" in result["checks"][-1]["not_checked"]


def test_explicit_model_checks_have_no_persisted_credentials_or_freshness_state(service):
    path = service.files.paths.workspace
    path.write_text(yaml.safe_dump({"app": {"api_key": SECRET}, "models": {"profiles": {"main": {}, "sub": {"model": "other"}}}}), encoding="utf-8")
    calls = []
    def probe(layers, check, **kwargs):
        calls.append(kwargs.get("profile"))
        return {"check": check, "status": "passed", "code": "ok"}
    service.probe = probe
    service.check(checks=["model"], profiles=["sub"])
    assert calls == ["sub"]
    service.check(checks=["model"], profiles=["sub"])
    assert calls == ["sub", "sub"]
    with pytest.raises(ConfigOperationError):
        service.check(checks=["model"], profiles=["missing"])
    with pytest.raises(ConfigOperationError):
        service.check(checks=["model"], profiles=["main"] * 9)


def test_retired_private_backups_are_left_untouched(service, tmp_path):
    backup = tmp_path / "home/.rcoder/config-management/old/history.json"
    backup.parent.mkdir(parents=True)
    backup.write_text('{"before":"private legacy backup"}')
    before = backup.read_bytes()
    service.inspect()
    service.check()
    assert backup.read_bytes() == before


def test_global_workspace_and_explicit_buffers_keep_inheritance_in_core(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".rcoder").mkdir(parents=True)
    user = home / ".rcoder/config.yaml"
    user.write_text("app: {api_key: inherited-secret, model: global-model}\n", encoding="utf-8")
    workspace = tmp_path / ".rcoder/config.yaml"
    workspace.parent.mkdir()
    workspace.write_text("app: {model: workspace-model}\n", encoding="utf-8")
    explicit = tmp_path / "launch.yaml"
    explicit.write_text("ui: {verbosity: debug}\n", encoding="utf-8")
    service = ConfigurationService.for_workspace(tmp_path, home=home, explicit=explicit)
    service.probe = lambda layers, check: {"check": check, "status": "passed", "code": "ok"}
    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", user)
    monkeypatch.setattr(ConfigLoader, "WORKSPACE_CONFIG_PATH", workspace)
    assert ConfigLoader(explicit).load().model == service.inspect()["next_start"]["model"] == "workspace-model"
    checked = service.check(documents=[{"scope": "user", "content": "app: {api_key: new-secret, model: another-global}\n"}])
    assert checked["valid"] and checked["model_targets"][0]["model"] == "workspace-model"
    inherited = service.check(documents=[{"scope": "workspace", "content": "{}"}])
    assert inherited["model_targets"][0]["model"] == "global-model"
    selected = service.check(documents=[{"scope": "explicit", "content": "app: {model: launch-model}"}])
    assert selected["model_targets"][0]["model"] == "launch-model"
    assert "inherited-secret" not in json.dumps(service.inspect())
    assert ConfigLoader(explicit).load().model == "workspace-model"
