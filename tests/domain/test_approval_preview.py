from pathlib import Path

from reuleauxcoder.domain.approval import (
    ApprovalRequest,
    ApprovalSectionKind,
)
from reuleauxcoder.domain.approval_preview import build_approval_preview
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


def test_edit_preview_uses_supplied_workspace_not_host_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    host = tmp_path / "host"
    peer = tmp_path / "peer"
    host.mkdir()
    peer.mkdir()
    (host / "demo.txt").write_text("host value\n")
    (peer / "demo.txt").write_text("peer value\n")
    monkeypatch.chdir(host)
    request = ApprovalRequest(
        tool_name="edit_file",
        tool_args={
            "file_path": "demo.txt",
            "old_string": "peer",
            "new_string": "remote",
        },
    )

    preview = build_approval_preview(
        request,
        workspace=LocalWorkspacePort(peer, cwd=peer),
    )

    assert len(preview.sections) == 1
    section = preview.sections[0]
    assert section.kind is ApprovalSectionKind.DIFF
    assert "-peer value" in section.content
    assert "+remote value" in section.content
    assert "host value" not in section.content


def test_non_file_approval_has_typed_arguments_section() -> None:
    request = ApprovalRequest(
        tool_name="shell",
        tool_args={"command": "echo hi"},
    )

    preview = build_approval_preview(request, workspace=None)

    assert preview.sections[0].kind is ApprovalSectionKind.JSON
    assert preview.sections[0].content == {"command": "echo hi"}


def test_read_only_approval_has_compact_target_instead_of_json() -> None:
    request = ApprovalRequest(
        tool_name="read_file",
        tool_args={"file_path": "CHANGELOG.md", "offset": 1, "limit": 10},
    )

    preview = build_approval_preview(request, workspace=None)

    assert preview.sections[0].kind is ApprovalSectionKind.TEXT
    assert preview.sections[0].title == "Target"
    assert preview.sections[0].content == "CHANGELOG.md · from line 1 · limit 10"
