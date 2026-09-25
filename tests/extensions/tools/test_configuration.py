"""Model adapters cannot obtain the authority of human configuration clients."""

import json

from reuleauxcoder.app.configuration import ConfigurationService
from reuleauxcoder.domain.agent.tool_outcome import ToolOutcomeStatus
from reuleauxcoder.extensions.tools.builtin.configuration import (
    ConfigApplyTool,
    ConfigPrepareTool,
    ConfigReadTool,
    ConfigValidateTool,
)


def test_model_tools_share_candidates_and_apply_authority(tmp_path):
    path = tmp_path / ".rcoder/config.yaml"
    path.parent.mkdir()
    path.write_text("app:\n  api_key: private-config-token\n", encoding="utf-8")
    service = ConfigurationService.for_workspace(tmp_path, home=tmp_path / "home")
    service.probe = lambda layers, check: {
        "check": check,
        "status": "passed",
        "code": "ok",
    }
    read, prepare, validate, apply = (
        ConfigReadTool(),
        ConfigPrepareTool(),
        ConfigValidateTool(),
        ConfigApplyTool(),
    )
    for tool in (read, prepare, validate, apply):
        tool.bind_configuration(service)
    state = json.loads(read.execute("inspect").content)
    assert "private-config-token" not in json.dumps(state)
    assert (
        "profiles"
        in json.loads(read.execute("describe", section="models").content)["schema"][
            "properties"
        ]
    )
    candidate = json.loads(
        prepare.execute(
            "prepare",
            base_revision=state["revision"],
            changes=[{"path": "/ui/verbosity", "value": "debug"}],
        ).content
    )
    assert json.loads(validate.execute(change_id=candidate["id"]).content)["valid"]
    preview = validate.approval_preview(
        {"change_id": candidate["id"], "checks": ["model"]}
    )
    assert preview.sections[0].content["model"]["model"] == "gpt-4o"
    assert "private-config-token" not in json.dumps(dict(preview.sections[0].content))
    assert json.loads(apply.execute(candidate["id"]).content)["status"] == "applied"
    assert "private-config-token" not in json.dumps(service.history())
    denied = prepare.execute(
        "prepare", changes=[{"path": "/approval/default_mode", "value": "allow"}]
    )
    assert denied.status is ToolOutcomeStatus.FAILED
    assert "user_required" in denied.content
    spoofed = validate.execute(operation="apply", change_id=candidate["id"])
    assert spoofed.status is ToolOutcomeStatus.FAILED
    human = service.prepare(changes=[{"path": "/ui/verbosity", "value": "compact"}])
    assert apply.execute(human["id"]).status is ToolOutcomeStatus.FAILED


def test_scoped_configuration_clone_has_no_host_management_service(tmp_path):
    tool = ConfigApplyTool()
    tool.bind_configuration(ConfigurationService.for_workspace(tmp_path, home=tmp_path))
    cloned = tool.clone_for_scope("subagent")
    result = cloned.execute("0" * 32)
    assert result.status is ToolOutcomeStatus.FAILED
    assert "owning workspace host" in result.content
