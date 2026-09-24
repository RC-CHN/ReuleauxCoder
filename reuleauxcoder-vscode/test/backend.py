"""Actual Python runtime with deterministic turns; no provider/network requests."""

import sys
from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.interaction_contracts import InputTextRequest
from reuleauxcoder.app.rpc.codec import encode, decode
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.extensions.hook_adapter import HookExtensionAdapter
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import GuardDecision
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.runtime.events import AssistantContentDelta, RuntimeEvent
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.infrastructure.rpc.peer import RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import StreamTransport
from reuleauxcoder.interfaces.entrypoint.rpc import create_server


class FakeLLM:
    model = "test-model"
    debug_trace = False
    support_modal = ("text", "image")

    def reconfigure(self, **values):
        self.__dict__.update(values)


class TestExtensions(HookExtensionAdapter):
    def authorize_tool(self, context):
        return (GuardDecision.require_approval("Review test edit"),) if agent.messages[-1]["content"] == "edit" else ()


config = Config(api_key="test", session_auto_save=True)
root = Path.cwd()
tool = EditFileTool(backend=LocalToolBackend(ExecutionContext(cwd=str(root), workspace_root=str(root))))
loop = SimpleNamespace()
agent = Agent(FakeLLM(), tools=[tool], config=config, loop=loop, extension_runtime=TestExtensions(HookRegistry()))
agent.current_session_id = "test-vscode"
agent.runtime_working_directory = str(root)
bus = UIEventBus()
ctx = SimpleNamespace(agent=agent, config=config, ui_bus=bus, action_registry=create_builtin_action_registry(), sessions_dir=root / "sessions", session_exit_time=None, skills_service=None)
peer = RpcPeer(StreamTransport(sys.stdin.buffer, sys.stdout.buffer))
server = create_server(ctx, peer, UIProfile("vscode", "Test", frozenset(UICapability)))


def run():
    text = agent.messages[-1]["content"]
    if text in {"edit", "auto-edit"}:
        result = ToolExecutor(agent).execute(ToolCall(id="edit-test", name="edit_file", arguments={"file_path": "example.py", "old_string": "old", "new_string": "new"}))
    elif text == "question":
        result = str(agent.ui_interactor.input_text(InputTextRequest("Question", "Type a value", secret=True)).cancelled)
    elif text == "wait":
        agent._stop_event.wait(30)
        result = "Stopped."
    else:
        result = "Completed 中文 👩🏽‍💻"
    bus.emit_runtime(RuntimeEvent(payload=AssistantContentDelta(result), agent_id=agent.agent_id, session_generation=agent.session_generation))
    return result


loop.run = run
# ToolExecutor passes workspace.resolve() results; also expand Windows 8.3 aliases here.
peer.methods["test.is_dirty"] = lambda path: bool(server.editor_documents.guard(str(Path(path).resolve())))
peer.methods["test.state"] = lambda: encode(server._publish_state())
if "--legacy-core" in sys.argv:
    def legacy_initialize(**params):
        result = decode(server.initialize(**params))
        result.pop("editor_api_version")
        return encode(result)

    peer.methods["initialize"] = legacy_initialize
peer.start()
try:
    peer.closed.wait()
finally:
    server.shutdown()
