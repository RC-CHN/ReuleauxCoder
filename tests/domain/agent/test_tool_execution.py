"""Tests for ToolExecutor, including CWD sync behaviour."""

import concurrent.futures
import threading
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.approval import ApprovalDecision, ApprovalSectionKind
from reuleauxcoder.domain.hooks.types import GuardDecision
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.process import ProcessChunk, ProcessResult
from reuleauxcoder.domain.workspace import WorkspaceError, WorkspaceErrorCode
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool


class _ShellToolStub:
    """A minimal stub mimicking ShellTool, with _cwd tracking."""

    name = "shell"
    description = "Run a shell command"
    parameters = {}

    def __init__(self) -> None:
        self._cwd: str | None = None

    def execute(self, command: str, timeout: int = 120) -> str:
        return "(no output)"

    def preflight_validate(self, arguments, *, schema_only=False):  # noqa: ARG002
        return None

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name}}


class _AgentStub:
    """Minimal agent stub for ToolExecutor."""

    def __init__(self, tool) -> None:
        self._tool = tool
        self.agent_id = "agent-test"
        self.session_generation = 0
        self.current_session_id = "session-test"
        self._current_turn_id = "turn-test"
        self.active_mode = "coder"
        self.state = SimpleNamespace(current_round=0)
        self.approval_provider = None
        self.hook_registry = SimpleNamespace(
            run_guards=lambda point, ctx: [],
            run_transforms=lambda point, ctx: ctx,
            run_observers=lambda point, ctx: None,
        )
        self.extension_runtime = SimpleNamespace(
            authorize_tool=lambda ctx: tuple(self.hook_registry.run_guards(None, ctx)),
            contribute_tool_context=lambda ctx: self.hook_registry.run_transforms(
                None, ctx
            ),
            process_tool_outcome=lambda ctx: self.hook_registry.run_transforms(
                None, ctx
            ),
            observe=lambda point, ctx: self.hook_registry.run_observers(point, ctx),
        )
        self.events = []

    def get_tool(self, name: str):  # noqa: ARG002
        return self._tool

    def is_tool_allowed_in_mode(self, name: str) -> bool:  # noqa: ARG002
        return True

    def is_tool_in_scope(self, name: str) -> bool:  # noqa: ARG002
        return True

    def suggest_modes_for_tool(self, name: str) -> list[str]:  # noqa: ARG002
        return []

    def get_active_mode_config(self):
        return SimpleNamespace(prompt_append="")

    def _emit_event(self, event) -> None:
        self.events.append(event)


def test_shell_cwd_syncs_to_runtime_working_directory() -> None:
    """After shell tool executes, ToolExecutor syncs _cwd → agent.runtime_working_directory."""
    tool = _ShellToolStub()
    tool._cwd = "/tmp/cool-dir"

    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_1", name="shell", arguments={"command": "echo hi"})
    executor.execute(tc)

    assert getattr(agent, "runtime_working_directory", None) == "/tmp/cool-dir"
    assert agent.events[-1].correlation_id == "call_1"
    assert agent.events[-1].tool_outcome.model_text == "(no output)"


def test_non_shell_tool_does_not_set_runtime_working_directory() -> None:
    """A tool without _cwd should not touch runtime_working_directory."""
    tool = SimpleNamespace(
        name="read_file",
        execute=lambda **kwargs: "file content",
        preflight_validate=lambda arguments, **kwargs: None,
        schema=lambda: {"type": "function", "function": {"name": "read_file"}},
    )
    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_2", name="read_file", arguments={"file_path": "/tmp/x"})
    executor.execute(tc)

    assert not hasattr(agent, "runtime_working_directory")


def test_shell_tool_without_cwd_does_not_set_runtime_working_directory() -> None:
    """ShellTool with _cwd=None should not set runtime_working_directory."""
    tool = _ShellToolStub()
    tool._cwd = None  # explicitly None

    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)

    tc = ToolCall(id="call_3", name="shell", arguments={"command": "echo hi"})
    executor.execute(tc)

    assert not hasattr(agent, "runtime_working_directory")


