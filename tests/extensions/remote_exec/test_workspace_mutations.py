from types import SimpleNamespace

from reuleauxcoder.domain.workspace import (
    WorkspaceMutationVerification,
    WorkspaceRevision,
)
from reuleauxcoder.extensions.remote_exec.backend import RemoteWorkspacePort


class _Backend:
    def __init__(self, capabilities: set[str]) -> None:
        self.context = SimpleNamespace(workspace_root="/workspace", cwd="/workspace")
        self.capabilities = capabilities

    def supports_capability(self, capability: str) -> bool:
        return capability in self.capabilities


def _revision(content_hash: str, size: int) -> dict[str, object]:
    return {
        "exists": True,
        "sha256": content_hash,
        "size_bytes": size,
        "mtime_ns": 123,
        "authoritative": True,
    }


def _receipt(
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    return {
        "resolved_path": "/workspace/demo.txt",
        "before": before,
        "intended_after_sha256": after["sha256"],
        "intended_size_bytes": after["size_bytes"],
        "observed_after": after,
        "atomic_replace": True,
        "verification": "applied_verified",
        "expected_before": before,
        "external_change_before_write": False,
    }


def test_remote_verified_primitives_preserve_peer_revision_and_receipt(
    monkeypatch,
) -> None:
    capabilities = {
        "workspace.fs.snapshot_text",
        "workspace.fs.write_text_verified",
    }
    port = RemoteWorkspacePort(_Backend(capabilities))  # type: ignore[arg-type]
    before = _revision("a" * 64, 3)
    after = _revision("b" * 64, 3)
    requests = []

    def request(operation: str, **arguments):
        requests.append((operation, arguments))
        if operation == "fs.snapshot_text":
            return {
                "resolved_path": "/workspace/demo.txt",
                "content": "old",
                "revision": before,
            }
        return {
            "old_content": "old",
            "new_content": "new",
            "mutation_receipt": _receipt(before, after),
        }

    monkeypatch.setattr(port, "_request", request)

    snapshot = port.snapshot_text("demo.txt")
    result = port.write_text_verified(
        "demo.txt",
        "new",
        expected_revision=snapshot.revision,
    )

    assert snapshot.revision.authoritative is True
    assert result.receipt.verification is WorkspaceMutationVerification.APPLIED_VERIFIED
    assert result.receipt.observed_after == WorkspaceRevision.from_dict(after)
    assert requests == [
        ("fs.snapshot_text", {"path": "demo.txt"}),
        (
            "fs.write_text_verified",
            {
                "path": "demo.txt",
                "content": "new",
                "expected_revision": snapshot.revision.to_dict(),
            },
        ),
    ]


def test_legacy_remote_peer_is_compatible_but_never_claims_verification(
    monkeypatch,
) -> None:
    port = RemoteWorkspacePort(_Backend(set()))  # type: ignore[arg-type]
    requests = []
    reads = iter(("old", "new"))

    def request(operation: str, **arguments):
        requests.append((operation, arguments))
        if operation == "fs.read_text":
            return {"content": next(reads)}
        return {"old_content": "old"}

    monkeypatch.setattr(port, "_request", request)

    result = port.write_text_verified("demo.txt", "new")

    assert result.old_content == "old"
    assert result.new_content == "new"
    assert result.receipt.verification is WorkspaceMutationVerification.UNKNOWN
    assert result.receipt.before.authoritative is False
    assert result.receipt.observed_after is not None
    assert result.receipt.observed_after.authoritative is False
    assert [operation for operation, _arguments in requests] == [
        "fs.read_text",
        "fs.write_text_atomic",
        "fs.read_text",
    ]
