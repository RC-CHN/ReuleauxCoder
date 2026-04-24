"""Tests for runner integration with remote execution."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib import request


_URLOPEN = request.build_opener(request.ProxyHandler({})).open

from reuleauxcoder.domain.config.models import (
    Config,
    ContextConfig,
    ModeConfig,
    RemoteExecConfig,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.interfaces.entrypoint.runner import (
    AppDependencies,
    AppOptions,
    AppRunner,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    method: str, url: str, payload: dict | None = None
) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


class FakeLLM:
    def __init__(self, model: str = "fake-model") -> None:
        self.model = model
        self.debug_trace = False
        self.api_key = "key"
        self.base_url = None
        self.temperature = 0.0
        self.max_tokens = 2048
        self.ui_bus = None

    def reconfigure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeContext:
    def __init__(self) -> None:
        self.max_tokens = 64000
        self._ui_bus = None

    def reconfigure(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens


class FakeAgent:
    def __init__(self, llm: FakeLLM, chat_behavior=None) -> None:
        self.llm = llm
        self.tools = []
        self.context = FakeContext()
        self.state = SimpleNamespace(
            messages=[],
            total_prompt_tokens=0,
            total_completion_tokens=0,
            current_round=0,
        )
        self.messages = self.state.messages
        self.available_modes = {
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        }
        self.active_mode = "coder"
        self.active_main_model_profile = None
        self.active_sub_model_profile = None
        self.session_fingerprint = "local"
        self.hook_registry = HookRegistry()
        self._event_handlers = []
        self.approval_provider = None
        self._chat_behavior = chat_behavior or (lambda _agent, prompt: f"ok:{prompt}")

    def register_hook(self, hook_point, hook) -> None:
        self.hook_registry.register(hook_point, hook)

    def add_event_handler(self, handler) -> None:
        self._event_handlers.append(handler)

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name

    def chat(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        response = self._chat_behavior(self, user_input)
        self.messages.append({"role": "assistant", "content": response})
        return response


def _build_runner_with_fake_agent(relay_bind: str, chat_behavior=None) -> AppRunner:
    config = Config(
        api_key="key",
        remote_exec=RemoteExecConfig(
            enabled=True, host_mode=True, relay_bind=relay_bind
        ),
        modes={
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        },
        active_mode="coder",
    )
    config.skills.enabled = False
    return AppRunner(
        options=AppOptions(),
        dependencies=AppDependencies(
            load_config=lambda _: config,
            create_llm=lambda cfg: FakeLLM(cfg.model),
            load_tools=lambda _backend: [],
            create_agent=lambda llm, _tools, _config: FakeAgent(
                llm, chat_behavior=chat_behavior
            ),
        ),
    )


def _register_peer(base_url: str, bootstrap_token: str, cwd: str) -> tuple[str, str]:
    _, register_body = _json_request(
        "POST",
        f"{base_url}/remote/register",
        {"bootstrap_token": bootstrap_token, "cwd": cwd, "workspace_root": cwd},
    )
    payload = register_body["payload"]
    return payload["peer_id"], payload["peer_token"]


def _collect_stream_events(
    base_url: str, peer_token: str, chat_id: str, timeout_sec: float = 3.0
) -> list[dict]:
    deadline = time.time() + timeout_sec
    cursor = 0
    events: list[dict] = []
    while time.time() < deadline:
        _, stream_body = _json_request(
            "POST",
            f"{base_url}/remote/chat/stream",
            {
                "peer_token": peer_token,
                "chat_id": chat_id,
                "cursor": cursor,
                "timeout_sec": 0.5,
            },
        )
        events.extend(stream_body["events"])
        cursor = stream_body["next_cursor"]
        if stream_body["done"]:
            return events
    raise AssertionError("timed out waiting for stream events")


class TestRunnerRemoteExec:
    def test_local_mode_no_relay(self, tmp_path: Path) -> None:
        """When remote_exec is disabled, runner starts normally with local backend."""
        config = Config(remote_exec=RemoteExecConfig(enabled=False))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is None
        assert ctx.agent is not None
        runner.cleanup(ctx.agent)

    def test_local_mode_smoke_startup_uses_local_backends(self, tmp_path: Path) -> None:
        """Smoke test: normal local startup should not initialize remote services."""
        config = Config(
            api_key="key",
            remote_exec=RemoteExecConfig(enabled=False, host_mode=False),
        )
        runner = AppRunner(
            options=AppOptions(server_mode=False),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is None
            assert runner._relay_http_service is None
            assert ctx.agent is not None
            assert len(ctx.agent.tools) > 0
            assert all(
                getattr(tool.backend, "backend_id", None) == "local"
                for tool in ctx.agent.tools
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_remote_enabled_host_mode_starts_relay(self, tmp_path: Path) -> None:
        config = Config(remote_exec=RemoteExecConfig(enabled=True, host_mode=True))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is not None
        assert isinstance(runner._relay_server, RelayServer)
        assert all(
            isinstance(tool.backend, RemoteRelayToolBackend) for tool in ctx.agent.tools
        )
        runner.cleanup(ctx.agent)
        assert runner._relay_server is None

    def test_remote_init_failure_does_not_crash(self, tmp_path: Path) -> None:
        def bad_relay_factory(_config: Config) -> RelayServer:
            raise RuntimeError("boom")

        config = Config(remote_exec=RemoteExecConfig(enabled=True, host_mode=True))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
                create_remote_relay_server=bad_relay_factory,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is None
        assert ctx.agent is not None
        runner.cleanup(ctx.agent)

    def test_cleanup_runs_relay_cleanup(self, tmp_path: Path) -> None:
        config = Config(remote_exec=RemoteExecConfig(enabled=True, host_mode=True))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is not None
        # no peers connected, cleanup should still complete without error
        runner.cleanup(ctx.agent)
        assert runner._relay_server is None

    def test_runner_preserves_context_config_on_agent(self, tmp_path: Path) -> None:
        config = Config(
            api_key="key",
            context=ContextConfig(
                snip_keep_recent_tools=9,
                snip_threshold_chars=3210,
                snip_min_lines=8,
                summarize_keep_recent_turns=6,
            ),
            remote_exec=RemoteExecConfig(enabled=False),
        )
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert (
            getattr(ctx.agent, "config", None) is None
            or getattr(ctx.agent, "config", None) == config
        )
        assert ctx.agent.max_context_tokens == config.max_context_tokens
        runner.cleanup(ctx.agent)

    def test_server_mode_smoke_bootstrap_endpoint(self, tmp_path: Path) -> None:
        relay_bind = "127.0.0.1:18765"
        bootstrap_secret = "runner-secret"
        config = Config(
            api_key="key",
            remote_exec=RemoteExecConfig(
                enabled=True,
                host_mode=True,
                relay_bind=relay_bind,
                bootstrap_access_secret=bootstrap_secret,
            ),
        )
        runner = AppRunner(
            options=AppOptions(server_mode=True),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            assert isinstance(runner._relay_http_service, RemoteRelayHTTPService)

            req = request.Request(
                f"http://{relay_bind}/remote/bootstrap.sh",
                headers={"X-RC-Bootstrap-Secret": bootstrap_secret},
                method="GET",
            )
            with _URLOPEN(req, timeout=5) as resp:
                body = resp.read().decode("utf-8")
                content_type = resp.headers.get_content_type()

            assert resp.status == 200
            assert content_type in {"text/x-shellscript", "text/plain"}
            assert "#!/bin/sh" in body
            assert "RC_HOST" in body
            assert "rcoder-peer" in body
            assert "/remote/artifacts/{os}/{arch}/rcoder-peer" in body
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_emits_startup_panel(self, tmp_path: Path) -> None:
        port = _free_port()
        runner = _build_runner_with_fake_agent(f"127.0.0.1:{port}")
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            peer_id, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            terminal_outputs = [
                event["payload"]["content"]
                for event in events
                if event["type"] == "output"
                and event["payload"].get("format") == "terminal"
            ]
            merged = "\n".join(terminal_outputs)
            assert "REMOTE PEER READY" in merged
            assert peer_id in merged
            assert "Session" in merged
            assert "Fingerprint" in merged
            assert "Mode" in merged
            assert "Model" in merged
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_keeps_peer_sessions_isolated(
        self, tmp_path: Path
    ) -> None:
        port = _free_port()

        def chat_behavior(agent: FakeAgent, prompt: str) -> str:
            time.sleep(0.15)
            return f"reply:{prompt}:{getattr(agent, 'current_session_id', '-')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            peer_a, token_a = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path / "peer-a"),
            )
            peer_b, token_b = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path / "peer-b"),
            )

            starts: dict[str, dict] = {}

            def start_chat(label: str, token: str) -> None:
                _, body = _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/chat/start",
                    {"peer_token": token, "prompt": label},
                )
                starts[label] = body

            t1 = threading.Thread(target=start_chat, args=("alpha", token_a))
            t2 = threading.Thread(target=start_chat, args=("beta", token_b))
            t1.start()
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)

            events_a = _collect_stream_events(
                runner._relay_http_service.base_url, token_a, starts["alpha"]["chat_id"]
            )
            events_b = _collect_stream_events(
                runner._relay_http_service.base_url, token_b, starts["beta"]["chat_id"]
            )

            outputs_a = "\n".join(
                event["payload"].get("content", "")
                for event in events_a
                if event["type"] == "output"
            )
            outputs_b = "\n".join(
                event["payload"].get("content", "")
                for event in events_b
                if event["type"] == "output"
            )
            end_a = [event for event in events_a if event["type"] == "chat_end"][-1]
            end_b = [event for event in events_b if event["type"] == "chat_end"][-1]

            assert peer_a in outputs_a
            assert peer_b in outputs_b
            assert peer_b not in outputs_a
            assert peer_a not in outputs_b
            assert end_a["payload"]["response"].startswith("reply:alpha:")
            assert end_b["payload"]["response"].startswith("reply:beta:")
            assert end_a["payload"]["response"] != end_b["payload"]["response"]
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_sets_remote_runtime_working_directory(
        self, tmp_path: Path
    ) -> None:
        port = _free_port()

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            return f"cwd:{getattr(agent, 'runtime_working_directory', '<missing>')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            end_event = [event for event in events if event["type"] == "chat_end"][-1]
            assert end_event["payload"]["response"] == f"cwd:{tmp_path}"
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_slash_command_renders_terminal_view(
        self, tmp_path: Path
    ) -> None:
        port = _free_port()
        runner = _build_runner_with_fake_agent(f"127.0.0.1:{port}")
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "/help"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            terminal_outputs = [
                event["payload"]["content"]
                for event in events
                if event["type"] == "output"
                and event["payload"].get("format") == "terminal"
            ]
            merged = "\n".join(terminal_outputs)
            assert "REMOTE PEER READY" in merged
            assert "Available commands" in merged or "/help" in merged
            assert not any(
                event["type"] == "output"
                and event["payload"].get("format") == "plain"
                and "Open view:" in event["payload"].get("content", "")
                for event in events
            )
        finally:
            runner.cleanup(ctx.agent)
