from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time
from types import SimpleNamespace

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.config.models import Config, ModelProfileConfig
from reuleauxcoder.extensions.subagent.manager import get_subagent_manager
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.control import RequestGuidanceTool
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort
from reuleauxcoder.extensions.subagent.worker_protocol import WorkerSpec
from reuleauxcoder.extensions.subagent.worker_runtime import (
    ParentToolBroker,
    WorkerIPCClient,
    run_isolated_worker,
)
from reuleauxcoder.domain.runtime import tool_outcome_from_dict


class _UnusedLLM:
    model = "unused"


def test_worker_broker_call_ids_are_unique_when_generated_concurrently() -> None:
    client = object.__new__(WorkerIPCClient)
    client.spec = SimpleNamespace(worker_generation=7)
    client._send_lock = threading.Lock()
    client._tool_call_sequence = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        call_ids = list(
            executor.map(lambda _index: client._next_tool_call_id(), range(64))
        )

    assert len(set(call_ids)) == 64
    assert set(call_ids) == {f"broker_7_{index}" for index in range(1, 65)}


def test_isolated_worker_hard_stop_reaches_terminal_without_model_request() -> None:
    cancel = threading.Event()
    cancel.set()
    broker_agent = Agent(llm=_UnusedLLM(), tools=[], agent_id="child-worker")
    broker = ParentToolBroker(
        broker_agent,
        cancellation_event=cancel,
        event_sink=None,
    )
    spec = WorkerSpec(
        job_id="sj_cancel",
        agent_id="child-worker",
        session_id="session",
        session_generation=0,
        worker_generation=1,
        cancellation_epoch=1,
        delegated_prompt="This request must never be dispatched.",
        llm_kwargs={
            "model": "never-dispatched",
            "api_key": "test",
            "base_url": "http://127.0.0.1:1/v1",
            "temperature": 0.0,
            "max_tokens": 16,
        },
        tools=(),
        max_context_tokens=1024,
        max_rounds=1,
        max_tool_calls=1,
        max_tokens=16,
    )

    result = run_isolated_worker(
        spec,
        broker,
        cancel_event=cancel,
        timeout_seconds=5,
        grace_seconds=0.2,
    )

    assert result.status in {"cancelled", "killed"}


def test_parent_tool_broker_replays_committed_call_without_reexecution() -> None:
    cancel = threading.Event()
    broker_agent = Agent(llm=_UnusedLLM(), tools=[], agent_id="broker-idempotent")
    calls = []

    class _Executor:
        def execute(self, call):
            calls.append(call)
            return "stable result"

    broker_agent._executor = _Executor()
    broker = ParentToolBroker(
        broker_agent,
        cancellation_event=cancel,
        event_sink=None,
    )

    first = broker.execute("call_same", "read_file", {"file_path": "a.py"})
    replay = broker.execute("call_same", "read_file", {"file_path": "a.py"})
    conflict = broker.execute("call_same", "read_file", {"file_path": "b.py"})

    assert first.content == replay.content == "stable result"
    assert len(calls) == 1
    assert conflict.success is False
    assert conflict.content is not None
    assert "reused with a different request" in conflict.content


def test_parent_tool_broker_archives_large_ipc_outcome_by_content_hash(
    tmp_path,
) -> None:
    cancel = threading.Event()
    broker_agent = Agent(llm=_UnusedLLM(), tools=[], agent_id="broker-archive")
    broker_agent.runtime_working_directory = str(tmp_path)
    broker = ParentToolBroker(
        broker_agent,
        cancellation_event=cancel,
        event_sink=None,
    )
    source = "line of full output\n" * 4_000
    payload = broker.ipc_tool_result(
        ToolOutcome(
            summary="large output",
            content=source,
            model_content="bounded model view",
        )
    )

    assert "outcome_ref" in payload
    reference = payload["outcome_ref"]
    archive = Path(reference["path"])
    assert archive.exists()
    assert archive.stat().st_size == reference["size_bytes"]
    archived_outcome = json.loads(archive.read_text(encoding="utf-8"))
    assert archived_outcome["content"] == source
    projected = tool_outcome_from_dict(payload["outcome"])
    assert projected.content is None
    assert projected.model_text == "bounded model view"


