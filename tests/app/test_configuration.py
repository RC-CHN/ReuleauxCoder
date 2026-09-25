"""Configuration transactions preserve a usable core across errors and crashes."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading

import pytest
import yaml

from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.domain.config.management import ConfigOperationError
from reuleauxcoder.infrastructure.persistence.config_transactions import (
    ConfigPaths,
    ConfigTransactionStore,
)
from reuleauxcoder.services.config.validation import parse_document, resolve_layers


SECRET = "private-api-key-do-not-return"


@pytest.fixture
def service(tmp_path):
    user = tmp_path / "home/.rcoder/config.yaml"
    workspace = tmp_path / "project/.rcoder/config.yaml"
    workspace.parent.mkdir(parents=True)
    workspace.write_text(
        yaml.safe_dump(
            {
                "app": {"api_key": SECRET, "model": "test-model"},
                "ui": {"verbosity": "compact"},
            }
        )
    )
    return ConfigurationService(
        ConfigTransactionStore(ConfigPaths(user, workspace), tmp_path / "state"),
        runtime=lambda: {"model": "running-model", "api_key": SECRET},
        probe=lambda layers, check: {"check": check, "status": "passed", "code": "ok"},
    )


def prepare(service, path="/ui/verbosity", value="standard", **kwargs):
    return service.prepare(changes=[{"path": path, "value": value}], **kwargs)


def test_schema_inspection_and_validation_are_read_only_and_redacted(service):
    before = service.store.paths.workspace.read_bytes()
    description = service.describe()
    inspection = service.inspect()
    validation = service.validate(checks=["static", "startup"])
    assert description["api_version"] == 1
    assert description["activation"] == "next_start"
    assert inspection["valid"] and validation["valid"]
    assert inspection["runtime"]["model"] == "running-model"
    assert inspection["next_start"]["model"] == "test-model"
    assert SECRET not in json.dumps([description, inspection, validation])
    assert service.store.paths.workspace.read_bytes() == before
    assert not service.store.state_dir.exists()
    assert not service.store.paths.user.exists()


@pytest.mark.parametrize(
    "content,code",
    [
        (b"app: [broken", "invalid_yaml"),
        (b"app: {}\napp: {}", "invalid_yaml"),
        (b"[not, an, object]", "invalid_document"),
        (b"app: &loop {next: *loop}", "invalid_document"),
        (b"app: {temperature: .nan}", "invalid_document"),
        (b"\xff", "invalid_document"),
    ],
)
def test_invalid_yaml_is_a_diagnostic_not_an_empty_success(content, code):
    _, issues = parse_document(content, "workspace")
    assert issues and issues[0].code == code


@pytest.mark.parametrize(
    "path,value",
    [
        ("/ui/show_tool_args", "false"),
        ("/ui/max_preview_chars", True),
        ("/app/temperature", 3),
        ("/app/max_tokens", 0),
        ("/models/active_main", "nonexistent"),
        ("/app/provider", "invalid"),
        ("/app/base_url", "https://user:private@example.test/api"),
        ("/lsp/typescript_mode", "unknown"),
        ("/app/unknown_setting", 1),
        ("/mcp/servers/broken/enabled", True),
    ],
)
def test_invalid_candidates_never_replace_working_configuration(service, path, value):
    before = service.store.paths.workspace.read_bytes()
    candidate = prepare(service, path, value)
    assert candidate["diagnostics"]
    with pytest.raises(ConfigOperationError) as caught:
        service.apply(change_id=candidate["id"])
    assert caught.value.code == "validation_required"
    assert service.store.paths.workspace.read_bytes() == before


def test_prepare_apply_restart_and_revert_share_durable_candidates(service):
    initial = service.inspect()
    candidate = prepare(service, base_revision=initial["revision"])
    assert candidate["status"] == "prepared"
    assert service.inspect()["revision"] == initial["revision"]
    restarted = ConfigurationService(service.store, probe=service.probe)
    applied = restarted.apply(change_id=candidate["id"])
    assert applied["status"] == "applied"
    assert applied["activation"] == "next_start"
    assert service.inspect()["next_start"]["ui"]["verbosity"] == "standard"
    assert service.inspect()["runtime"]["model"] == "running-model"
    assert restarted.apply(change_id=candidate["id"]) == applied
    revert = restarted.revert(change_id=candidate["id"])
    restarted.apply(change_id=revert["id"])
    assert service.inspect()["next_start"]["ui"]["verbosity"] == "compact"
    assert SECRET not in json.dumps(service.history())
    if os.name != "nt":
        assert service.store.paths.workspace.stat().st_mode & 0o077 == 0
        assert all(
            path.stat().st_mode & 0o077 == 0
            for path in service.store.directory.glob("*.json")
        )


def test_active_model_requires_live_validation_and_unknown_does_not_pass(service):
    before = service.store.paths.workspace.read_bytes()
    candidate = prepare(service, "/app/model", "replacement-model")
    assert candidate["requires_model_probe"]
    with pytest.raises(ConfigOperationError) as caught:
        service.apply(change_id=candidate["id"])
    assert caught.value.code == "validation_required"
    service.probe = lambda layers, check: {
        "check": check,
        "status": "unknown",
        "code": "timeout",
    }
    validation = service.validate(change_id=candidate["id"], checks=["model"])
    assert validation["valid"] and validation["checks"][-1]["status"] == "unknown"
    assert service.store.paths.workspace.read_bytes() == before
    service.probe = lambda layers, check: {
        "check": check,
        "status": "passed",
        "code": "ok",
    }
    service.validate(change_id=candidate["id"], checks=["model"])
    assert service.apply(change_id=candidate["id"])["model_verified"]


def test_human_can_explicitly_repair_model_offline_but_model_cannot_override(service):
    candidate = prepare(service, "/app/model", "replacement-model", actor="model")
    with pytest.raises(ConfigOperationError) as caught:
        service.apply(
            change_id=candidate["id"], allow_unverified_model=True, actor="model"
        )
    assert caught.value.code == "user_required"
    result = service.apply(change_id=candidate["id"], allow_unverified_model=True)
    assert result["model_verified"] is False


def test_any_layer_change_invalidates_candidate_including_shadowed_user_setting(
    service,
):
    candidate = prepare(service)
    service.store.paths.user.parent.mkdir(parents=True)
    service.store.paths.user.write_text("ui:\n  verbosity: debug\n")
    with pytest.raises(ConfigOperationError) as caught:
        service.apply(change_id=candidate["id"])
    assert caught.value.code == "conflict"


def test_session_runtime_cannot_hide_broken_restart_configuration(service):
    service.store.paths.workspace.write_text("app:\n  api_key: ''\n")
    state = service.inspect()
    assert state["runtime"]["model"] == "running-model"
    assert state["next_start"] is None and not state["valid"]


def test_probes_do_not_hold_commit_lock_and_recheck_after_concurrent_edit(service):
    entered, release = threading.Event(), threading.Event()
    candidate = prepare(service)

    def probe(layers, check):
        entered.set()
        assert release.wait(5)
        return {"check": check, "status": "passed", "code": "ok"}

    service.probe = probe
    with ThreadPoolExecutor() as pool:
        task = pool.submit(service.apply, change_id=candidate["id"])
        try:
            assert entered.wait(5)
            with service.store.locked():
                path = service.store.paths.workspace
                path.write_bytes(
                    path.read_bytes() + b"\nprompt:\n  system_append: concurrent\n"
                )
        finally:
            release.set()
        with pytest.raises(ConfigOperationError) as caught:
            task.result(timeout=5)
        assert caught.value.code == "conflict"


def test_interrupted_commit_is_recovered_without_reapplying_file(service, monkeypatch):
    candidate = prepare(service)
    original = service.store.write

    def crash(record):
        if record["status"] == "applied":
            raise OSError("simulated journal completion failure")
        original(record)

    monkeypatch.setattr(service.store, "write", crash)
    with pytest.raises(OSError):
        service.apply(change_id=candidate["id"])
    assert service.store.read(candidate["id"])["status"] == "committing"
    assert service.inspect()["next_start"]["ui"]["verbosity"] == "standard"
    monkeypatch.setattr(service.store, "write", original)
    monkeypatch.setattr(
        service.store,
        "replace",
        lambda *args: pytest.fail("must not repeat replacement"),
    )
    assert service.apply(change_id=candidate["id"])["status"] == "applied"


def test_failed_replace_retains_original_and_can_retry(service, monkeypatch):
    candidate = prepare(service)
    before = service.store.paths.workspace.read_bytes()
    original = service.store.replace
    monkeypatch.setattr(
        service.store,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk failure")),
    )
    with pytest.raises(OSError):
        service.apply(change_id=candidate["id"])
    assert service.store.paths.workspace.read_bytes() == before
    monkeypatch.setattr(service.store, "replace", original)
    assert service.apply(change_id=candidate["id"])["status"] == "applied"


def test_recovery_can_repair_external_corruption_with_explicit_revision(service):
    candidate = prepare(service)
    service.apply(change_id=candidate["id"])
    service.store.paths.workspace.write_text("app: [broken")
    state = service.inspect()
    assert not state["valid"]
    with pytest.raises(ConfigOperationError):
        service.revert(change_id=candidate["id"])
    recovery = service.recover(
        change_id=candidate["id"], base_revision=state["revision"]
    )
    service.apply(change_id=recovery["id"], allow_unverified_model=True)
    assert service.inspect()["valid"]
    assert service.inspect()["next_start"]["ui"]["verbosity"] == "compact"


@pytest.mark.parametrize(
    "path,value",
    [
        ("/approval/default_mode", "allow"),
        ("/approval", {"default_mode": "allow"}),
        ("/remote_exec/enabled", True),
        ("/modes/profiles/coder/tools", ["*"]),
        ("/app/api_key", "new-secret"),
        ("/models/profiles/new", {"api_key": "nested-secret", "model": "new"}),
        ("/mcp/servers/new", {"command": "server", "env": {"TOKEN": "nested-secret"}}),
    ],
)
def test_model_cannot_bypass_human_fields_with_parent_replacement(service, path, value):
    with pytest.raises(ConfigOperationError) as caught:
        prepare(service, path, value, actor="model")
    assert caught.value.code == "user_required"


def test_model_cannot_apply_human_candidate_or_spoof_actor(service):
    candidate = prepare(service)
    with pytest.raises(ConfigOperationError):
        service.apply(change_id=candidate["id"], actor="model")
    with pytest.raises(ConfigOperationError):
        service.execute(
            "apply", {"change_id": candidate["id"], "actor": "user"}, actor="model"
        )


def test_paths_support_profile_names_with_dots_and_json_pointer_escapes(service):
    candidate = prepare(
        service, "/models/profiles/a.b~1c", {"model": "new", "api_key": "other-key"}
    )
    assert not candidate["diagnostics"]
    assert candidate[
        "requires_model_probe"
    ]  # First profile becomes the effective main profile.
    service.apply(change_id=candidate["id"], allow_unverified_model=True)
    assert "a.b/c" in service.inspect()["next_start"]["model_profiles"]


def test_file_lock_serializes_independent_processes(service):
    script = """
