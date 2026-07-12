"""Tests for ToolExecutor, including CWD sync behaviour."""

from types import SimpleNamespace

from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.hooks.types import GuardDecision
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.process import ProcessChunk, ProcessResult
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
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

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
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
        preflight_validate=lambda **kwargs: None,
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
        preflight_validate=lambda **kwargs: None,
        schema=lambda: {"type": "function", "function": {"name": "structured"}},
    )
    agent = _AgentStub(tool)

    result = ToolExecutor(agent).execute(
        ToolCall(id="call_failed", name="structured", arguments={})
    )

    assert result == "plain failure without legacy prefix"
    assert agent.events[-1].tool_success is False
    assert agent.events[-1].tool_outcome is failure


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


def test_stale_approval_is_refreshed_and_external_change_reaches_model(tmp_path) -> None:
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
