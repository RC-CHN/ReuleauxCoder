"""Tests for ProjectContextHook — multi-file loading and injection."""

from pathlib import Path
from unittest.mock import MagicMock

from reuleauxcoder.domain.hooks.builtin.project_context import (
    DEFAULT_CONTEXT_FILES,
    ProjectContextHook,
    ProjectContextSnapshot,
    ProjectContextStartupObservationError,
    ProjectContextStartupNotifier,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    HookPoint,
    RunnerStartupContext,
)
from reuleauxcoder.domain.llm.context_messages import (
    SYNTHETIC_CONTEXT_METADATA_KEY,
)


def _make_context(messages: list[dict] | None = None) -> BeforeLLMRequestContext:
    return BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        request_params={"model": "gpt-4o", "messages": list(messages or [])},
        messages=list(messages or []),
        model="gpt-4o",
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_returns_empty_when_no_files(tmp_path: Path, monkeypatch) -> None:
    """No context files → empty list."""
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    assert hook._load_project_context_snapshot() == ProjectContextSnapshot()


def test_load_finds_agent_md(tmp_path: Path, monkeypatch) -> None:
    """AGENT.md exists → one entry."""
    (tmp_path / "AGENT.md").write_text("Project rules")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    parts = hook._load_project_context_snapshot().parts
    assert parts == (("AGENT.md", "Project rules"),)