from pathlib import Path
from reuleauxcoder.infrastructure.persistence.config_transactions import ConfigPaths, ConfigTransactionStore
from reuleauxcoder.domain.config.management import ConfigOperationError
import sys
store = ConfigTransactionStore(ConfigPaths(Path('user'), Path('workspace')), Path(sys.argv[1]))
try:
    with store.locked():
        raise AssertionError('lock was bypassed')
except ConfigOperationError as error:
    assert error.code == 'busy'
"""
    with service.store.locked():
        subprocess.run(
            [sys.executable, "-c", script, str(service.store.state_dir)],
            check=True,
            timeout=10,
        )


def test_pure_layer_resolution_does_not_mutate_defaults_or_inputs():
    data = {
        "app": {"api_key": "test"},
        "models": {"profiles": {"p": {"api_key": "test", "model": "test"}}},
    }
    original = json.dumps(data, sort_keys=True)
    resolve_layers(
        [
            ("user", data),
            ("workspace", {"models": {"profiles": {"p": {"temperature": 0.3}}}}),
        ]
    )
    assert json.dumps(data, sort_keys=True) == original


def test_symlink_is_not_replaced_by_management(service, tmp_path):
    target = tmp_path / "real.yaml"
    path = service.store.paths.workspace
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    candidate = prepare(service)
    with pytest.raises(ConfigOperationError) as caught:
        service.apply(change_id=candidate["id"])
    assert caught.value.code == "symlink_target"
    assert path.is_symlink()
    assert yaml.safe_load(target.read_text())["ui"]["verbosity"] == "compact"
