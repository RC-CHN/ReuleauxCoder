from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _python_sources(relative_root: str) -> str:
    root = ROOT / relative_root
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    )


def test_builtin_contributions_do_not_register_during_import() -> None:
    command_sources = _python_sources("reuleauxcoder/extensions/command/builtin")
    tool_sources = _python_sources("reuleauxcoder/extensions/tools/builtin")
    hook_sources = _python_sources("reuleauxcoder/domain/hooks/builtin")

    assert "register_command_module" not in command_sources
    assert "@register_tool" not in tool_sources
    assert "@register_hook" not in hook_sources


def test_builtin_loaders_do_not_scan_packages_to_trigger_registration() -> None:
    sources = "\n".join(
        (
            (ROOT / "reuleauxcoder/app/commands/loader.py").read_text(
                encoding="utf-8"
            ),
            (ROOT / "reuleauxcoder/extensions/tools/registry.py").read_text(
                encoding="utf-8"
            ),
            (ROOT / "reuleauxcoder/domain/hooks/discovery.py").read_text(
                encoding="utf-8"
            ),
        )
    )

    assert "iter_modules" not in sources
    assert "import_module" not in sources


def test_obsolete_decorator_view_registry_is_removed() -> None:
    assert not (ROOT / "reuleauxcoder/interfaces/view_registration.py").exists()


def test_tui_application_does_not_own_command_panel_business_types() -> None:
    source = (ROOT / "reuleauxcoder/interfaces/tui/application.py").read_text(
        encoding="utf-8"
    )

    forbidden = (
        "ApprovalView",
        "MCPServersView",
        "ModelListViewModel",
        "ModesViewModel",
        "SessionsViewModel",
        "SkillsViewModel",
        "SubagentJobsViewModel",
        "ThinkingEffortViewModel",
    )
    assert all(name not in source for name in forbidden)


def test_cli_package_contains_no_tui_specific_modules() -> None:
    cli_root = ROOT / "reuleauxcoder/interfaces/cli"
    forbidden = (
        "mini_tui.py",
        "command_popup.py",
        "markdown_fragments.py",
        "selection_panel.py",
        "transcript_benchmark.py",
        "virtual_transcript.py",
    )

    assert all(not (cli_root / filename).exists() for filename in forbidden)
