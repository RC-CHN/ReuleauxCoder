from pathlib import Path

from reuleauxcoder.extensions.tools.backend import LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


class _Manager:
    def __init__(self, source: Path):
        self.source = source
        self.calls: list[tuple[str, dict]] = []

    def send_request_sync(self, path, method, params, cancellation=None):
        self.calls.append((method, params))
        location = {
            "uri": self.source.as_uri(),
            "range": {"start": {"line": 1, "character": 4}},
        }
        if method == "workspace/symbol":
            return [{"name": "Agent", "kind": 5, "location": location}]
        if method == "textDocument/prepareCallHierarchy":
            return [{"name": "run", "kind": 12, **location}]
        if method == "callHierarchy/incomingCalls":
            return [{"from": {"name": "main", **location}, "fromRanges": []}]
        if method == "callHierarchy/outgoingCalls":
            return [{"to": {"name": "execute", **location}, "fromRanges": []}]
        if method == "textDocument/references":
            return [location]
        raise AssertionError(method)


def _tool(tmp_path: Path):
    source = tmp_path / "main.py"
    source.write_text("def run():\n    return 1\n\nprint(run())\n", encoding="utf-8")
    manager = _Manager(source)
    backend = LocalToolBackend(workspace=LocalWorkspacePort(tmp_path))
    return source, manager, LspTool(backend=backend, lsp_manager=manager)


def test_workspace_symbol_uses_file_anchor(tmp_path: Path) -> None:
    source, manager, tool = _tool(tmp_path)

    outcome = tool.execute(
        operation="workspaceSymbol", filePath=str(source), query="Agent"
    )

    assert outcome.success
    assert manager.calls[0][0] == "workspace/symbol"
    assert manager.calls[0][1] == {"query": "Agent"}
    assert "[class] Agent" in outcome.content
    assert "main.py:2:5" in outcome.content


def test_call_hierarchy_supports_both_directions(tmp_path: Path) -> None:
    source, manager, tool = _tool(tmp_path)

    outcome = tool.execute(
        operation="callHierarchy", filePath=str(source), line=1, character=5
    )

    assert outcome.success
    methods = [method for method, _ in manager.calls]
    assert "textDocument/prepareCallHierarchy" in methods
    assert "callHierarchy/incomingCalls" in methods
    assert "callHierarchy/outgoingCalls" in methods
    assert "incoming" in outcome.content
    assert "outgoing" in outcome.content
    assert "main" in outcome.content
    assert "execute" in outcome.content


def test_batch_references_include_bounded_source_context(tmp_path: Path) -> None:
    source, manager, tool = _tool(tmp_path)

    outcome = tool.execute(
        operation="batchReferences",
        filePath=str(source),
        positions=[{"filePath": str(source), "line": 1, "character": 5}],
        context_lines=1,
    )

    assert outcome.success
    assert manager.calls[-1][1]["context"] == {"includeDeclaration": True}
    assert "1 references" in outcome.content
    assert ">    2 |     return 1" in outcome.content
    assert "    1 | def run():" in outcome.content
