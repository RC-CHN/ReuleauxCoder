"""Remote relay bootstrap and peer chat binding helpers."""

from __future__ import annotations

import json
import io
from pathlib import Path
import uuid
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from reuleauxcoder.app.runtime.session_state import (
    apply_session_runtime_state,
    build_session_runtime_state,
    restore_config_runtime_defaults,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
)
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.protocol import ChatResponse
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.tools.backend import ExecutionContext
from reuleauxcoder.interfaces.shared.approval_preview import (
    build_preview_diff as _build_preview_diff,
)
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.registration import CLI_PROFILE
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


def init_remote_relay(runner, config: Config, ui_bus: UIEventBus) -> None:
    """Initialize remote relay server if enabled and host_mode."""
    try:
        relay = runner.dependencies.create_remote_relay_server(config)
    except Exception as exc:
        ui_bus.warning(
            f"Remote relay initialization failed: {exc}", kind=UIEventKind.REMOTE
        )
        return
    if relay is None:
        return
    try:
        relay.start()
        runner._relay_server = relay
    except Exception as exc:
        ui_bus.warning(
            f"Remote relay server failed to start: {exc}", kind=UIEventKind.REMOTE
        )
        return

    try:
        http_service = runner.dependencies.create_remote_http_service(
            config, relay, ui_bus
        )
    except Exception as exc:
        relay.stop()
        runner._relay_server = None
        ui_bus.warning(
            f"Remote relay HTTP service initialization failed: {exc}",
            kind=UIEventKind.REMOTE,
        )
        return

    if http_service is not None:
        try:
            http_service.start()
            runner._relay_http_service = http_service
        except Exception as exc:
            relay.stop()
            runner._relay_server = None
            runner._relay_http_service = None
            ui_bus.warning(
                f"Remote relay HTTP service failed to start: {exc}",
                kind=UIEventKind.REMOTE,
            )
            return

    ui_bus.success(
        "Remote relay server started.",
        kind=UIEventKind.REMOTE,
        bind=getattr(config.remote_exec, "relay_bind", None),
        base_url=runner._relay_http_service.base_url
        if runner._relay_http_service
        else None,
    )