def test_parent_tool_broker_classifies_effectful_scoped_tools() -> None:
    cancel = threading.Event()
    readonly = SimpleNamespace(name="read_file", effect_class="read_only_internal")
    effectful = SimpleNamespace(name="shell", effect_class="process_execution")
    broker_agent = Agent(llm=_UnusedLLM(), tools=[], agent_id="broker-effects")
    broker_agent.tools = [readonly, effectful]
    broker = ParentToolBroker(
        broker_agent,
        cancellation_event=cancel,
        event_sink=None,
    )

    assert broker.is_effectful("read_file") is False
    assert broker.is_effectful("shell") is True


class _StreamingHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        chunks = [
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Conclusion: isolated worker complete"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


class _ToolCallingHandler(BaseHTTPRequestHandler):
    requests = []
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        with type(self).lock:
            type(self).requests.append(request)
            request_number = len(type(self).requests)
        if request_number == 1:
            chunks = [
                {
                    "id": "chatcmpl-tool",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_read_demo",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"file_path":"demo.txt"}',
                                        },
                                    },
                                    {
                                        "index": 1,
                                        "id": "call_read_other",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"file_path":"other.txt"}',
                                        },
                                    },
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-tool",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                    ],
                },
            ]
        else:
            chunks = [
                {
                    "id": "chatcmpl-tool",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "Conclusion: broker read complete"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-tool",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


class _HungRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        time.sleep(5)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


class _GuidanceHandler(BaseHTTPRequestHandler):
    requests = []
    lock = threading.Lock()
    request_seen = None
    release_response = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        with type(self).lock:
            type(self).requests.append(request)
            request_number = len(type(self).requests)
        if request_number == 1 and type(self).request_seen is not None:
            type(self).request_seen.set()
            type(self).release_response.wait(timeout=3)
        if request_number == 1:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_guidance",
                        "type": "function",
                        "function": {
                            "name": "request_guidance",
                            "arguments": json.dumps(
                                {"question": "Which API should I preserve?"}
                            ),
                        },
                    }
                ]
            }
            finish_reason = "tool_calls"
        else:
            delta = {"content": "Conclusion: resumed with parent guidance"}
            finish_reason = "stop"
        chunks = [
            {
                "id": "chatcmpl-guidance",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "chatcmpl-guidance",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            },
        ]
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