def test_structured_failure_status_is_preserved_without_string_guessing() -> None:
    failure = ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content="plain failure without legacy prefix",
        error_kind=ToolErrorKind.EXECUTION,
    )
    tool = SimpleNamespace(
        name="structured",
        execute=lambda **kwargs: failure,
        preflight_validate=lambda arguments, **kwargs: None,
        schema=lambda: {"type": "function", "function": {"name": "structured"}},
    )
    agent = _AgentStub(tool)

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_failed", name="structured", arguments={})
    )

    assert result == "plain failure without legacy prefix"
    assert agent.events[-1].tool_success is False
    assert agent.events[-1].tool_outcome is failure


def test_missing_required_arguments_are_rejected_before_execution(tmp_path) -> None:
    target = tmp_path / "should-not-exist.txt"
    tool = WriteFileTool()
    agent = _AgentStub(tool)

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="missing-content",
            name="write_file",
            arguments={"file_path": str(target)},
        )
    )

    assert "Tool call rejected [invalid_arguments]" in result
    assert "content" in result
    assert not target.exists()
    outcome = agent.events[-1].tool_outcome
    assert outcome.error_kind is ToolErrorKind.INVALID_ARGUMENTS
    assert outcome.metadata["missing_arguments"] == ("content",)
    assert agent.events[-1].tool_result == outcome.display_text
    assert result == outcome.model_text
    assert outcome.display_text != outcome.model_text


def test_schema_rejection_happens_before_authorization() -> None:
    tool = WriteFileTool()
    agent = _AgentStub(tool)
    authorization_contexts = []
    agent.extension_runtime.authorize_tool = lambda ctx: (
        authorization_contexts.append(ctx) or ()
    )

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="missing-content-before-auth",
            name="write_file",
            arguments={"file_path": "demo.txt"},
        )
    )

    assert "Tool call rejected [invalid_arguments]" in result
    assert authorization_contexts == []


def test_authorization_receives_canonical_approval_subjects(tmp_path) -> None:
    backend = LocalToolBackend(
        ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
    )
    tool = WriteFileTool(backend)
    agent = _AgentStub(tool)
    authorization_contexts = []

    def authorize(context):
        authorization_contexts.append(context)
        return (GuardDecision.deny("stop after observing context"),)

    agent.extension_runtime.authorize_tool = authorize

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="subject-before-auth",
            name="write_file",
            arguments={"file_path": "src/demo.py", "content": "value\n"},
        )
    )

    assert result == "stop after observing context"
    assert authorization_contexts[0].metadata["approval_subjects"] == (
        "src/demo.py",
    )
    assert not (tmp_path / "src").exists()


def test_unknown_tool_returns_active_name_suggestion() -> None:
    agent = _AgentStub(ReadFileTool())
    agent.strict_tool_scope = True
    agent.get_tool = lambda name: None
    agent.get_active_tools = lambda: [ReadFileTool()]

    result = ToolExecutor(agent).execute(
        ToolCall(id="unknown-read", name="reed_file", arguments={})
    )

    assert "Tool call rejected [unknown_tool]" in result
    assert "'read_file'" in result
    outcome = agent.events[-1].tool_outcome
    assert outcome.error_kind is ToolErrorKind.NOT_FOUND
    assert outcome.metadata["suggested_tools"] == ("read_file",)


def test_missing_scoped_tool_does_not_fall_back_to_global_builtin_catalog() -> None:
    agent = _AgentStub(None)
    agent.strict_tool_scope = False
    agent.get_active_tools = lambda: []

    result = ToolExecutor(agent).execute(
        ToolCall(id="missing-read", name="read_file", arguments={"file_path": "x"})
    )

    assert "Tool call rejected [unknown_tool]" in result
    assert agent.events[-1].tool_outcome.error_kind is ToolErrorKind.NOT_FOUND


def test_guard_warning_is_emitted_as_structured_diagnostic() -> None:
    tool = _ShellToolStub()
    agent = _AgentStub(tool)
    agent.hook_registry.run_guards = lambda point, ctx: [
        GuardDecision.warn("command deserves review")
    ]
    executor = ToolExecutor(agent)

    executor.execute(
        ToolCall(id="call_warn", name="shell", arguments={"command": "echo hi"})
    )

    diagnostic = agent.events[0]
    assert diagnostic.data["code"] == "tool.guard_warning"
    assert diagnostic.data["message"] == "command deserves review"
    assert diagnostic.data["details"]["tool_call_id"] == "call_warn"


