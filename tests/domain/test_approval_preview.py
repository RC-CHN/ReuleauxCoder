import hashlib
from pathlib import Path

import pytest

from reuleauxcoder.domain.approval import (
    ApprovalRequest,
    ApprovalSectionKind,
)
from reuleauxcoder.domain.approval_preview import build_approval_preview, capture_approval_document
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


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_native_diff_and_text_preview_share_the_captured_revision(tmp_path, newline):
    path = tmp_path / "example.py"
    before = f"before = 1{newline}"
    path.write_bytes(before.encode("utf-8"))
    workspace = LocalWorkspacePort(tmp_path, cwd=tmp_path)
    request = ApprovalRequest("edit_file", {"file_path": "example.py", "old_string": "1", "new_string": "2"})
    snapshot = capture_approval_document(request, workspace=workspace)
    path.write_bytes(f"editor changed this{newline}".encode("utf-8"))
    preview = build_approval_preview(request, workspace=workspace, document=snapshot)
    document = preview.documents[0]
    assert document.before == before
    assert document.after == f"before = 2{newline}"
    assert document.before_sha256 == snapshot.revision.sha256 == hashlib.sha256(before.encode("utf-8")).hexdigest()
    assert "-before = 1" in preview.sections[0].content
    assert "+before = 2" in preview.sections[0].content
    assert "editor changed" not in preview.sections[0].content


def test_new_file_preview_retains_absent_base_revision(tmp_path):
    preview = build_approval_preview(
        ApprovalRequest("write_file", {"file_path": "new.py", "content": "中文\n"}),
        workspace=LocalWorkspacePort(tmp_path, cwd=tmp_path),
    )
    assert preview.documents[0].before == ""
    assert preview.documents[0].after == "中文\n"
    assert preview.documents[0].before_exists is False
    assert preview.documents[0].before_sha256 is None
