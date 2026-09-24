"""Real runtime/stdio transport with a deterministic LLM loop; no network or API key."""

import shlex
import sys
from dataclasses import fields
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.interaction_contracts import (
    ChoiceItem,
    ChooseOneRequest,
    ConfirmRequest,
    InputTextRequest,
    ReviewContext,
    ReviewGrantOption,
    ReviewRequest,
)
from reuleauxcoder.app.rpc.codec import decode, encode
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolArchiveReference,
    ToolDiagnostic,
    ToolDiff,
    ToolOutcome,
    ToolTruncation,
)
from reuleauxcoder.domain.approval import ApprovalSection, ApprovalSectionKind
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.process_manager import ProcessManager
from reuleauxcoder.domain.runtime.events import (
    AssistantContentDelta,
    DiagnosticsPublished,
    PlanUpdated,
    ProcessSessionChanged,
    ProgressReported,
    ReasoningDelta,
    RuntimeDiagnostic,
    RuntimeEvent,
    SubagentJobChanged,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutputDelta,
)
from reuleauxcoder.infrastructure.process.local import LocalProcessPort
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.infrastructure.rpc.peer import RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import StreamTransport
from reuleauxcoder.interfaces.entrypoint.rpc import create_server


class FakeLLM:
    model = "test-model"
    debug_trace = False

    def reconfigure(self, **values):
        self.__dict__.update(values)


config = Config(
    api_key="test", session_auto_save=True, history_file=str(Path.cwd() / "history")
)
loop = SimpleNamespace()
agent = Agent(FakeLLM(), tools=[ShellTool()], config=config, loop=loop)
agent.current_session_id = "test-session"
agent.process_manager = ProcessManager()
bus = UIEventBus()
registry = create_builtin_action_registry()
ctx = SimpleNamespace(
    agent=agent,
    config=config,
    ui_bus=bus,
    action_registry=registry,
    sessions_dir=Path.cwd() / "sessions",
    session_exit_time=None,
    skills_service=None,
)
peer = RpcPeer(StreamTransport(sys.stdin.buffer, sys.stdout.buffer))
server = create_server(ctx, peer, UIProfile("tui", "Test", frozenset(UICapability)))

DIFF = "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+新内容"
outcome = ToolOutcome(
    summary="Updated file",
    stdout="stdout retained",
    stderr="stderr retained",
    diff=ToolDiff("example.py", DIFF, additions=1, deletions=1),
    diagnostics=(ToolDiagnostic("example.py", 2, 1, "diagnostic retained", "warning"),),
    exit_code=0,
    duration_seconds=0.25,
    truncation=ToolTruncation(100, 10, 10, 1),
    model_content="bounded model content",
    archive_reference=ToolArchiveReference(
        "archive/full.txt", checksum_sha256="abc", size_bytes=100
    ),
    metadata=MappingProxyType(
        {"$type": "user-data", "nested": MappingProxyType({"value": "retained"})}
    ),
)
assert decode(encode(outcome)) == outcome


def emit(payload):
    bus.emit_runtime(
        RuntimeEvent(
            payload=payload,
            agent_id=agent.agent_id,
            session_generation=agent.session_generation,
        )
    )