class _ReviewingProvider:
    def __init__(self, *, mutate=None, reviewed: bool = True) -> None:
        self.mutate = mutate
        self.reviewed = reviewed
        self.requests = []

    def request_approval(self, request):  # noqa: ARG002
        self.requests.append(request)
        if self.mutate is not None:
            mutate, self.mutate = self.mutate, None
            mutate()
        return ApprovalDecision.allow_once("approved", reviewed=self.reviewed)


class _DenyingProvider:
    def __init__(self) -> None:
        self.requests = []

    def request_approval(self, request):
        self.requests.append(request)
        return ApprovalDecision.deny_once("external path rejected")


@pytest.mark.parametrize(
    ("tool_type", "tool_name"),
    [
        (ReadFileTool, "read_file"),
        (ListFileTool, "list_file"),
        (GlobTool, "glob"),
        (GrepTool, "grep"),
    ],
)
def test_external_readonly_workspace_tools_are_allowed_by_default(
    tmp_path, tool_type, tool_name
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "sample.txt"
    target.write_text("needle outside\n")
    backend = LocalToolBackend(
        ExecutionContext(cwd=str(root), workspace_root=str(root))
    )
    tool = tool_type(backend=backend)
    agent = _AgentStub(tool)
    if tool_name == "read_file":
        arguments = {"file_path": str(target)}
        granted_target = target
        expected = "needle outside"
    elif tool_name == "glob":
        arguments = {"pattern": "*.txt", "path": str(external)}
        granted_target = external
        expected = str(target)
    elif tool_name == "grep":
        arguments = {"pattern": "needle", "path": str(external)}
        granted_target = external
        expected = "needle outside"
    else:
        arguments = {"path": str(external)}
        granted_target = external
        expected = "sample.txt"

    result = ToolExecutor(agent).execute(
        ToolCall(id=f"external-{tool_name}", name=tool_name, arguments=arguments)
    )

    assert expected in result
    assert agent.events[-1].tool_success is True
    assert backend.workspace is not None
    with pytest.raises(WorkspaceError) as revoked:
        backend.workspace.stat_entry(granted_target)
    assert revoked.value.code is WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE


def test_external_readonly_tool_still_honors_explicit_approval_policy(
    tmp_path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "external.txt"
    target.write_text("not exposed\n")
    tool = ReadFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(root), workspace_root=str(root))
        )
    )
    agent = _AgentStub(tool)
    agent.hook_registry.run_guards = lambda point, ctx: [
        GuardDecision.require_approval("explicit review")
    ]
    provider = _DenyingProvider()
    agent.approval_provider = provider

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="external-read-review",
            name="read_file",
            arguments={"file_path": str(target)},
        )
    )

    assert result == "external path rejected"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.metadata["force_human_review"] is False
    assert request.metadata["external_workspace_path"] == str(target.resolve())
    assert [section.title for section in request.preview.sections] == [
        "Outside workspace",
        "Target",
    ]


def test_external_write_forces_exact_path_review_and_revokes_access(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "external.txt"
    tool = WriteFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(root), workspace_root=str(root))
        )
    )
    agent = _AgentStub(tool)
    provider = _ReviewingProvider()
    agent.approval_provider = provider

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="external-write",
            name="write_file",
            arguments={"file_path": str(target), "content": "review me\n"},
        )
    )

    assert result.startswith(f"Wrote 1 lines to {target}")
    assert target.read_text() == "review me\n"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.metadata["force_human_review"] is True
    assert request.metadata["external_workspace_path"] == str(target.resolve())
    assert request.metadata["workspace_root"] == str(root.resolve())
    assert request.subjects == (target.resolve().as_posix(),)
    assert [candidate.id for candidate in request.grant_candidates] == ["exact"]
    assert all(not candidate.broad for candidate in request.grant_candidates)
    assert [section.kind for section in request.preview.sections] == [
        ApprovalSectionKind.TEXT,
        ApprovalSectionKind.DIFF,
    ]
    assert request.preview.sections[0].title == "Outside workspace"
    assert "this file only" in request.preview.sections[0].content
    with pytest.raises(WorkspaceError) as revoked:
        tool.backend.workspace.read_text(target)
    assert revoked.value.code is WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE


