from types import SimpleNamespace

import pytest

import reuleauxcoder.extensions.remote_exec.backend as backend_module
from reuleauxcoder.domain.workspace import (
    WorkspaceEntry,
    WorkspaceError,
    WorkspaceGlobResult,
    WorkspaceSearchMatch,
    WorkspaceSearchResult,
)
from reuleauxcoder.extensions.remote_exec.backend import (
    RemoteWorkspacePort,
    _peer_glob_safe,
)


class _Backend:
    def __init__(self, capabilities: set[str]) -> None:
        self.context = SimpleNamespace(workspace_root="/workspace", cwd="/workspace")
        self.capabilities = capabilities

    def supports_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def resolve_peer_id(self) -> str:
        return "peer"


def _entry() -> dict[str, object]:
    return {
        "path": "/workspace/demo.py",
        "relative_path": "demo.py",
        "name": "demo.py",
        "is_file": True,
        "is_dir": False,
        "size": 12,
        "mtime": 20.0,
        "mode": 0o100600,
    }


def test_remote_glob_uses_single_peer_primitive_when_semantics_are_safe(
    monkeypatch,
) -> None:
    port = RemoteWorkspacePort(_Backend({"workspace.fs.glob"}))  # type: ignore[arg-type]
    requests = []

    def request(operation: str, **arguments):
        requests.append((operation, arguments))
        return {
            "entries": [_entry()],
            "match_count": 1,
            "listing_truncated": False,
        }

    monkeypatch.setattr(port, "_request", request)

    result = port.glob_paths("**/*.py", ".")

    assert result == WorkspaceGlobResult(
        entries=(WorkspaceEntry(**_entry()),),
        match_count=1,
        listing_truncated=False,
    )
    assert [operation for operation, _arguments in requests] == ["fs.glob"]


def test_remote_regex_search_uses_single_peer_primitive(monkeypatch) -> None:
    port = RemoteWorkspacePort(  # type: ignore[arg-type]
        _Backend({"workspace.fs.search_text.bounded"})
    )
    requests = []

    def request(operation: str, **arguments):
        requests.append((operation, arguments))
        return {
            "matches": [
                {
                    "path": "/workspace/demo.py",
                    "line_number": 4,
                    "line": "class Agent:",
                }
            ],
            "truncated": False,
            "reasons": [],
            "scanned_files": 1,
            "scanned_bytes": 12,
        }

    monkeypatch.setattr(port, "_request", request)

    result = port.search_text(r"class\s+Agent", ".", include="src/**/*.py")

    assert result == WorkspaceSearchResult(
        matches=(
            WorkspaceSearchMatch(
                path="/workspace/demo.py",
                line_number=4,
                line="class Agent:",
            ),
        ),
        truncated=False,
        scanned_files=1,
        scanned_bytes=12,
    )
    assert [operation for operation, _arguments in requests] == ["fs.search_text"]
    assert requests[0][1]["pattern"] == r"class\s+Agent"
    assert requests[0][1]["literal"] is False
    assert requests[0][1]["limits"]["max_scan_bytes"] > 0


def test_old_peer_requires_upgrade_for_search_but_keeps_glob_fallback(
    monkeypatch,
) -> None:
    port = RemoteWorkspacePort(_Backend(set()))  # type: ignore[arg-type]
    expected_glob = WorkspaceGlobResult((), 0)
    monkeypatch.setattr(
        backend_module,
        "glob_paths_via_primitives",
        lambda *_args, **_kwargs: expected_glob,
    )

    assert port.glob_paths("**/*.py", ".") is expected_glob
    with pytest.raises(WorkspaceError, match="upgraded rcoder-peer"):
        port.search_text(r"class\s+Agent", ".")
    assert _peer_glob_safe("src/[ab].py") is False
