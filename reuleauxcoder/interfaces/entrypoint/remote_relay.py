"""Remote relay bootstrap and peer chat binding helpers."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from reuleauxcoder.app.runtime.session_state import (
    bind_session_persistence,
    build_session_persistence_kwargs,
    apply_session_runtime_state,
    build_session_runtime_state,
    restore_config_runtime_defaults,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.protocol import ChatResponse
from reuleauxcoder.extensions.remote_exec.protocol import TerminalCapabilities
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.tools.backend import ExecutionContext
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.registration import REMOTE_CLI_PROFILE
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.interaction_presenter import (
    interaction_constraints,
    render_interaction_request,
)
from reuleauxcoder.interfaces.approval import make_approval_handler
from reuleauxcoder.app.runtime.interactions import InteractionCoordinator
from reuleauxcoder.interfaces.interactions import (
    ChooseOneRequest,
    ChooseOneResponse,
    ConfirmRequest,
    ConfirmResponse,
    InputTextRequest,
    InputTextResponse,
    ReviewRequest,
    ReviewResponse,
)
from reuleauxcoder.interfaces.events import AgentEventBridge, UIEventBus, UIEventKind
from reuleauxcoder.interfaces.entrypoint.session_lifecycle import (
    observe_session_callback,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionRestoreError
from reuleauxcoder.presentation import PresentationPolicy


@dataclass(slots=True)
class PeerPresentation:
    """Long-lived presentation path owned by one connected peer."""

    console: Console
    renderer: CLIRenderer
    ui_bus: UIEventBus
    agent_bridge: AgentEventBridge


def create_remote_console(terminal: TerminalCapabilities) -> Console:
    """Create the Host renderer sink from negotiated peer terminal facts."""
    color_system = {
        "none": None,
        "standard": "standard",
        "256": "256",
        "truecolor": "truecolor",
    }[terminal.color_level]
    return Console(
        file=io.StringIO(),
        record=True,
        width=terminal.width,
        # This is a record-only Host sink, never the Host's real terminal.
        # ANSI is added explicitly by export_remote_console when supported.
        force_terminal=False,
        force_jupyter=False,
        color_system=color_system,
        emoji=terminal.unicode,
    )


def export_remote_console(console: Console, *, clear: bool = True) -> str:
    """Export ANSI only when the peer declared color support."""
    return console.export_text(
        clear=clear,
        styles=console.color_system is not None,
    )


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


def bind_remote_chat_handler(
    runner, agent: Agent, action_registry: ActionRegistry
) -> None:
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
    peer_presenters: dict[str, PeerPresentation] = {}

    def _terminal_for_peer(peer_id: str) -> TerminalCapabilities:
        peer = relay_server.registry.get(peer_id)
        return TerminalCapabilities.from_dict(
            peer.meta.get("terminal")
            if peer is not None and isinstance(peer.meta, dict)
            else None
        )

    def _renderer_for(console: Console, peer_id: str) -> CLIRenderer:
        policy = (
            PresentationPolicy.from_ui_config(config.ui)
            if config is not None
            else PresentationPolicy()
        )
        return CLIRenderer(
            console_override=console,
            policy=policy,
            terminal_width_provider=lambda: _terminal_for_peer(peer_id).width,
        )

    def _console_for_peer(peer_id: str) -> Console:
        return create_remote_console(_terminal_for_peer(peer_id))

    def _presentation_for_peer(
        peer_id: str, presentation_agent: Agent
    ) -> PeerPresentation:
        console = _console_for_peer(peer_id)
        renderer = _renderer_for(console, peer_id)
        command_bus = UIEventBus()
        record_runtime_issue = getattr(presentation_agent, "record_runtime_issue", None)
        if callable(record_runtime_issue):
            command_bus.bind_subscriber_failure_sink(
                record_runtime_issue,
                agent_id=presentation_agent.agent_id,
                default=True,
            )
        command_bus.subscribe(renderer.on_ui_event, replay_history=False)
        return PeerPresentation(
            console,
            renderer,
            command_bus,
            AgentEventBridge(
                command_bus,
                generation_owner_agent_id=presentation_agent.agent_id,
            ),
        )

    def _connection_marker(peer_id: str) -> str:
        peer = relay_server.registry.get(peer_id)
        return f"{getattr(peer, 'connected_at', 0):.6f}" if peer is not None else "0"

    def _dispose_peer(peer_id: str) -> None:
        presenter = peer_presenters.pop(peer_id, None)
        if presenter is not None:
            presenter.renderer.close()
        peer_agent = peer_agents.pop(peer_id, None)
        if peer_agent is not None and peer_agent is not agent:
            ui_bus.unbind_subscriber_failure_sink(agent_id=peer_agent.agent_id)
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
            peer_presenters[peer_id] = _presentation_for_peer(peer_id, agent)
            return agent
        peer_config: Config = config

        peer_llm = runner.dependencies.create_llm(peer_config)
        peer_llm.ui_bus = ui_bus
        peer_backend = RemoteRelayToolBackend(relay_server=relay_server, ui_bus=ui_bus)
        peer_tools = runner.dependencies.load_tools(peer_backend)
        peer_hook_registry = runner.dependencies.create_hook_registry()
        peer_agent = runner.dependencies.create_agent(
            peer_llm, peer_tools, peer_config, peer_hook_registry
        )
        peer_agent.runtime_config = peer_config
        peer_agent.reasoning_display_mode = (
            "inline" if peer_config.ui.reasoning_display == "inline" else "quiet"
        )
        peer_agent.relay_server = relay_server
        peer_agent.extension_manager = runner._extension_manager
        peer_agent.skills_service = skills_service
        peer_agent.skills_catalog = agent.skills_catalog
        runner._register_hooks(peer_agent, peer_config)
        runner._wire_agent_tools(peer_agent)

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
            bind_issue = bind_session_persistence(
                peer_config,
                peer_agent,
                session_store,
                peer_agent.current_session_id,
                fingerprint=fingerprint,
            )
            presentation = _presentation_for_peer(peer_id, peer_agent)
            record_runtime_issue = getattr(peer_agent, "record_runtime_issue", None)
            if callable(record_runtime_issue):
                ui_bus.bind_subscriber_failure_sink(
                    record_runtime_issue,
                    agent_id=peer_agent.agent_id,
                )
            peer_agents[peer_id] = peer_agent
            peer_connection_markers[peer_id] = marker
            peer_presenters[peer_id] = presentation
            peer_agent.lifecycle.runner_started(
                metadata={"ui_bus": ui_bus, "peer_id": peer_id}
            )
            peer_agent.lifecycle.session_started(
                peer_agent.current_session_id,
                reason=(f"{reason}_persistence_unavailable" if bind_issue else reason),
                metadata={"peer_id": peer_id},
            )
            if bind_issue is not None:
                observe_session_callback(
                    ui_bus.warning if ui_bus is not None else None,
                    "Remote session is active, but persistence is unavailable "
                    f"({bind_issue.render()}).",
                    kind=UIEventKind.SESSION,
                    phase=bind_issue.phase,
                    error_type=bind_issue.error_type,
                    ref=bind_issue.ref,
                    agent=peer_agent,
                    diagnostic_phase="persistence_observer",
                    diagnostic_ref="ui_bus",
                )
            return peer_agent

        latest_result = session_store.get_latest_result(fingerprint=fingerprint)
        latest = latest_result.session
        inventory_issues = tuple(latest_result.issues)
        if latest:
            loaded = session_store.load(latest.id)
            if loaded is None:
                raise SessionRestoreError(
                    phase="session_load",
                    error_type="FileNotFoundError",
                    ref="session",
                ) from None
            apply_session_runtime_state(loaded, peer_config, peer_agent)
            peer_agent.session_inventory_issues = inventory_issues
            restore_issues = tuple(getattr(loaded, "restore_issues", ()))
            for diagnostic_phase, issues in (
                ("inventory_observer", inventory_issues),
                ("restore_observer", restore_issues),
            ):
                for issue in issues:
                    observe_session_callback(
                        ui_bus.warning if ui_bus is not None else None,
                        f"Remote session state is degraded ({issue.render()}).",
                        kind=UIEventKind.SESSION,
                        phase=issue.phase,
                        error_type=issue.error_type,
                        ref=issue.ref,
                        count=issue.count,
                        agent=peer_agent,
                        diagnostic_phase=diagnostic_phase,
                        diagnostic_ref="ui_bus",
                    )
            peer_agent.current_session_id = latest.id
            return _cache_created_agent(
                "remote_restore_degraded"
                if restore_issues or inventory_issues
                else "remote_restore"
            )

        new_session_id = session_store.generate_session_id()
        restore_config_runtime_defaults(peer_config, peer_agent)
        peer_agent.session_inventory_issues = inventory_issues
        for issue in inventory_issues:
            observe_session_callback(
                ui_bus.warning if ui_bus is not None else None,
                f"Remote session inventory degraded ({issue.render()}).",
                kind=UIEventKind.SESSION,
                phase=issue.phase,
                error_type=issue.error_type,
                ref=issue.ref,
                count=issue.count,
                agent=peer_agent,
                diagnostic_phase="inventory_observer",
                diagnostic_ref="ui_bus",
            )
        peer_agent.current_session_id = new_session_id
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
            **build_session_persistence_kwargs(peer_agent),
        )
        peer_agent.current_session_id = sid

        # Lifecycle delivery observes a completed save. It cannot turn that
        # durable success into a failed save (or cause the caller to retry it).
        try:
            peer_agent.lifecycle.session_saved(sid)
        except BaseException as error:
            _request_peer_stop(peer_agent, error)
            _record_peer_runtime_issue(
                peer_agent,
                "session_saved_observer",
                _safe_peer_error_type(error),
                "lifecycle",
            )

    def _safe_peer_error_type(error: BaseException) -> str:
        name = type(error).__name__
        if (
            name
            and len(name) <= 64
            and name.isascii()
            and name.replace("_", "").isalnum()
        ):
            return name
        return "Exception"

    def _request_peer_stop(peer_agent: Agent, error: BaseException) -> None:
        if not isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            return
        try:
            peer_agent.request_stop()
        except BaseException:
            peer_agent._control_plane_recovery_required = True

    def _record_peer_runtime_issue(
        peer_agent: Agent,
        phase: str,
        error_type: str,
        ref: str,
    ) -> None:
        recorder_error: BaseException | None = None
        recorder = getattr(peer_agent, "record_runtime_issue", None)
        if callable(recorder):
            try:
                recorder(phase, error_type, ref)
                return
            except BaseException as error:
                recorder_error = error
                _request_peer_stop(peer_agent, error)
        try:
            Agent.record_runtime_issue(peer_agent, phase, error_type, ref)
            if recorder_error is not None:
                Agent.record_runtime_issue(
                    peer_agent,
                    "runtime_issue_recorder",
                    _safe_peer_error_type(recorder_error),
                    ref,
                )
        except BaseException as error:
            _request_peer_stop(peer_agent, error)
            peer_agent._control_plane_recovery_required = True

    def _persistence_error_payload(peer_agent: Agent, error: BaseException) -> dict:
        if isinstance(error, SessionRestoreError):
            phase = error.phase
            error_type = error.error_type
            ref = error.ref
        else:
            phase = "session_save"
            error_type = _safe_peer_error_type(error)
            ref = "session"
        _record_peer_runtime_issue(peer_agent, phase, error_type, ref)
        return {
            "message": (
                "Session persistence failed "
                f"(phase={phase}, error_type={error_type}, ref={ref})"
            ),
            "code": "session_persistence_failed",
            "phase": phase,
            "error_type": error_type,
            "ref": ref,
        }

    def _save_peer_session_once(peer_agent: Agent, peer_id: str) -> dict | None:
        try:
            _save_peer_session(peer_agent, peer_id)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            _request_peer_stop(peer_agent, error)
            return _persistence_error_payload(peer_agent, error)
        return None

    def _chat(peer_id: str, prompt: str) -> ChatResponse:
        try:
            peer_agent = _create_peer_agent(peer_id)
        except SessionRestoreError as error:
            return ChatResponse(response="", error=str(error))
        chat_error: Exception | None = None
        response = ""
        try:
            response = peer_agent.chat(prompt)
        except Exception as error:
            chat_error = error
        persistence_error = _save_peer_session_once(peer_agent, peer_id)
        if chat_error is not None:
            message = str(chat_error)
            if persistence_error is not None:
                message += f"\n{persistence_error['message']}"
            return ChatResponse(response="", error=message)
        return ChatResponse(
            response=response,
            error=(
                str(persistence_error["message"])
                if persistence_error is not None
                else None
            ),
        )

    def _stream_chat(peer_id: str, prompt: str, remote_session) -> None:
        try:
            peer_agent = _create_peer_agent(peer_id)
        except SessionRestoreError as error:
            remote_session.append_event(
                "error",
                {
                    "message": str(error),
                    "code": error.code,
                    "phase": error.phase,
                    "error_type": error.error_type,
                    "ref": error.ref,
                },
            )
            return
        peer_agent.clear_stop_request()
        remote_session.cancel_callback = peer_agent.request_stop
        admit_steering = getattr(peer_agent, "admit_user_steering", None)
        interrupt_intent = getattr(peer_agent, "request_interrupt_intent", None)
        if callable(admit_steering) and callable(interrupt_intent):
            remote_session.bind_chat_control(
                admit_steering=admit_steering,
                interrupt_intent=interrupt_intent,
                stop_turn=peer_agent.request_stop,
            )
        presentation = peer_presenters[peer_id]
        ansi_console = presentation.console
        renderer = presentation.renderer

        def _flush_output() -> None:
            rendered = export_remote_console(ansi_console)
            if rendered:
                remote_session.append_event(
                    "output", {"format": "terminal", "content": rendered}
                )

        if prompt.strip().startswith("/") and config is not None:
            try:
                command_result = handle_command(
                    prompt.strip(),
                    peer_agent,
                    config,
                    getattr(peer_agent, "current_session_id", None),
                    presentation.ui_bus,
                    REMOTE_CLI_PROFILE,
                    action_registry,
                    sessions_dir,
                    skills_service,
                )
            except Exception as command_error:
                _flush_output()
                persistence_error = _save_peer_session_once(peer_agent, peer_id)
                remote_session.append_event(
                    "error", {"message": str(command_error)}
                )
                if persistence_error is not None:
                    remote_session.append_event("error", persistence_error)
                remote_session.append_event("chat_end", {"response": ""})
                return
            if command_result["action"] != "chat":
                peer_agent.current_session_id = command_result["session_id"]

                _flush_output()

                if command_result["action"] == "exit":
                    remote_session.append_event(
                        "output",
                        {
                            "format": "plain",
                            "content": "Exit command received. Use Ctrl+C to terminate remote peer.\n",
                        },
                    )
                persistence_error = _save_peer_session_once(peer_agent, peer_id)
                if persistence_error is not None:
                    remote_session.append_event("error", persistence_error)
                remote_session.append_event("chat_end", {"response": ""})
                return

        class _RemoteUIInteractor:
            def notify(self, event) -> None:
                renderer.on_ui_event(event)
                _flush_output()

            def _request(self, request):
                _flush_output()
                remote_session.register_interaction(request.request_id)
                render_interaction_request(
                    ansi_console,
                    request,
                    max_preview_lines=renderer.policy.tool_preview_lines,
                    max_preview_chars=renderer.policy.tool_preview_chars,
                )
                rendered_frame = export_remote_console(ansi_console)
                kind = {
                    ConfirmRequest: "confirm",
                    ChooseOneRequest: "choose_one",
                    InputTextRequest: "text_input",
                    ReviewRequest: "review",
                }[type(request)]
                payload = {
                    "request_id": request.request_id,
                    "kind": kind,
                    "rendered_frame": rendered_frame,
                    "input_constraints": interaction_constraints(request),
                }
                remote_session.append_event("interaction_request", payload)
                value, cancelled, reason = remote_session.wait_interaction(
                    request.request_id
                )
                remote_session.append_event(
                    "interaction_resolved",
                    {
                        "request_id": request.request_id,
                        "cancelled": cancelled,
                        "reason": reason,
                    },
                )
                return value, cancelled, reason

            def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
                value, cancelled, _ = self._request(request)
                return ConfirmResponse(confirmed=value is True, cancelled=cancelled)

            def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
                value, cancelled, _ = self._request(request)
                selected = value if isinstance(value, str) else None
                return ChooseOneResponse(selected_id=selected, cancelled=cancelled)

            def input_text(self, request: InputTextRequest) -> InputTextResponse:
                value, cancelled, _ = self._request(request)
                text = value if isinstance(value, str) else None
                return InputTextResponse(value=text, cancelled=cancelled)

            def review(self, request: ReviewRequest) -> ReviewResponse:
                value, cancelled, reason = self._request(request)
                if isinstance(value, dict):
                    action = value.get("action")
                    selected_id = value.get("selected_id")
                    feedback = value.get("reason")
                    if action in {"allow_once", "allow_session", "deny"}:
                        return ReviewResponse(
                            approved=action in {"allow_once", "allow_session"}
                            and not cancelled,
                            cancelled=cancelled,
                            reason=(
                                str(feedback)
                                if isinstance(feedback, str)
                                else reason
                            ),
                            action=action,
                            selected_id=(
                                str(selected_id)
                                if isinstance(selected_id, str)
                                else None
                            ),
                        )
                return ReviewResponse(
                    approved=value is True and not cancelled,
                    cancelled=cancelled,
                    reason=reason,
                )

            def cancel(self, request_id: str) -> None:
                remote_session.resolve_interaction(
                    request_id,
                    None,
                    True,
                    "interaction cancelled",
                )

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
            elif event.event_type == AgentEventType.USER_STEERING:
                remote_session.append_event(
                    "steering_applied",
                    {
                        "steering_id": event.data.get("steering_id"),
                        "attempt_id": event.data.get("attempt_id"),
                    },
                )
            presentation.agent_bridge.on_agent_event(event)
            _flush_output()

        for tool in peer_agent.tools:
            backend = getattr(tool, "backend", None)
            context = getattr(backend, "context", None)
            if isinstance(context, ExecutionContext):
                context.remote_stream_handler = _on_remote_stream

        previous_approval = peer_agent.approval_provider
        previous_interactor = getattr(peer_agent, "ui_interactor", None)
        interaction_coordinator = InteractionCoordinator(_RemoteUIInteractor())
        peer_agent.add_event_handler(_on_agent_event)
        peer_agent.ui_interactor = interaction_coordinator
        from reuleauxcoder.app.runtime.approval import build_runtime_approval_provider

        peer_agent.approval_provider = build_runtime_approval_provider(
            peer_agent, make_approval_handler(interaction_coordinator)
        )
        try:
            result = ""
            chat_error: Exception | None = None
            try:
                result = peer_agent.chat(prompt)
            except Exception as error:
                chat_error = error
            _flush_output()
            persistence_error = _save_peer_session_once(peer_agent, peer_id)
            if chat_error is not None:
                remote_session.append_event("error", {"message": str(chat_error)})
            if persistence_error is not None:
                remote_session.append_event("error", persistence_error)
            if chat_error is None:
                remote_session.append_event("chat_end", {"response": result})
        finally:
            interaction_coordinator.shutdown(reason="remote chat closed")
            peer_agent.approval_provider = previous_approval
            peer_agent.ui_interactor = previous_interactor
            try:
                peer_agent._event_handlers.remove(_on_agent_event)
            except ValueError:
                pass

    runner._relay_http_service.set_chat_handler(_chat)
    runner._relay_http_service.set_stream_chat_handler(_stream_chat)