def test_internal_write_review_offers_exact_and_directory_session_grants(
    tmp_path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    tool = WriteFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(root), workspace_root=str(root))
        )
    )
    agent = _AgentStub(tool)
    agent.hook_registry.run_guards = lambda point, ctx: [
        GuardDecision.require_approval("review write")
    ]
    provider = _ReviewingProvider()
    agent.approval_provider = provider

    ToolExecutor(agent).execute(
        ToolCall(
            id="internal-grants",
            name="write_file",
            arguments={"file_path": "src/demo.py", "content": "value\n"},
        )
    )

    request = provider.requests[0]
    assert request.tool_source == "builtin"
    assert request.effect_class == "filesystem_mutation"
    assert request.subjects == ("src/demo.py",)
    assert request.scope_key is not None
    assert [candidate.id for candidate in request.grant_candidates] == [
        "exact",
        "directory",
    ]
    exact, directory = request.grant_candidates
    assert [rule.pattern for rule in exact.proposed_rules] == ["src/demo.py"]
    assert [rule.pattern for rule in directory.proposed_rules] == ["src/**"]
    assert all(
        rule.tool_name == "write_file" and rule.effect_class is None
        for candidate in request.grant_candidates
        for rule in candidate.proposed_rules
    )
    assert all(
        rule.scope_key == request.scope_key
        for candidate in request.grant_candidates
        for rule in candidate.proposed_rules
    )
    assert directory.broad is True


def test_shell_review_offers_only_an_exact_signature_grant(tmp_path) -> None:
    tool = ShellTool(
        LocalToolBackend(
            ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
        )
    )
    agent = _AgentStub(tool)
    agent.hook_registry.run_guards = lambda point, ctx: [
        GuardDecision.require_approval("review command")
    ]
    provider = _ReviewingProvider()
    agent.approval_provider = provider

    ToolExecutor(agent).execute(
        ToolCall(
            id="shell-grant",
            name="shell",
            arguments={"command": "echo approval"},
        )
    )

    request = provider.requests[0]
    assert len(request.subjects) == 1
    assert [candidate.id for candidate in request.grant_candidates] == ["exact"]
    candidate = request.grant_candidates[0]
    assert candidate.label == "This command signature"
    assert candidate.broad is False
    assert candidate.proposed_rules[0].pattern == request.subjects[0]


def test_external_edit_is_preflighted_after_review_under_exact_path_grant(
    tmp_path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "external.txt"
    target.write_text("old value\n")
    tool = EditFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(root), workspace_root=str(root))
        )
    )
    agent = _AgentStub(tool)
    provider = _ReviewingProvider()
    agent.approval_provider = provider

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="external-edit",
            name="edit_file",
            arguments={
                "file_path": str(target),
                "old_string": "old",
                "new_string": "new",
            },
        )
    )

    assert result.startswith(f"Edited {target}")
    assert target.read_text() == "new value\n"
    assert len(provider.requests) == 1
    assert "-old value" in provider.requests[0].preview.sections[1].content
    assert "+new value" in provider.requests[0].preview.sections[1].content


def test_denied_external_write_does_not_create_target(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "external.txt"
    tool = WriteFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(root), workspace_root=str(root))
        )
    )
    agent = _AgentStub(tool)
    provider = _DenyingProvider()
    agent.approval_provider = provider

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="external-deny",
            name="write_file",
            arguments={"file_path": str(target), "content": "blocked\n"},
        )
    )

    assert result == "external path rejected"
    assert not target.exists()
    assert len(provider.requests) == 1


def test_tool_outcome_marks_identical_human_reviewed_diff(tmp_path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("old\n")
    tool = EditFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
        )
    )
    agent = _AgentStub(tool)
    agent.hook_registry.run_guards = lambda point, ctx: [
        GuardDecision.require_approval()
    ]
    agent.approval_provider = _ReviewingProvider()

    ToolExecutor(agent).execute(
        ToolCall(
            id="reviewed",
            name="edit_file",
            arguments={
                "file_path": str(target),
                "old_string": "old",
                "new_string": "new",
            },
        )
    )

    assert agent.events[-1].tool_outcome.metadata["diff_reviewed"] is True


