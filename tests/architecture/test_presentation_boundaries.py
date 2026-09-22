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
    view_source = (ROOT / "reuleauxcoder/interfaces/cli/views/builtin.py").read_text(
        encoding="utf-8"
    )
    assert "_completed_blocks" not in source
    assert "_compact_tool_output" not in source
    assert "[truncated]" not in source
    assert 'name == "edit_file"' not in source
    assert "event.data" not in source
    assert "AgentEvent" not in source
    assert "event.data" not in view_source


def test_command_application_and_features_do_not_import_frontends() -> None:
    forbidden = ("rich", "textual", "prompt_toolkit", "reuleauxcoder.interfaces")
    violations = []
    paths = [
        *(ROOT / "reuleauxcoder/extensions/command").rglob("*.py"),
        *(ROOT / "reuleauxcoder/app/commands").rglob("*.py"),
        *(ROOT / "reuleauxcoder/app/rpc").rglob("*.py"),
        ROOT / "reuleauxcoder/app/ui_events.py",
        ROOT / "reuleauxcoder/app/interaction_contracts.py",
    ]
    for path in paths:
        for imported in _imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert violations == []


def test_command_frontends_depend_on_contracts_without_dispatch_or_storage():
    forbidden = (
        "reuleauxcoder.app.commands.service",
        "reuleauxcoder.app.commands.registry",
        "reuleauxcoder.extensions.command",
        "reuleauxcoder.infrastructure.persistence",
        "reuleauxcoder.app.rpc.server",
        "reuleauxcoder.domain.agent.agent",
        "reuleauxcoder.domain.agent.loop",
        "reuleauxcoder.domain.context.manager",
        "reuleauxcoder.interfaces.entrypoint",
        "reuleauxcoder.services.llm",
    )
    paths = [
        *(
            path
            for path in (ROOT / "reuleauxcoder/interfaces/cli").rglob("*.py")
            if path.name != "main.py"
        ),
        ROOT / "reuleauxcoder/interfaces/relay.py",
    ]
    for path in paths:
        assert not any(name.startswith(forbidden) for name in _imports(path)), path


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


def test_remote_adapter_submits_only_through_rpc() -> None:
    source = (
        ROOT / "reuleauxcoder" / "interfaces" / "entrypoint" / "remote_relay.py"
    ).read_text(encoding="utf-8")

    assert source.count("command_bus = UIEventBus()") == 1
    assert "RuntimeServer(commands, backend)" in source
    assert ".chat(" not in source
    assert ".submit(" not in source


def test_core_config_parser_does_not_consume_legacy_model_alias() -> None:
    source = (ROOT / "reuleauxcoder" / "services" / "config" / "loader.py").read_text(
        encoding="utf-8"
    )
    parser_source = source.split("def _parse_config", 1)[1]

    assert 'models_config.get("active")' not in parser_source


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
                violations.append(
                    f"{path.relative_to(ROOT)} dynamically mutates {target}"
                )
    assert violations == []
