from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_presentation_core_has_no_ui_framework_or_io_dependencies() -> None:
    forbidden = (
        "rich",
        "textual",
        "prompt_toolkit",
        "openai",
        "reuleauxcoder.interfaces",
        "reuleauxcoder.services.llm",
    )
    violations = []
    for path in (ROOT / "reuleauxcoder" / "presentation").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert violations == []


def test_cli_renderer_does_not_restore_legacy_string_protocols() -> None:
    source = (ROOT / "reuleauxcoder/interfaces/cli/render.py").read_text(
        encoding="utf-8"
    )
    assert "_completed_blocks" not in source
    assert "_compact_tool_output" not in source
    assert "[truncated]" not in source
    assert 'name == "edit_file"' not in source


def test_command_extensions_do_not_import_cli_or_ui_frameworks() -> None:
    forbidden = ("rich", "textual", "reuleauxcoder.interfaces.cli")
    violations = []
    command_root = ROOT / "reuleauxcoder" / "extensions" / "command"
    for path in command_root.rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert violations == []