def test_stale_approval_is_refreshed_and_external_change_reaches_model(
    tmp_path,
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("old\n")
    tool = EditFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
        )
    )
    agent = _AgentStub(tool)
    agent.hook_registry.run_guards = lambda point, ctx: [
        GuardDecision.require_approval()
    ]
    provider = _ReviewingProvider(
        mutate=lambda: target.write_text("old\nexternal change\n")
    )
    agent.approval_provider = provider

    ToolExecutor(agent).execute(
        ToolCall(
            id="stale-review",
            name="edit_file",
            arguments={
                "file_path": str(target),
                "old_string": "old",
                "new_string": "new",
            },
        )
    )

    outcome = agent.events[-1].tool_outcome
    assert len(provider.requests) == 2
    assert provider.requests[1].metadata["workspace_changed_during_approval"] is True
    assert outcome.metadata["diff_reviewed"] is True
    assert outcome.metadata["workspace_changed_during_approval"] is True
    assert "+external change" in outcome.model_text


def test_write_outcome_suppresses_identical_human_reviewed_diff(tmp_path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("old\n")
    tool = WriteFileTool(
        backend=LocalToolBackend(
            ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path))
        )
    )
    agent = _AgentStub(tool)
    agent.hook_registry.run_guards = lambda point, ctx: [
        GuardDecision.require_approval()
    ]
    agent.approval_provider = _ReviewingProvider()

    ToolExecutor(agent).execute(
        ToolCall(
            id="reviewed-write",
            name="write_file",
            arguments={"file_path": str(target), "content": "new\n"},
        )
    )

    assert agent.events[-1].tool_outcome.metadata["diff_reviewed"] is True


def test_shell_process_chunks_are_published_without_changing_full_outcome(
    tmp_path,
) -> None:
    class StreamingProcess:
        def run(self, command, *, stream_handler, **kwargs):  # noqa: ARG002
            stream_handler(ProcessChunk("stdout", "first\n"))
            stream_handler(ProcessChunk("stdout", "last\n"))
            return ProcessResult(stdout="first\nlast\n", exit_code=0)

    tool = ShellTool(
        LocalToolBackend(
            ExecutionContext(cwd=str(tmp_path), workspace_root=str(tmp_path)),
            process=StreamingProcess(),
        )
    )
    agent = _AgentStub(tool)

    ToolExecutor(agent).execute(
        ToolCall(id="streaming", name="shell", arguments={"command": "demo"})
    )

    chunks = [
        event.data["text"]
        for event in agent.events
        if event.event_type.value == "tool_output_delta"
    ]
    assert chunks == ["first\n", "last\n"]
    assert agent.events[-1].tool_outcome.model_text == "first\nlast"


def test_parallel_tool_stream_handlers_do_not_cross_or_leak(tmp_path) -> None:
    barrier = threading.Barrier(2)
    first_finished = threading.Event()
    outer_chunks = []

    class ParallelStreamingProcess:
        def run(self, command, *, stream_handler, **kwargs):  # noqa: ARG002
            barrier.wait()
            if command == "second":
                assert first_finished.wait(2)
            stream_handler(ProcessChunk("stdout", command + "\n"))
            if command == "first":
                first_finished.set()
            return ProcessResult(stdout=command + "\n", exit_code=0)

    def outer_handler(tool_name, chunk) -> None:
        outer_chunks.append((tool_name, chunk.data))

    context = ExecutionContext(
        cwd=str(tmp_path),
        workspace_root=str(tmp_path),
        remote_stream_handler=outer_handler,
    )
    tool = ShellTool(
        LocalToolBackend(context, process=ParallelStreamingProcess())
    )
    agent = _AgentStub(tool)
    executor = ToolExecutor(agent)
    calls = (
        ToolCall(id="call-first", name="shell", arguments={"command": "first"}),
        ToolCall(id="call-second", name="shell", arguments={"command": "second"}),
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(executor.execute, calls))

    deltas = [
        (event.correlation_id, event.data["text"])
        for event in agent.events
        if event.event_type.value == "tool_output_delta"
    ]
    assert sorted(results) == ["first", "second"]
    assert sorted(deltas) == [
        ("call-first", "first\n"),
        ("call-second", "second\n"),
    ]
    assert sorted(outer_chunks) == [
        ("shell", "first\n"),
        ("shell", "second\n"),
    ]
    assert context.remote_stream_handler is outer_handler
