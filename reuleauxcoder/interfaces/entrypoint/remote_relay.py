"""Remote relay bootstrap and peer chat binding helpers."""

from __future__ import annotations

import json
from pathlib import Path
import uuid
from typing import Any, Callable

from rich import box
from rich.console import Console
from rich.panel import Panel

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
from reuleauxcoder.domain.approval_preview import build_preview_diff as _build_preview_diff
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.registration import CLI_PROFILE
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies
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
    startup_announced: set[tuple[str, str, str]] = set()

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

    def _create_peer_agent(
        peer_id: str, remote_stream_handler: Callable[[str, Any], None] | None = None
    ) -> Agent:
        if config is None:
            return agent

        peer_llm = runner.dependencies.create_llm(config)
        peer_llm.ui_bus = ui_bus
        peer_backend = RemoteRelayToolBackend(relay_server=relay_server, ui_bus=ui_bus)
        peer_tools = runner.dependencies.load_tools(peer_backend)
        peer_agent = runner.dependencies.create_agent(peer_llm, peer_tools, config)
        setattr(peer_agent, "runtime_config", config)
        setattr(peer_agent, "skills_service", skills_service)
        setattr(peer_agent, "skills_catalog", getattr(agent, "skills_catalog", ""))
        runner._register_hooks(peer_agent, config)
        runner._wire_agent_tool_parent(peer_agent)

        peer = relay_server.registry.get(peer_id)
        workspace_root = peer.workspace_root if peer is not None else None
        runtime_cwd = workspace_root or (peer.cwd if peer is not None else None)
        if runtime_cwd:
            setattr(peer_agent, "runtime_working_directory", runtime_cwd)
        for tool in peer_agent.tools:
            backend = getattr(tool, "backend", None)
            if getattr(backend, "backend_id", None) != "remote_relay":
                continue
            context = getattr(backend, "context", None)
            if not isinstance(context, ExecutionContext):
                continue
            context.peer_id = peer_id
            context.remote_stream_handler = remote_stream_handler
            if workspace_root:
                context.workspace_root = workspace_root

        fingerprint = _peer_fingerprint(peer_id)
        setattr(peer_agent, "session_fingerprint", fingerprint)

        latest = session_store.get_latest(fingerprint=fingerprint)
        if latest:
            loaded = session_store.load(latest.id)
            if loaded is not None:
                apply_session_runtime_state(loaded, config, peer_agent)
                setattr(peer_agent, "current_session_id", latest.id)
                return peer_agent

        restore_config_runtime_defaults(config, peer_agent)
        setattr(peer_agent, "current_session_id", session_store.generate_session_id())
        return peer_agent

    def _save_peer_session(peer_agent: Agent, peer_id: str) -> None:
        if config is None or not getattr(peer_agent, "messages", None):
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
        setattr(peer_agent, "current_session_id", sid)

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

        session_id = getattr(peer_agent, "current_session_id", "-") or "-"
        peer_info = relay_server.registry.get(peer_id)
        connection_marker = (
            f"{getattr(peer_info, 'connected_at', 0):.6f}"
            if peer_info is not None
            else "0"
        )
        startup_key = (peer_id, str(session_id), connection_marker)
        if startup_key not in startup_announced:
            startup_console = Console(
                record=True, force_terminal=True, color_system="truecolor"
            )
            startup_console.print(
                Panel(
                    (
                        f"[bold]Peer[/bold]: {peer_id}\n"
                        f"[bold]Session[/bold]: {session_id}\n"
                        f"[bold]Fingerprint[/bold]: {_peer_fingerprint(peer_id)}\n"
                        f"[bold]Mode[/bold]: {getattr(peer_agent, 'active_mode', '-') or '-'}\n"
                        f"[bold]Model[/bold]: {getattr(getattr(peer_agent, 'llm', None), 'model', '-') or '-'}"
                    ),
                    title="REMOTE PEER READY",
                    border_style="green",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
            startup_rendered = startup_console.export_text(clear=True, styles=True)
            if startup_rendered:
                remote_session.append_event(
                    "output", {"format": "terminal", "content": startup_rendered}
                )
            startup_announced.add(startup_key)

        if prompt.strip().startswith("/") and config is not None:
            command_bus = UIEventBus()
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
                setattr(peer_agent, "current_session_id", command_result["session_id"])

                command_console = Console(
                    record=True, force_terminal=True, color_system="truecolor"
                )
                command_renderer = CLIRenderer(console_override=command_console)
                for event in getattr(command_bus, "_history", []):
                    command_renderer.on_ui_event(event)
                rendered = command_console.export_text(clear=True, styles=True)
                command_renderer.close()
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

        ansi_console = Console(
            record=True, force_terminal=True, color_system="truecolor"
        )
        renderer = CLIRenderer(console_override=ansi_console)

        def _flush_output() -> None:
            rendered = ansi_console.export_text(clear=True, styles=True)
            if rendered:
                remote_session.append_event(
                    "output", {"format": "terminal", "content": rendered}
                )

        class _RemoteApprovalProvider(ApprovalProvider):
            def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
                approval_id = str(uuid.uuid4())
                remote_session.register_approval(approval_id)
                diff_text = _build_preview_diff(request)
                sections: list[dict[str, Any]] = []
                if diff_text is not None:
                    title = (
                        "Proposed file diff"
                        if request.tool_name == "write_file"
                        else "Proposed edit diff"
                    )
                    sections.append(
                        {
                            "id": "diff",
                            "title": title,
                            "kind": "diff",
                            "content": diff_text,
                        }
                    )
                elif request.tool_args:
                    sections.append(
                        {
                            "id": "args",
                            "title": "Arguments",
                            "kind": "json",
                            "content": request.tool_args,
                        }
                    )
                payload = {
                    "approval_id": approval_id,
                    "tool_name": request.tool_name,
                    "tool_source": request.tool_source,
                    "reason": request.reason,
                    "sections": sections,
                    "format": "markdown",
                    "content": "\n\n".join(
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
                    ),
                }
                remote_session.append_event("approval_request", payload)
                decision, reason = remote_session.wait_approval(approval_id)
                remote_session.append_event(
                    "approval_resolved",
                    {
                        "approval_id": approval_id,
                        "decision": decision,
                        "reason": reason,
                    },
                )
                if decision == "allow_once":
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
            if event.event_type == AgentEventType.TOOL_CALL_START:
                remote_session.append_event(
                    "tool_call_start",
                    {"tool_name": event.tool_name, "tool_args": event.tool_args or {}},
                )
            elif event.event_type == AgentEventType.TOOL_CALL_END:
                remote_session.append_event(
                    "tool_call_end",
                    {
                        "tool_name": event.tool_name,
                        "tool_success": event.tool_success,
                        "tool_result": event.tool_result or "",
                    },
                )
            elif event.event_type == AgentEventType.ERROR:
                remote_session.append_event(
                    "error", {"message": event.error_message or "unknown error"}
                )
            renderer.on_event(event)
            _flush_output()

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
            renderer.close()

    runner._relay_http_service.set_chat_handler(_chat)
    runner._relay_http_service.set_stream_chat_handler(_stream_chat)
