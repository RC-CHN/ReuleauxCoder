"""Tests for ProjectContextHook — multi-file loading and injection."""

from pathlib import Path

import pytest

from reuleauxcoder.domain.hooks.builtin.project_context import (
    ProjectContextHook,
    DEFAULT_CONTEXT_FILES,
)
from reuleauxcoder.domain.hooks.types import (
    BeforeLLMRequestContext,
    HookPoint,
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
    assert hook._load_all_project_contexts() == []


def test_load_finds_agent_md(tmp_path: Path, monkeypatch) -> None:
    """AGENT.md exists → one entry."""
    (tmp_path / "AGENT.md").write_text("Project rules")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    parts = hook._load_all_project_contexts()
    assert parts == [("AGENT.md", "Project rules")]


def test_load_collects_all_existing_in_fixed_order(
    tmp_path: Path, monkeypatch,
) -> None:
    """Multiple candidates → all found, in DEFAULT_CONTEXT_FILES order."""
    (tmp_path / "CLAUDE.md").write_text("CLAUDE")
    (tmp_path / "AGENT.md").write_text("AGENT")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    parts = hook._load_all_project_contexts()
    # AGENT.md before CLAUDE.md
    assert parts == [("AGENT.md", "AGENT"), ("CLAUDE.md", "CLAUDE")]


def test_load_skips_empty_files(tmp_path: Path, monkeypatch) -> None:
    """Empty file → not included."""
    (tmp_path / "AGENT.md").write_text("")
    monkeypatch.chdir(tmp_path)
    hook = ProjectContextHook()
    assert hook._load_all_project_contexts() == []


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
    assert result.messages[1]["role"] == "system"
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
    assert msg["role"] == "system"
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
    hook._load_all_project_contexts = lambda: [("AGENT.md", "Keep it short")]

    messages = [
        {"role": "system", "content": "[system prompt]"},
        {"role": "user", "content": "fix the bug"},
    ]
    context = _make_context(messages)
    result = hook.run(context)

    assert len(result.messages) == 3
    assert result.messages[1]["role"] == "system"
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