def test_load_collects_all_existing_in_fixed_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Multiple candidates → all found, in DEFAULT_CONTEXT_FILES order."""
    (tmp_path / "CLAUDE.md").write_text("CLAUDE")
    (tmp_path / "AGENT.md").write_text("AGENT")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    parts = hook._load_project_context_snapshot().parts
    # AGENT.md before CLAUDE.md
    assert parts == (("AGENT.md", "AGENT"), ("CLAUDE.md", "CLAUDE"))


def test_load_skips_empty_files(tmp_path: Path, monkeypatch) -> None:
    """Empty file → not included."""
    (tmp_path / "AGENT.md").write_text("")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    assert hook._load_project_context_snapshot().parts == ()


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


def test_run_injects_single_file(tmp_path: Path, monkeypatch) -> None:
    """Single AGENT.md → one system message at index 1."""
    (tmp_path / "AGENT.md").write_text("Rule: use Chinese")
    monkeypatch.chdir(tmp_path)

    messages = [
        {"role": "system", "content": "[system prompt]"},
        {"role": "user", "content": "hello"},
    ]
    context = _make_context(messages)
    hook = ProjectContextHook()
    result = hook.run(context)

    assert len(result.messages) == 3
    assert result.messages[0]["content"] == "[system prompt]"
    assert result.messages[1]["role"] == "user"
    assert result.messages[1]["content"].startswith("<project_context")
    assert "Rule: use Chinese" in result.messages[1]["content"]
    assert "--- AGENT.md ---" in result.messages[1]["content"]
    assert result.messages[2] == {"role": "user", "content": "hello"}


def test_run_concatenates_multiple_files(tmp_path: Path, monkeypatch) -> None:
    """Multiple files → concatenated in order into one system message."""
    (tmp_path / "AGENT.md").write_text("Alpha")
    (tmp_path / "CLAUDE.md").write_text("Charlie")
    monkeypatch.chdir(tmp_path)

    messages = [
        {"role": "system", "content": "[system prompt]"},
        {"role": "user", "content": "hello"},
    ]
    context = _make_context(messages)
    hook = ProjectContextHook()
    result = hook.run(context)

    assert len(result.messages) == 3
    msg = result.messages[1]
    assert msg["role"] == "user"
    # Order: AGENT.md before CLAUDE.md
    agent_idx = msg["content"].index("--- AGENT.md ---")
    claude_idx = msg["content"].index("--- CLAUDE.md ---")
    assert agent_idx < claude_idx
    assert "Alpha" in msg["content"]
    assert "Charlie" in msg["content"]


def test_run_noop_when_no_files(tmp_path: Path, monkeypatch) -> None:
    """Without context files, messages stay unchanged."""
    monkeypatch.chdir(tmp_path)
    messages = [
        {"role": "system", "content": "[system prompt]"},
        {"role": "user", "content": "hello"},
    ]
    context = _make_context(messages)
    hook = ProjectContextHook()
    result = hook.run(context)
    assert result.messages == messages


def test_transformed_messages_flow_to_request_params() -> None:
    """After transform, context.messages carries injected content."""
    hook = ProjectContextHook()
    # Stub loader so we don't depend on filesystem state
    hook._load_project_context_snapshot = lambda: ProjectContextSnapshot(
        (("AGENT.md", "Keep it short"),)
    )

    messages = [
        {"role": "system", "content": "[system prompt]"},
        {"role": "user", "content": "fix the bug"},
    ]
    context = _make_context(messages)
    result = hook.run(context)

    assert len(result.messages) == 3
    assert result.messages[1]["role"] == "user"
    assert "Keep it short" in result.messages[1]["content"]


# ---------------------------------------------------------------------------
# Order stability
# ---------------------------------------------------------------------------


def test_default_context_files_order_is_stable() -> None:
    """DEFAULT_CONTEXT_FILES is deterministic — must not change accidentally."""
    assert DEFAULT_CONTEXT_FILES == [
        "AGENT.md",
        "AGENTS.md",
        ".agent.md",
        "CLAUDE.md",
        ".claude.md",
    ]


def test_concatenated_message_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    """Running the hook twice with same files → identical output."""
    (tmp_path / "AGENT.md").write_text("A")
    (tmp_path / "CLAUDE.md").write_text("B")
    monkeypatch.chdir(tmp_path)

    hook = ProjectContextHook()
    messages = [
        {"role": "system", "content": "[prompt]"},
        {"role": "user", "content": "hi"},
    ]

    r1 = hook.run(_make_context(messages))
    r2 = hook.run(_make_context(messages))
    assert r1.messages == r2.messages


def test_context_file_content_is_cached_until_metadata_changes(
    tmp_path: Path, monkeypatch
) -> None:
    context_file = tmp_path / "AGENT.md"
    context_file.write_text("first", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    original_read_text = Path.read_text
    reads = 0

    def counted_read_text(path: Path, *args, **kwargs):
        nonlocal reads
        if path.name == "AGENT.md":
            reads += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)
    hook = ProjectContextHook()

    assert hook._load_project_context_snapshot().parts == (("AGENT.md", "first"),)
    assert hook._load_project_context_snapshot().parts == (("AGENT.md", "first"),)
    assert reads == 1

    context_file.write_text("second value", encoding="utf-8")
    assert hook._load_project_context_snapshot().parts == (
        ("AGENT.md", "second value"),
    )
    assert reads == 2


def test_first_decode_failure_is_injected_as_safe_session_diagnostic(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"\xffprivate-bytes")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()

    result = hook.run(
        _make_context(
            [
                {"role": "system", "content": "[prompt]"},
                {"role": "user", "content": "help"},
            ]
        )
    )

    assert len(result.messages) == 3
    diagnostic = result.messages[1]
    assert diagnostic[SYNTHETIC_CONTEXT_METADATA_KEY] == {
        "tag": "session_diagnostic",
        "source": "workspace_instruction_loader",
    }
    assert (
        "phase=decode error_type=UnicodeDecodeError ref=AGENTS.md"
        in diagnostic["content"]
    )
    assert "instruction_state=partial_or_unavailable" in diagnostic["content"]
    assert "private-bytes" not in diagnostic["content"]
    assert result.messages[2]["content"] == "help"


def test_read_failure_retains_last_good_instructions_and_reports_failure(
    tmp_path: Path, monkeypatch
) -> None:
    context_file = tmp_path / "AGENT.md"
    context_file.write_text("stable previous rules", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    assert hook._load_project_context_snapshot().parts == (
        ("AGENT.md", "stable previous rules"),
    )

    context_file.write_text("unreadable replacement", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_instruction_read(path: Path, *args, **kwargs):
        if path == context_file:
            raise PermissionError("credential=must-not-leak")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_instruction_read)
    result = hook.run(
        _make_context(
            [
                {"role": "system", "content": "[prompt]"},
                {"role": "user", "content": "help"},
            ]
        )
    )

    assert len(result.messages) == 4
    assert "stable previous rules" in result.messages[1]["content"]
    assert "unreadable replacement" not in result.messages[1]["content"]
    diagnostic = result.messages[2]["content"]
    assert "phase=read error_type=PermissionError ref=AGENT.md" in diagnostic
    assert "instruction_state=last_good_snapshot_retained" in diagnostic
    assert "credential=must-not-leak" not in diagnostic


def test_stat_failure_is_not_treated_as_missing_and_hides_unsafe_ref(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    configured_ref = "nested/private-rules.md"
    target = tmp_path / configured_ref
    original_stat = Path.stat

    def fail_instruction_stat(path: Path, *args, **kwargs):
        if path == target:
            raise PermissionError("/tenant/secret/path")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_instruction_stat)
    hook = ProjectContextHook(context_files=[configured_ref])
    snapshot = hook._load_project_context_snapshot()

    assert snapshot.parts == ()
    assert len(snapshot.failures) == 1
    assert snapshot.failures[0].phase == "stat"
    assert snapshot.failures[0].error_type == "PermissionError"
    assert snapshot.failures[0].ref == "context_file_1"
    rendered = hook.run(_make_context()).messages[0]["content"]
    assert "phase=stat error_type=PermissionError ref=context_file_1" in rendered
    assert configured_ref not in rendered
    assert "/tenant/secret/path" not in rendered


def test_failed_read_is_retried_without_metadata_change(
    tmp_path: Path, monkeypatch
) -> None:
    context_file = tmp_path / "AGENT.md"
    context_file.write_text("rules", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    original_read_text = Path.read_text
    attempts = 0

    def transient_read(path: Path, *args, **kwargs):
        nonlocal attempts
        if path == context_file:
            attempts += 1
            if attempts == 1:
                raise PermissionError("transient secret")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient_read)
    hook = ProjectContextHook()

    first = hook._load_project_context_snapshot()
    second = hook._load_project_context_snapshot()

    assert first.parts == ()
    assert first.failures[0].error_type == "PermissionError"
    assert second.parts == (("AGENT.md", "rules"),)
    assert second.failures == ()
    assert attempts == 2


def test_unknown_workspace_identity_never_reuses_previous_instructions(
    tmp_path: Path, monkeypatch
) -> None:
    previous_workspace = tmp_path / "previous"
    previous_workspace.mkdir()
    (previous_workspace / "AGENT.md").write_text("previous secret rules")
    monkeypatch.chdir(previous_workspace)
    hook = ProjectContextHook()
    assert hook._load_project_context_snapshot().parts == (
        ("AGENT.md", "previous secret rules"),
    )

    def fail_cwd(cls):
        del cls
        raise PermissionError("cwd-secret=must-not-leak")

    monkeypatch.setattr(Path, "cwd", classmethod(fail_cwd))
    snapshot = hook._load_project_context_snapshot()

    assert snapshot.parts == ()
    assert snapshot.retained_last_good is False
    assert snapshot.failures[0].phase == "workspace"
    assert snapshot.failures[0].error_type == "PermissionError"
    rendered = hook.run(_make_context()).messages[0]["content"]
    assert "instruction_state=partial_or_unavailable" in rendered
    assert "previous secret rules" not in rendered
    assert "cwd-secret=must-not-leak" not in rendered


def test_startup_stat_failure_is_isolated_with_safe_hook_diagnostic(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "AGENTS.md"
    original_stat = Path.stat

    def fail_instruction_stat(path: Path, *args, **kwargs):
        if path == target:
            raise PermissionError("startup-token=must-not-leak")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_instruction_stat)
    registry = HookRegistry()
    registry.register(HookPoint.RUNNER_STARTUP, ProjectContextStartupNotifier())
    diagnostics = registry.run_observers(
        HookPoint.RUNNER_STARTUP,
        RunnerStartupContext(hook_point=HookPoint.RUNNER_STARTUP),
    )

    assert len(diagnostics) == 1
    assert (
        "phase=stat, error_type=PermissionError, ref=AGENTS.md"
        in diagnostics[0].message
    )
    assert "startup-token=must-not-leak" not in diagnostics[0].message


def test_startup_observation_error_exposes_only_validated_failure_facts() -> None:
    safe = ProjectContextStartupObservationError(
        phase="stat",
        error_type="PermissionError",
        ref="AGENTS.md",
    )
    assert safe.phase == "stat"
    assert safe.error_type == "PermissionError"
    assert safe.ref == "AGENTS.md"

    unsafe = ProjectContextStartupObservationError(
        phase="notify\nsecret",
        error_type="BadError\nsecret",
        ref="../../private/path",
    )
    assert unsafe.phase == "observe"
    assert unsafe.error_type == "Exception"
    assert unsafe.ref == "project_context"
    assert "secret" not in str(unsafe)
    assert "private/path" not in str(unsafe)


def test_startup_ui_failure_is_isolated_with_safe_hook_diagnostic(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "AGENT.md").write_text("rules", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    ui_bus = MagicMock()
    ui_bus.info.side_effect = PermissionError("ui-token=must-not-leak")
    registry = HookRegistry()
    registry.register(HookPoint.RUNNER_STARTUP, ProjectContextStartupNotifier())
    diagnostics = registry.run_observers(
        HookPoint.RUNNER_STARTUP,
        RunnerStartupContext(
            hook_point=HookPoint.RUNNER_STARTUP,
            metadata={"ui_bus": ui_bus},
        ),
    )

    assert len(diagnostics) == 1
    assert (
        "phase=notify, error_type=PermissionError, ref=project_context_notice"
        in diagnostics[0].message
    )
    assert "ui-token=must-not-leak" not in diagnostics[0].message
