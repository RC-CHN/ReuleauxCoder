"""Remote relay bootstrap and peer chat binding helpers."""

from __future__ import annotations

from pathlib import Path
import threading


from reuleauxcoder.app.runtime.session_state import (
    bind_session_persistence,
    build_session_persistence_kwargs,
    apply_session_runtime_state,
    build_session_runtime_state,
    restore_config_runtime_defaults,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.protocol import ChatResponse
from reuleauxcoder.extensions.remote_exec.protocol import TerminalCapabilities
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.tools.backend import ExecutionContext
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.ui_events import UIEventBus, UIEventKind
from reuleauxcoder.app.rpc.client import RuntimeClient
from reuleauxcoder.app.rpc.server import RuntimeServer
from reuleauxcoder.infrastructure.rpc.peer import RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import MemoryTransport
from reuleauxcoder.interfaces.entrypoint.rpc import LocalConnection
from reuleauxcoder.interfaces.entrypoint.session_lifecycle import (
    observe_session_callback,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionRestoreError


def create_remote_console(terminal):
    from reuleauxcoder.interfaces.relay import create_remote_console as create

    return create(terminal)


def export_remote_console(console, *, clear=True):
    from reuleauxcoder.interfaces.relay import export_remote_console as export

    return export(console, clear=clear)


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

    from reuleauxcoder.interfaces.relay import RelayUI
    from reuleauxcoder.interfaces.cli.registration import REMOTE_CLI_PROFILE

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
    peer_presenters: dict[str, RelayUI] = {}
    peer_connections: dict[str, LocalConnection] = {}
    peer_lifecycle = threading.Condition()
    initializing_peers = 0
    closing = False

    def _terminal_for_peer(peer_id: str) -> TerminalCapabilities:
        peer = relay_server.registry.get(peer_id)
        return TerminalCapabilities.from_dict(
            peer.meta.get("terminal")
            if peer is not None and isinstance(peer.meta, dict)
            else None
        )

    def _connect_peer(peer_id, peer_agent, session_exit_time=None):
        presentation = RelayUI(lambda: _terminal_for_peer(peer_id))
        command_bus = UIEventBus()
        command_bus.bind_subscriber_failure_sink(
            peer_agent.record_runtime_issue,
            agent_id=peer_agent.agent_id,
            default=True,
        )
        commands = CommandService(
            peer_agent,
            config,
            command_bus,
            REMOTE_CLI_PROFILE,
            action_registry,
            sessions_dir=sessions_dir,
            skills_service=skills_service,
            session_exit_time=session_exit_time,
        )
        left, right = MemoryTransport.pair()
        frontend, backend = RpcPeer(left), RpcPeer(right)
        client = RuntimeClient(frontend, presentation.bus, presentation)
        server = RuntimeServer(commands, backend)
        backend.methods["runtime.checkpoint"] = lambda: commands.checkpoint(
            lambda: _save_peer_session_once(peer_agent, peer_id)
        )
        connection = LocalConnection(client, server)
        backend.start()
        frontend.start()
        try:
            client.initialize(REMOTE_CLI_PROFILE, activate=False)
            presentation.connect(client)
        except BaseException:
            connection.close()
            raise
        peer_presenters[peer_id] = presentation
        peer_connections[peer_id] = connection

        def stream(tool_name, chunk):
            server._notify(
                "relay.tool_stream",
                tool_name=tool_name,
                format="plain",
                stream=chunk.chunk_type,
                content=chunk.data,
            )

        for tool in peer_agent.tools:
            context = getattr(getattr(tool, "backend", None), "context", None)
            if isinstance(context, ExecutionContext):
                context.remote_stream_handler = stream

    def _connection_marker(peer_id: str) -> str:
        peer = relay_server.registry.get(peer_id)
        return f"{getattr(peer, 'connected_at', 0):.6f}" if peer is not None else "0"

    def _dispose_peer(peer_id: str) -> None:
        connection = peer_connections.pop(peer_id, None)
        try:
            if connection is not None:
                connection.close()
        finally:
            presenter = peer_presenters.pop(peer_id, None)
            if presenter is not None:
                presenter.close()
            peer_agent = peer_agents.pop(peer_id, None)
            peer_connection_markers.pop(peer_id, None)
            if peer_agent is not None and peer_agent is not agent:
                ui_bus.unbind_subscriber_failure_sink(agent_id=peer_agent.agent_id)
            manager = getattr(peer_agent, "_subagent_manager", None)
            if manager is not None:
                manager.shutdown(wait=True)
            if peer_agent is not None and peer_agent is not agent:
                peer_agent.lifecycle.runner_shutdown()

    def _dispose_all_peers() -> None:
        nonlocal closing
        with peer_lifecycle:
            closing = True
            # Initialization publishes the connection only after its handshake.
            # Wait for ownership to be registered before taking the cleanup set.
            peer_lifecycle.wait_for(lambda: initializing_peers == 0)
        failure = None
        for peer_id in tuple(peer_presenters):
            try:
                _dispose_peer(peer_id)
            except BaseException as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

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

    def _peer_view(peer_id: str) -> RelayUI:
        nonlocal initializing_peers
        with peer_lifecycle:
            if closing:
                raise RuntimeError("Remote relay host is closing")
            initializing_peers += 1
        try:
            _initialize_peer_agent(peer_id)
            return peer_presenters[peer_id]
        finally:
            with peer_lifecycle:
                initializing_peers -= 1
                peer_lifecycle.notify_all()

    def _initialize_peer_agent(peer_id: str) -> Agent:
        marker = _connection_marker(peer_id)
        existing = peer_agents.get(peer_id)
        if existing is not None and peer_connection_markers.get(peer_id) == marker:
            return existing
        _dispose_peer(peer_id)
        if config is None:
            peer_agents[peer_id] = agent
            peer_connection_markers[peer_id] = marker
            _connect_peer(peer_id, agent)
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

        def _cache_created_agent(
            reason: str, session_exit_time: str | None = None
        ) -> Agent:
            bind_issue = bind_session_persistence(
                peer_config,
                peer_agent,
                session_store,
                peer_agent.current_session_id,
                fingerprint=fingerprint,
            )
            record_runtime_issue = getattr(peer_agent, "record_runtime_issue", None)
            if callable(record_runtime_issue):
                ui_bus.bind_subscriber_failure_sink(
                    record_runtime_issue,
                    agent_id=peer_agent.agent_id,
                )
            _connect_peer(peer_id, peer_agent, session_exit_time)
            peer_agents[peer_id] = peer_agent
            peer_connection_markers[peer_id] = marker
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
                        diagnostic_phase=diagnostic_phase,
                        diagnostic_ref="ui_bus",
                    )
            peer_agent.current_session_id = latest.id
            return _cache_created_agent(
                "remote_restore_degraded"
                if restore_issues or inventory_issues
                else "remote_restore",
                session_exit_time=session_store.get_exit_time(loaded.messages),
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
            presentation = _peer_view(peer_id)
        except SessionRestoreError as error:
            return ChatResponse(response="", error=str(error))
        return presentation.run(prompt)

    def _stream_chat(peer_id: str, prompt: str, remote_session) -> None:
        try:
            presentation = _peer_view(peer_id)
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
        presentation.run(prompt, remote_session)

    runner._relay_http_service.set_chat_handler(_chat)
    runner._relay_http_service.set_stream_chat_handler(_stream_chat)
