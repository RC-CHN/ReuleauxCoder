from types import SimpleNamespace

import reuleauxcoder.extensions.remote_exec.backend as backend_module
from reuleauxcoder.domain.workspace import (
    WorkspaceEntry,
    WorkspaceGlobResult,
    WorkspaceSearchMatch,
    WorkspaceSearchResult,
)
from reuleauxcoder.extensions.remote_exec.backend import (
    RemoteWorkspacePort,
    _peer_glob_safe,
    _peer_literal_search_safe,
)


class _Backend:
    def __init__(self, capabilities: set[str]) -> None:
        self.context = SimpleNamespace(workspace_root="/workspace", cwd="/workspace")
        self.capabilities = capabilities

    def supports_capability(self, capability: str) -> bool:
        return capability in self.capabilities


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


def test_remote_literal_search_uses_single_peer_primitive(monkeypatch) -> None:
    port = RemoteWorkspacePort(  # type: ignore[arg-type]
        _Backend({"workspace.fs.search_text"})
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
        }

    monkeypatch.setattr(port, "_request", request)

    result = port.search_text("class Agent", ".", include="*.py")

    assert result == WorkspaceSearchResult(
        matches=(
            WorkspaceSearchMatch(
                path="/workspace/demo.py",
                line_number=4,
                line="class Agent:",
            ),
        ),
        truncated=False,
    )
    assert [operation for operation, _arguments in requests] == ["fs.search_text"]


def test_unsafe_or_unadvertised_searches_keep_compatibility_fallback(
    monkeypatch,
) -> None:
    port = RemoteWorkspacePort(_Backend(set()))  # type: ignore[arg-type]
    expected_glob = WorkspaceGlobResult((), 0)
    expected_search = WorkspaceSearchResult(())
    monkeypatch.setattr(
        backend_module,
        "glob_paths_via_primitives",
        lambda *_args, **_kwargs: expected_glob,
    )
    monkeypatch.setattr(
        backend_module,
        "search_text_via_primitives",
        lambda *_args, **_kwargs: expected_search,
    )

    assert port.glob_paths("**/*.py", ".") is expected_glob
    assert port.search_text(r"class\s+Agent", ".") is expected_search
    assert _peer_glob_safe("src/[ab].py") is False
    assert _peer_literal_search_safe(r"class\s+Agent", "*.py") is False