def bind_remote_chat_handler(runner, agent: Agent) -> None:
    """Bind remote chat handlers for interactive peers."""
    if runner._relay_http_service is None or runner._relay_server is None:
        return

    relay_server: RelayServer = runner._relay_server
    config = getattr(agent, "runtime_config", None)
    ui_bus = getattr(agent.context, "_ui_bus", None)
    sessions_dir = (
        Path(config.session_dir)
        if config and getattr(config, "session_dir", None)
        else None
    )
    skills_service: SkillsService | None = getattr(agent, "skills_service", None)
    session_store = runner.dependencies.create_session_store(sessions_dir)
    peer_agents: dict[str, Agent] = {}
    peer_connection_markers: dict[str, str] = {}
    peer_presenters: dict[str, tuple[Console, CLIRenderer]] = {}

    def _connection_marker(peer_id: str) -> str:
        peer = relay_server.registry.get(peer_id)
        return (
            f"{getattr(peer, 'connected_at', 0):.6f}"
            if peer is not None
            else "0"
        )

    def _dispose_peer(peer_id: str) -> None:
        presenter = peer_presenters.pop(peer_id, None)
        if presenter is not None:
            presenter[1].close()
        peer_agent = peer_agents.pop(peer_id, None)
        manager = getattr(peer_agent, "_subagent_manager", None)
        if manager is not None:
            manager.shutdown(wait=True)
        if peer_agent is not None and peer_agent is not agent:
            peer_agent.lifecycle.runner_shutdown()
        peer_connection_markers.pop(peer_id, None)

    def _dispose_all_peers() -> None:
        for peer_id in tuple(peer_presenters):
            _dispose_peer(peer_id)

    runner._remote_chat_cleanup = _dispose_all_peers

    def _peer_fingerprint(peer_id: str) -> str:
        peer = relay_server.registry.get(peer_id)
        workspace_root = peer.workspace_root if peer is not None else "."
        machine_key = peer_id
        if peer is not None:
            host_info = (
                peer.meta.get("host_info_min") if isinstance(peer.meta, dict) else None
            )
            if isinstance(host_info, dict):
                machine_key = str(
                    host_info.get("hostname") or host_info.get("machine_id") or peer_id
                )
        return f"remote:{machine_key}:{workspace_root or '.'}"

    def _create_peer_agent(peer_id: str) -> Agent:
        marker = _connection_marker(peer_id)
        existing = peer_agents.get(peer_id)
        if existing is not None and peer_connection_markers.get(peer_id) == marker:
            return existing
        _dispose_peer(peer_id)
        if config is None:
            peer_agents[peer_id] = agent
            peer_connection_markers[peer_id] = marker
            console = Console(
                file=io.StringIO(),
                record=True,
                force_terminal=True,
                color_system="truecolor",
            )
            peer_presenters[peer_id] = (
                console,
                CLIRenderer(console_override=console),
            )
            return agent

        peer_llm = runner.dependencies.create_llm(config)
        peer_llm.ui_bus = ui_bus
        peer_backend = RemoteRelayToolBackend(relay_server=relay_server, ui_bus=ui_bus)
        peer_tools = runner.dependencies.load_tools(peer_backend)
        peer_agent = runner.dependencies.create_agent(peer_llm, peer_tools, config)
        peer_agent.runtime_config = config
        peer_agent.skills_service = skills_service
        peer_agent.skills_catalog = agent.skills_catalog
        runner._register_hooks(peer_agent, config)
        runner._wire_agent_tool_parent(peer_agent)

        peer = relay_server.registry.get(peer_id)
        workspace_root = peer.workspace_root if peer is not None else None
        runtime_cwd = workspace_root or (peer.cwd if peer is not None else None)
        if runtime_cwd:
            peer_agent.runtime_working_directory = runtime_cwd
        for tool in peer_agent.tools:
            backend = getattr(tool, "backend", None)
            if getattr(backend, "backend_id", None) != "remote_relay":
                continue
            context = getattr(backend, "context", None)
            if not isinstance(context, ExecutionContext):
                continue
            context.peer_id = peer_id
            if workspace_root:
                context.workspace_root = workspace_root

        fingerprint = _peer_fingerprint(peer_id)
        peer_agent.session_fingerprint = fingerprint

        def _cache_created_agent(reason: str) -> Agent:
            peer_agents[peer_id] = peer_agent
            peer_connection_markers[peer_id] = marker
            console = Console(
                file=io.StringIO(),
                record=True,
                force_terminal=True,
                color_system="truecolor",
            )
            peer_presenters[peer_id] = (
                console,
                CLIRenderer(console_override=console),
            )
            peer_agent.lifecycle.runner_started(
                metadata={"ui_bus": ui_bus, "peer_id": peer_id}
            )
            peer_agent.lifecycle.session_started(
                peer_agent.current_session_id,
                reason=reason,
                metadata={"peer_id": peer_id},
            )
            return peer_agent

        latest = session_store.get_latest(fingerprint=fingerprint)
        if latest:
            loaded = session_store.load(latest.id)
            if loaded is not None:
                apply_session_runtime_state(loaded, config, peer_agent)
                peer_agent.current_session_id = latest.id
                return _cache_created_agent("remote_restore")

        restore_config_runtime_defaults(config, peer_agent)
        peer_agent.current_session_id = session_store.generate_session_id()
        return _cache_created_agent("remote_new")

    def _save_peer_session(peer_agent: Agent, peer_id: str) -> None:
        if (
            config is None
            or not config.session_auto_save
            or not getattr(peer_agent, "messages", None)
        ):
            return
        sid = session_store.save(
            peer_agent.messages,
            getattr(peer_agent.llm, "model", config.model),
            getattr(peer_agent, "current_session_id", None),
            total_prompt_tokens=peer_agent.state.total_prompt_tokens,
            total_completion_tokens=peer_agent.state.total_completion_tokens,
            active_mode=getattr(peer_agent, "active_mode", None),
            runtime_state=build_session_runtime_state(config, peer_agent),
            fingerprint=_peer_fingerprint(peer_id),
        )
        peer_agent.current_session_id = sid
        peer_agent.lifecycle.session_saved(sid)

    def _chat(peer_id: str, prompt: str) -> ChatResponse:
        peer_agent = _create_peer_agent(peer_id)
        try:
            response = peer_agent.chat(prompt)
            _save_peer_session(peer_agent, peer_id)
            return ChatResponse(response=response)
        except Exception as exc:
            _save_peer_session(peer_agent, peer_id)
            return ChatResponse(response="", error=str(exc))

    def _stream_chat(peer_id: str, prompt: str, remote_session) -> None:
        peer_agent = _create_peer_agent(peer_id)
        ansi_console, renderer = peer_presenters[peer_id]

        if prompt.strip().startswith("/") and config is not None:
            command_bus = UIEventBus()
            command_bus.subscribe(renderer.on_ui_event, replay_history=False)
            command_result = handle_command(
                prompt.strip(),
                peer_agent,
                config,
                getattr(peer_agent, "current_session_id", None),
                command_bus,
                CLI_PROFILE,
                runner.dependencies.create_action_registry(),
                sessions_dir,
                skills_service,
            )
            if command_result["action"] != "chat":
                peer_agent.current_session_id = command_result["session_id"]

                rendered = ansi_console.export_text(clear=True, styles=True)
                if rendered:
                    remote_session.append_event(
                        "output", {"format": "terminal", "content": rendered}
                    )

                if command_result["action"] == "exit":
                    remote_session.append_event(
                        "output",
                        {
                            "format": "plain",
                            "content": "Exit command received. Use Ctrl+C to terminate remote peer.\n",
                        },
                    )
                _save_peer_session(peer_agent, peer_id)
                remote_session.append_event("chat_end", {"response": ""})
                return

        def _flush_output() -> None:
            rendered = ansi_console.export_text(clear=True, styles=True)
            if rendered:
                remote_session.append_event(
                    "output", {"format": "terminal", "content": rendered}
                )

        class _RemoteApprovalProvider(ApprovalProvider):
            def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
                request_id = str(uuid.uuid4())
                remote_session.register_interaction(request_id)
                diff_text = _build_preview_diff(request)
                approval_markdown = "\n\n".join(
                    part
                    for part in [
                        f"## Approval required: {request.tool_name}",
                        f"Tool `{request.tool_name}` from source `{request.tool_source}` requires approval.",
                        request.reason or "",
                        (
                            f"```json\n{json.dumps(request.tool_args, ensure_ascii=False, indent=2)}\n```"
                            if request.tool_args and diff_text is None
                            else ""
                        ),
                        f"```diff\n{diff_text}\n```" if diff_text else "",
                    ]
                    if part
                )
                approval_console = Console(
                    file=io.StringIO(),
                    record=True,
                    force_terminal=True,
                    color_system="truecolor",
                )
                approval_console.print(Markdown(approval_markdown))
                rendered_approval = approval_console.export_text(
                    clear=True, styles=True
                )
                payload = {
                    "request_id": request_id,
                    "kind": "review",
                    "rendered_frame": rendered_approval,
                    "input_constraints": {
                        "value_type": "boolean",
                        "approve_label": "Approve",
                        "reject_label": "Reject",
                    },
                }
                remote_session.append_event("interaction_request", payload)
                value, cancelled, reason = remote_session.wait_interaction(request_id)
                remote_session.append_event(
                    "interaction_resolved",
                    {
                        "request_id": request_id,
                        "cancelled": cancelled,
                        "reason": reason,
                    },
                )
                if value is True and not cancelled:
                    return ApprovalDecision.allow_once(reason)
                return ApprovalDecision.deny_once(reason)

        def _on_remote_stream(tool_name: str, chunk: Any) -> None:
            remote_session.append_event(
                "tool_call_stream",
                {
                    "tool_name": tool_name,
                    "format": "plain",
                    "stream": getattr(chunk, "chunk_type", "stdout"),
                    "content": getattr(chunk, "data", ""),
                },
            )

        def _on_agent_event(event: AgentEvent) -> None:
            if event.event_type == AgentEventType.ERROR:
                remote_session.append_event(
                    "error", {"message": event.error_message or "unknown error"}
                )
            renderer.on_event(event)
            _flush_output()

        for tool in peer_agent.tools:
            backend = getattr(tool, "backend", None)
            context = getattr(backend, "context", None)
            if isinstance(context, ExecutionContext):
                context.remote_stream_handler = _on_remote_stream

        previous_approval = peer_agent.approval_provider
        peer_agent.add_event_handler(_on_agent_event)
        peer_agent.approval_provider = _RemoteApprovalProvider()
        try:
            result = peer_agent.chat(prompt)
            _flush_output()
            _save_peer_session(peer_agent, peer_id)
            remote_session.append_event("chat_end", {"response": result})
        except Exception as exc:
            _flush_output()
            _save_peer_session(peer_agent, peer_id)
            remote_session.append_event("error", {"message": str(exc)})
        finally:
            peer_agent.approval_provider = previous_approval
            try:
                peer_agent._event_handlers.remove(_on_agent_event)
            except ValueError:
                pass

    runner._relay_http_service.set_chat_handler(_chat)
    runner._relay_http_service.set_stream_chat_handler(_stream_chat)
