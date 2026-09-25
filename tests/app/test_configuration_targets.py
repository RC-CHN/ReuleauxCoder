"""Authority follows effective references; verification follows each changed model."""

import json

import pytest
import yaml

from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.domain.config.management import ConfigOperationError


@pytest.fixture
def service(tmp_path):
    home = tmp_path / "home"
    user = home / ".rcoder/config.yaml"
    user.parent.mkdir(parents=True)
    user.write_text(yaml.safe_dump({
        "app": {"api_key": "synthetic-private", "base_url": "https://example.invalid/v1"},
        "models": {"active_main": "main", "active_sub": "sub", "profiles": {
            "main": {"model": "main-model"}, "sub": {"model": "sub-model"},
            "review": {"model": "review-model"},
        }},
        "approval": {"reviewer": "auto_review", "auto_review_model_profile": "review"},
    }))
    instance = ConfigurationService.for_workspace(tmp_path, home=home)
    instance.probe = lambda layers, check, **kw: {"check": check, "status": "passed", "code": "ok"}
    return instance


@pytest.mark.parametrize("path,value,scope", [
    ("/models/profiles/review/model", "other", "workspace"),
    ("/models/profiles/review/base_url", "https://other.invalid/v1", "workspace"),
    ("/models/profiles/review/temperature", 0.9, "user"),
    ("/app/base_url", "https://other.invalid/v1", "workspace"),
    ("/app/max_tokens", 999, "user"),
    ("/models/profiles/review", {"model": "replacement"}, "workspace"),
    ("/mcp/servers/new/command", "unreviewed-program", "workspace"),
    ("/lsp/servers/python/cmd", "unreviewed-program", "workspace"),
])
def test_model_cannot_mutate_referenced_reviewer_or_launchers(service, path, value, scope):
    revision = service.inspect()["revision"]
    with pytest.raises(ConfigOperationError) as caught:
        service.prepare(changes=[{"path": path, "value": value}], scope=scope, actor="model")
    assert caught.value.code == "user_required"
    assert service.inspect()["revision"] == revision
    assert service.history()["changes"] == []


def test_unrelated_model_changes_and_ui_edits_remain_allowed(service):
    candidate = service.prepare(changes=[{"path": "/models/profiles/sub/model", "value": "new-sub"}], actor="model")
    assert [item["profile"] for item in candidate["model_targets"]] == ["sub"]
    assert "synthetic-private" not in json.dumps(candidate)
    assert service.prepare(changes=[{"path": "/ui/verbosity", "value": "debug"}], actor="model")


def test_each_changed_profile_needs_its_own_fresh_success(service):
    candidate = service.prepare(changes=[
        {"path": "/models/profiles/sub/model", "value": "new-sub"},
        {"path": "/models/profiles/review/reasoning_effort_values", "value": {"high": "strong"}},
    ])
    assert {item["profile"] for item in candidate["model_targets"]} == {"sub", "review"}
    calls = []
    def probe(layers, check, *, profile=None):
        calls.append((check, profile))
        return {"check": check, "status": "passed", "code": "ok"}
    service.probe = probe
    service.validate(change_id=candidate["id"], checks=["model"], profiles=["main"])
    with pytest.raises(ConfigOperationError, match="every affected profile"):
        service.apply(change_id=candidate["id"])
    service.validate(change_id=candidate["id"], checks=["model"], profiles=["sub"])
    with pytest.raises(ConfigOperationError):
        service.apply(change_id=candidate["id"])
    result = service.validate(change_id=candidate["id"], checks=["model"], profiles=["review"])
    assert result["checks"][-1]["profile"] == "review"
    assert result["checks"][-1]["checked_at"] > 0
    recorded = service.store.read(candidate["id"])["checks"]
    assert result["checks"][-1] in recorded
    assert "images" in result["checks"][-1]["not_checked"]
    assert service.apply(change_id=candidate["id"])["model_verified"] is True
    assert ("model", "sub") in calls and ("model", "review") in calls


def test_latest_failed_check_replaces_prior_success_only_for_its_profile(service):
    candidate = service.prepare(changes=[{"path": "/models/profiles/sub/max_tokens", "value": 999}])
    service.validate(change_id=candidate["id"], checks=["model"])
    service.probe = lambda layers, check, **kw: {"check": check, "status": "unknown", "code": "timeout"}
    service.validate(change_id=candidate["id"], checks=["model"], profiles=["sub"])
    service.probe = lambda layers, check, **kw: {"check": check, "status": "passed", "code": "ok"}
    with pytest.raises(ConfigOperationError):
        service.apply(change_id=candidate["id"])


def test_schema_capabilities_are_independent_of_release_version(service):
    description = service.describe()
    assert {"effective_profiles", "profile_probes", "reviewer_protection"} <= set(description["capabilities"])


def test_old_validation_contract_cannot_authorize_a_pending_write(service):
    candidate = service.prepare(changes=[{"path": "/ui/verbosity", "value": "debug"}])
    record = service.store.read(candidate["id"])
    record.pop("policy_revision")
    service.store.write(record)
    with pytest.raises(ConfigOperationError, match="prepare this candidate again"):
        service.apply(change_id=candidate["id"])