def test_isolated_worker_runs_model_loop_in_spawn_process(monkeypatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    cancel = threading.Event()
    broker_agent = Agent(llm=_UnusedLLM(), tools=[], agent_id="child-success")
    broker = ParentToolBroker(
        broker_agent,
        cancellation_event=cancel,
        event_sink=None,
    )
    spec = WorkerSpec(
        job_id="sj_success",
        agent_id="child-success",
        session_id="session",
        session_generation=0,
        worker_generation=1,
        cancellation_epoch=0,
        delegated_prompt="Return the result.",
        llm_kwargs={
            "model": "test",
            "api_key": "test",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "temperature": 0.0,
            "max_tokens": 64,
        },
        tools=(),
        max_context_tokens=4096,
        max_rounds=2,
        max_tool_calls=2,
        max_tokens=4096,
    )
    try:
        result = run_isolated_worker(
            spec,
            broker,
            cancel_event=cancel,
            timeout_seconds=5,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.status == "ok", result
    assert result.summary == "Conclusion: isolated worker complete"
    assert any(
        message.get("content") == "Conclusion: isolated worker complete"
        for message in result.messages
    )


def test_manager_runs_background_job_through_isolated_worker(
    monkeypatch, tmp_path
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = ModelProfileConfig(
        name="sub",
        model="test",
        api_key="test",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        max_tokens=64,
        max_context_tokens=4096,
    )
    config = Config(
        model_profiles={"sub": profile},
        active_model_profile="sub",
        active_main_model_profile="sub",
        active_sub_model_profile="sub",
    )
    root = Agent(llm=_UnusedLLM(), tools=[], config=config)
    root.current_session_id = "session"
    root.runtime_working_directory = str(tmp_path)
    manager = get_subagent_manager(root)
    try:
        job_id = manager.submit_background(
            parent_agent=root,
            task="Return a final result",
            mode="explore",
            timeout_seconds=5,
            auto_verify=False,
        )
        job = manager.wait_job(job_id, timeout=8)
    finally:
        manager.shutdown()
        server.shutdown()
        server.server_close()

    assert job is not None
    assert job.status == "completed"
    assert job.structured_result is not None
    assert job.structured_result.summary == "isolated worker complete"
    assert job.structured_result.confidence == "low"
    assert job.structured_result.unresolved


def test_worker_tool_call_round_trips_through_parent_broker(
    monkeypatch, tmp_path
) -> None:
    _ToolCallingHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ToolCallingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "demo.txt").write_text("broker evidence\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("second broker result\n", encoding="utf-8")
    profile = ModelProfileConfig(
        name="sub",
        model="test",
        api_key="test",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        max_tokens=128,
        max_context_tokens=4096,
    )
    config = Config(
        model_profiles={"sub": profile},
        active_model_profile="sub",
        active_main_model_profile="sub",
        active_sub_model_profile="sub",
    )
    read_tool = ReadFileTool()
    read_tool.backend.context.cwd = str(tmp_path)
    read_tool.backend.context.workspace_root = str(tmp_path)
    read_tool.backend.workspace = LocalWorkspacePort(tmp_path, cwd=tmp_path)
    root = Agent(llm=_UnusedLLM(), tools=[read_tool], config=config)
    root.current_session_id = "session"
    root.runtime_working_directory = str(tmp_path)
    manager = get_subagent_manager(root)
    try:
        job_id = manager.submit_background(
            parent_agent=root,
            task="Read demo.txt",
            mode="explore",
            timeout_seconds=8,
            auto_verify=False,
        )
        job = manager.wait_job(job_id, timeout=10)
    finally:
        manager.shutdown()
        server.shutdown()
        server.server_close()

    assert job is not None and job.status == "completed"
    assert job.structured_result is not None
    assert job.structured_result.summary == "broker read complete"
    assert job.structured_result.confidence == "low"
    assert any("tool read_file" in item for item in job.structured_result.evidence)
    assert len(_ToolCallingHandler.requests) == 2
    tool_messages = [
        message
        for message in _ToolCallingHandler.requests[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 2
    assert "broker evidence" in tool_messages[0]["content"]
    assert "second broker result" in tool_messages[1]["content"]
    assert job.tool_calls == 2


def test_manager_cancel_hard_kills_worker_stuck_in_provider_request(
    monkeypatch, tmp_path
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HungRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = ModelProfileConfig(
        name="sub",
        model="test",
        api_key="test",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        max_tokens=128,
        max_context_tokens=4096,
    )
    config = Config(
        model_profiles={"sub": profile},
        active_model_profile="sub",
        active_main_model_profile="sub",
        active_sub_model_profile="sub",
    )
    root = Agent(llm=_UnusedLLM(), tools=[], config=config)
    root.current_session_id = "session"
    root.runtime_working_directory = str(tmp_path)
    manager = get_subagent_manager(root)
    try:
        job_id = manager.submit_background(
            parent_agent=root,
            task="This request will hang",
            mode="explore",
            timeout_seconds=30,
            auto_verify=False,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get_job(job_id)
            if job is not None and job.status == "running":
                break
            time.sleep(0.01)
        time.sleep(0.2)
        started = time.monotonic()
        assert manager.cancel_job(job_id) is True
        job = manager.wait_job(job_id, timeout=3)
        elapsed = time.monotonic() - started
    finally:
        manager.shutdown(wait=False)
        server.shutdown()
        server.server_close()

    assert job is not None
    assert job.status in {"cancelled", "killed"}
    assert job.cancellation_id == f"cancel_{job_id}_1"
    assert job.usage_uncertain is True
    assert elapsed < 2.5


def test_guidance_parks_and_resumes_same_job_with_stable_replay_prefix(
    monkeypatch, tmp_path
) -> None:
    _GuidanceHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GuidanceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = ModelProfileConfig(
        name="sub",
        model="test",
        api_key="test",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        max_tokens=128,
        max_context_tokens=4096,
    )
    config = Config(
        model_profiles={"sub": profile},
        active_model_profile="sub",
        active_main_model_profile="sub",
        active_sub_model_profile="sub",
    )
    root = Agent(llm=_UnusedLLM(), tools=[RequestGuidanceTool()], config=config)
    root.current_session_id = "session"
    root.runtime_working_directory = str(tmp_path)
    manager = get_subagent_manager(root)
    try:
        job_id = manager.submit_background(
            parent_agent=root,
            task="Ask before choosing an API",
            mode="explore",
            timeout_seconds=8,
            auto_verify=False,
        )
        deadline = time.monotonic() + 8
        parked = None
        while time.monotonic() < deadline:
            parked = manager.get_job(job_id)
            if parked is not None and parked.status == "blocked":
                break
            time.sleep(0.02)
        assert parked is not None and parked.status == "blocked"
        assert parked.guidance_request_id
        assert parked.resume_reference
        checkpoint_payload = json.loads(
            Path(parked.resume_reference).read_text(encoding="utf-8")
        )
        assert checkpoint_payload["content_hash"]
        assert checkpoint_payload["metadata"]["checkpoint_id"].startswith("cp_")
        assert checkpoint_payload["metadata"]["tool_adjacency_complete"] is True
        assert checkpoint_payload["metadata"]["worker_generation"] == 1
        assert manager.wait_for_parent_activity(root.agent_id, timeout=0) is True

        assert manager.send_message(
            job_id,
            "Preserve the public v1 API and add compatibility internally.",
            sender_agent_id=root.agent_id,
        )
        completed = manager.wait_job(job_id, timeout=8)
    finally:
        manager.shutdown()
        server.shutdown()
        server.server_close()

    assert completed is not None and completed.id == job_id
    assert completed.status == "completed"
    assert completed.worker_generation == 2
    assert len(_GuidanceHandler.requests) == 2
    first_messages = _GuidanceHandler.requests[0]["messages"]
    resumed_messages = _GuidanceHandler.requests[1]["messages"]
    # execution_state is the intentionally volatile final overlay; persisted
    # transcript messages before it must remain an exact replay prefix.
    stable_first = [
        message
        for message in first_messages
        if not str(message.get("content") or "").startswith("<execution_state")
    ]
    stable_resumed = [
        message
        for message in resumed_messages
        if not str(message.get("content") or "").startswith("<execution_state")
    ]
    assert stable_resumed[: len(stable_first)] == stable_first
    assert any(message.get("role") == "tool" for message in resumed_messages)
    assert any(
            message.get("role") == "user"
        and "Preserve the public v1 API" in str(message.get("content") or "")
        for message in resumed_messages
    )


def test_directive_arriving_during_provider_round_survives_immediate_park(
    monkeypatch, tmp_path
) -> None:
    class Handler(_GuidanceHandler):
        requests = []
        request_seen = threading.Event()
        release_response = threading.Event()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = ModelProfileConfig(
        name="sub",
        model="test",
        api_key="test",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        max_tokens=128,
        max_context_tokens=4096,
    )
    config = Config(
        model_profiles={"sub": profile},
        active_model_profile="sub",
        active_main_model_profile="sub",
        active_sub_model_profile="sub",
    )
    root = Agent(llm=_UnusedLLM(), tools=[RequestGuidanceTool()], config=config)
    root.current_session_id = "session"
    root.runtime_working_directory = str(tmp_path)
    manager = get_subagent_manager(root)
    try:
        job_id = manager.submit_background(
            parent_agent=root,
            task="Ask before choosing an API",
            mode="explore",
            timeout_seconds=8,
            auto_verify=False,
        )
        assert Handler.request_seen.wait(timeout=3)
        assert manager.send_message(
            job_id,
            "Use the compatibility API sent during the active provider round.",
        )
        time.sleep(0.1)
        Handler.release_response.set()
        completed = manager.wait_job(job_id, timeout=8)
    finally:
        Handler.release_response.set()
        manager.shutdown()
        server.shutdown()
        server.server_close()

    assert completed is not None and completed.status == "completed"
    assert len(Handler.requests) == 2
    resumed_messages = Handler.requests[1]["messages"]
    matching = [
        message
        for message in resumed_messages
        if "Use the compatibility API sent during the active provider round."
        in str(message.get("content") or "")
    ]
    assert len(matching) == 1
    assert any(
            message.get("role") == "user"
        and "Re-read every relevant file or symbol" in str(message.get("content") or "")
        for message in resumed_messages
    )


def test_blocked_job_can_be_cancelled_without_reviving(monkeypatch) -> None:
    def parked(**_kwargs):
        return __import__(
            "reuleauxcoder.extensions.subagent.models", fromlist=["SubagentResult"]
        ).SubagentResult(
            status="blocked",
            summary="waiting",
            transcript_ref="checkpoint.json",
        )

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", parked
    )
    root = Agent(llm=_UnusedLLM(), tools=[])
    manager = get_subagent_manager(root)
    job_id = manager.submit_background(
        parent_agent=root, task="pause", mode="explore", auto_verify=False
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = manager.get_job(job_id)
        if job is not None and job.status == "blocked":
            break
        time.sleep(0.01)
    assert manager.cancel_job(job_id) is True
    cancelled = manager.get_job(job_id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert manager.send_message(job_id, "late guidance") is False
    manager.shutdown()