def run():
    text = agent.messages[-1]["content"]
    goal = agent.goal_controller.state
    if goal is not None and goal.status == "active":
        agent.goal_controller.record_usage(
            goal.id,
            {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "estimated": False,
            },
        )
        if agent.goal_controller.state.tokens_used >= 60:
            agent.goal_controller.update(status="complete")
        emit(AssistantContentDelta("Goal checkpoint verified.\n"))
        return "Goal checkpoint verified."
    if text == "exercise":
        emit(ReasoningDelta("reasoning retained", display_mode="hidden"))
        emit(
            PlanUpdated(
                1,
                (
                    {
                        "step": "Implement",
                        "status": "in_progress",
                        "active_form": "Implementing",
                    },
                ),
            )
        )
        emit(ProgressReported(1, "working", "Building the new interface", "Verify"))
        emit(
            SubagentJobChanged(
                "job-1", "read", "Inspect", "completed", result="child result retained"
            )
        )
        emit(
            ProcessSessionChanged(
                "created",
                "process-1",
                "running",
                "pipe",
                "local",
                "echo test",
                ".",
                1,
                stdout="background stdout",
            )
        )
        emit(
            DiagnosticsPublished(
                "batch",
                "example.py",
                1,
                1,
                (RuntimeDiagnostic(1, 1, "live diagnostic"),),
            )
        )
        emit(ToolCallStarted("tool-1", "edit", {"path": "example.py"}))
        response = agent.ui_interactor.review(
            ReviewRequest(
                "Review edit",
                "Change example.py",
                sections=(
                    ApprovalSection("diff", "Changes", ApprovalSectionKind.DIFF, DIFF),
                ),
                context=ReviewContext("edit", "builtin", subjects=("example.py",)),
                grant_options=(
                    ReviewGrantOption("file", "This file", "Allow edits to example.py"),
                ),
            )
        )
        bus.info(
            f"Review result: {response.action}; {response.selected_id}; {response.reason}"
        )
        emit(ToolOutputDelta("tool-1", "streaming tool output"))
        emit(ToolCallFinished("tool-1", "edit", outcome))
        answer = agent.ui_interactor.confirm(
            ConfirmRequest("Confirm test", "Continue?")
        )
        bus.info(f"Confirmed: {answer.confirmed}")
        choice = agent.ui_interactor.choose_one(
            ChooseOneRequest(
                "Choose test", [ChoiceItem("a", "Alpha"), ChoiceItem("b", "Beta")]
            )
        )
        bus.info(f"Choice: {choice.selected_id}")
        secret = agent.ui_interactor.input_text(
            InputTextRequest("Secret test", "Enter test secret", secret=True)
        )
        bus.info(f"Secret received: {bool(secret.value)}")
    elif text == "wait":
        agent._stop_event.wait(10)
    elif text == "fail":
        raise RuntimeError("deliberate failure retained")
    emit(AssistantContentDelta("Completed **successfully**.\n"))
    emit(AssistantContentDelta("中文 👩🏽‍💻"))
    return "Completed **successfully**.\n中文 👩🏽‍💻"


loop.run = run


def contract_fixture():
    return encode(
        {
            "outcome": outcome,
            "parameters": {
                action.action_id: [item.name for item in fields(action.command_type)]
                for action in registry.iter_actions(server.commands.ui_profile)
            },
        }
    )


peer.methods["test.fixture"] = contract_fixture


def image_capability(enabled):
    agent.llm.support_modal = ("text", "image") if enabled else ("text",)
    return encode(server._publish_state())


peer.methods["test.image_capability"] = image_capability


def process_fixture():
    source = "import time; print('\\n'.join(f'output {i}' for i in range(200)), flush=True); time.sleep(30)"
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"
    ids = []
    for _ in range(2):
        handle = agent.process_manager.start(
            LocalProcessPort(), command, cwd=str(Path.cwd()), runtime_timeout=60, tty=False,
            owner_agent_id=agent.agent_id, owner_session_id=agent.current_session_id,
            session_generation=agent.session_generation, origin_turn_id=None,
        )
        agent.process_manager.publish(handle.session_id)
        ids.append(handle.session_id)
    return ids


peer.methods["test.processes"] = process_fixture
peer.start()
try:
    peer.closed.wait()
finally:
    server.shutdown()
    # This fixture owns the manager normally cleaned up by EntrypointRunner.
    report = agent.process_manager.shutdown()
    assert report.unknown == 0 and report.reap_timeouts == 0, report
    peer.close()
    peer.wait_closed(timeout=10)
    agent.unbind_session_persistence()
