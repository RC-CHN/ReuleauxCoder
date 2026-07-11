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
    source = (ROOT / "reuleauxcoder/interfaces/cli/render.py").read_text(encoding="utf-8")
    view_source = (
        ROOT / "reuleauxcoder/interfaces/cli/views/builtin.py"
    ).read_text(encoding="utf-8")
    assert "_completed_blocks" not in source
    assert "_compact_tool_output" not in source
    assert "[truncated]" not in source
    assert 'name == "edit_file"' not in source
    assert "event.data" not in source
    assert "event.data" not in view_source


def test_command_extensions_do_not_import_cli_or_ui_frameworks() -> None:
    forbidden = ("rich", "textual", "reuleauxcoder.interfaces.cli")
    violations = []
    command_root = ROOT / "reuleauxcoder" / "extensions" / "command"
    for path in command_root.rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert violations == []


def test_command_handlers_only_use_single_typed_effect_channel() -> None:
    command_root = ROOT / "reuleauxcoder" / "extensions" / "command"
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in command_root.rglob("*.py")
    )
    assert "ctx.ui_bus" not in sources
    assert "CommandEffectBuilder" not in sources
    assert "CommandResult" not in sources

    models = (ROOT / "reuleauxcoder" / "app" / "commands" / "models.py").read_text(
        encoding="utf-8"
    )
    view_models = (
        ROOT / "reuleauxcoder" / "app" / "commands" / "view_models.py"
    ).read_text(encoding="utf-8")
    assert "CommandEffectBuilder" not in models
    assert "MarkdownViewModel" not in view_models
    assert "DataViewModel" not in view_models
    assert "view_model_from_payload" not in view_models


def test_runtime_does_not_dynamically_inject_agent_dependencies() -> None:
    violations = []
    root = ROOT / "reuleauxcoder"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "setattr":
                continue
            if not node.args:
                continue
            target = ast.unparse(node.args[0])
            if target.endswith("agent") or target.endswith(".agent"):
                violations.append(f"{path.relative_to(ROOT)} dynamically mutates {target}")
    assert violations == []
