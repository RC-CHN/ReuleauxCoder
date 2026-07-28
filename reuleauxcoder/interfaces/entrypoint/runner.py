"""Application runner - shared initialization logic for all interfaces.

This module provides a unified entry point that handles:
- Configuration loading
- LLM client initialization
- Agent setup with hooks and tools
- MCP server management
- Session management

Different interfaces (CLI, TUI, VSCode extension) can reuse this logic
and only need to implement their own UI-specific rendering.
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.runtime.session_state import (
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.app.runtime.extension_bridge import LegacyHookLifecycleParticipant
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.process_manager import ProcessEvent, ProcessManager
from reuleauxcoder.domain.runtime.events import (
    ProcessSessionChanged,
    RuntimeEvent,
)
from reuleauxcoder.domain.hooks import (
    discover_hook_specs,
    instantiate_hooks,
)
from reuleauxcoder.domain.extensions import (
    ExtensionDefinition,
    ExtensionPhase,
    ExtensionManager,
    ExtensionManifest,
    ExtensionScope,
)
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.interfaces.entrypoint.dependencies import (
    AppContext,
    AppDependencies,
    AppOptions,
    _default_create_remote_artifact_provider,
)
from reuleauxcoder.interfaces.entrypoint.remote_relay import (
    bind_remote_chat_handler,
    init_remote_relay,
)
from reuleauxcoder.interfaces.entrypoint.session_lifecycle import restore_session
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.infrastructure.persistence.notes_store import NoteStore
from reuleauxcoder.infrastructure.version_control import GitMonitor
from reuleauxcoder.services.llm.client import LLM

__all__ = ["AppRunner", "_default_create_remote_artifact_provider"]


class AppRunner:
    """Application runner that handles initialization and cleanup."""

    def __init__(
        self,
        options: AppOptions | None = None,
        dependencies: AppDependencies | None = None,
        startup_progress: Callable[[str], None] | None = None,
    ):
        self.options = options or AppOptions()
        self.dependencies = dependencies or AppDependencies()
        self._startup_progress = startup_progress
        self._mcp_manager: MCPManager | None = None
        self._relay_server: RelayServer | None = None
        self._relay_http_service: RemoteRelayHTTPService | None = None
        self._lsp_manager: LspManager | None = None
        self._git_monitor: GitMonitor | None = None
        self._process_manager: ProcessManager | None = None
        self._agent: Agent | None = None
        self._ui_bus: UIEventBus | None = None
        self._remote_chat_cleanup: Callable[[], None] | None = None
        self._extension_manager = ExtensionManager()
        self._extension_manager.register(
            ExtensionDefinition(
                manifest=ExtensionManifest(
                    extension_id="core.hooks",
                    version="1.0.0",
                    scopes=frozenset({ExtensionScope.RUNNER}),
                    phase=ExtensionPhase.LIFECYCLE,
                    remote_compatible=True,
                    thread_safe=False,
                ),
                factory=lambda context: LegacyHookLifecycleParticipant(
                    coordinator=context.services["agent"].lifecycle,
                    ui_bus=context.services["ui_bus"],
                    session_id=context.services.get("session_id"),
                ),
            )
        )

    def initialize(self) -> AppContext:
        """Initialize all application components and return context."""
        self._report_startup("Loading configuration...")
        config = self.dependencies.load_config(self.options.config_path)
        self._report_startup(f"Configuration loaded (model: {config.model}).")
        if self.options.server_mode:
            config.remote_exec.enabled = True
            config.remote_exec.host_mode = True
        self._report_startup("Initializing command registry and runtime services...")
        ui_bus = self.dependencies.create_ui_bus()
        self._ui_bus = ui_bus
        action_registry = self.dependencies.create_action_registry()
        self._init_remote_relay(config, ui_bus)
        config, ui_bus, llm, agent = self._build_core(config, ui_bus)
        self._agent = agent
        self._bind_remote_chat_handler(agent, action_registry)
        self._report_startup("Discovering skills...")
        skills_service = self._init_skills(config, agent, ui_bus)
        self._report_startup("Skills catalog ready.")
        enabled_mcp_servers = sum(
            1 for server in config.mcp_servers if getattr(server, "enabled", True)
        )
        if enabled_mcp_servers:
            self._report_startup(
                f"Connecting {enabled_mcp_servers} configured MCP server(s)..."
            )
        mcp_manager = self._attach_mcp_if_configured(config, agent, ui_bus)
        if enabled_mcp_servers:
            self._report_startup(
                "MCP discovery started in the background; continuing startup."
            )
        sessions_dir = Path(config.session_dir) if config.session_dir else None
        if self.options.server_mode:
            restore_config_runtime_defaults(config, agent)
            current_session_id, session_exit_time = None, None
            self._report_startup("Session restore skipped in server mode.")
        else:
            current_session_id, session_exit_time, sessions_dir = self._restore_session(
                config, agent, ui_bus
            )

        app_ctx = AppContext(
            config=config,
            llm=llm,
            agent=agent,
            ui_bus=ui_bus,
            ui_interactor=None,
            mcp_manager=mcp_manager,
            skills_service=skills_service,
            action_registry=action_registry,
            process_manager=self._process_manager,
            current_session_id=current_session_id,
            session_exit_time=session_exit_time,
            sessions_dir=sessions_dir,
        )
        extension_scope = self._extension_manager.open_scope(
            ExtensionScope.RUNNER,
            "runner",
            services={
                "agent": agent,
                "ui_bus": ui_bus,
                "session_id": current_session_id,
            },
        )
        agent.extension_manager = self._extension_manager
        agent.extension_scope = extension_scope
        hook_participant = extension_scope.get("core.hooks")
        if hook_participant is None:
            raise RuntimeError("Core hook lifecycle extension failed to initialize")
        hook_participant.start()
        self._report_startup("Runtime initialization complete.")
        return app_ctx

    def _report_startup(self, message: str) -> None:
        if self._startup_progress is not None:
            try:
                self._startup_progress(message)
            except Exception:
                # Progress reporting is advisory and must never make startup
                # fail when an embedding UI callback is unavailable.
                pass

    def _build_core(
        self,
        config: Config,
        ui_bus: UIEventBus,
    ) -> tuple[Config, UIEventBus, LLM, Agent]:
        """Build config + ui bus + llm + agent, with runtime hooks initialized."""
        if self.options.model:
            config.model = self.options.model

        self._report_startup("Initializing model client...")
        llm = self.dependencies.create_llm(config)
        llm.ui_bus = ui_bus
        self._report_startup("Loading built-in tools...")
        tool_backend = self.dependencies.create_tool_backend(config, ui_bus)
        if self._relay_server is not None:
            tool_backend = RemoteRelayToolBackend(
                relay_server=self._relay_server, ui_bus=ui_bus
            )
        tools = self.dependencies.load_tools(tool_backend)
        self._report_startup(f"Loaded {len(tools)} built-in tool(s).")
        hook_registry = self.dependencies.create_hook_registry()
        agent = self.dependencies.create_agent(llm, tools, config, hook_registry)
        # Custom dependency factories may return an Agent without forwarding
        # config.  Runtime services and tool adapters must still see the exact
        # effective configuration loaded by this runner.
        agent.config = config
        agent.runtime_config = config
        agent.notes_store = NoteStore(
            Path.cwd(),
            workspace_max=config.notes_workspace_max,
            global_max=config.notes_global_max,
        )
        agent.reasoning_display_mode = (
            "inline" if config.ui.reasoning_display == "inline" else "quiet"
        )
        agent.relay_server = self._relay_server
        agent.current_session_id = None
        agent.session_fingerprint = get_session_fingerprint(config, agent)
        agent.context._ui_bus = ui_bus
        process_manager = self.dependencies.create_process_manager(
            lambda event: self._emit_process_event(ui_bus, event)
        )
        self._process_manager = process_manager
        agent.process_manager = process_manager

        self._report_startup("Discovering runtime hooks and workspace services...")
        self._register_hooks(agent, config)
        agent.hook_registry.bind_runtime_service(
            "process_manager", process_manager
        )
        self._init_git_monitor(agent)
        if LspConfig.from_config(config).enabled:
            self._report_startup("Checking configured language servers...")
        self._init_lsp(config, agent, ui_bus)
        self._wire_agent_tools(agent)
        self._hint_rtk_install(config, ui_bus)
        return config, ui_bus, llm, agent

    @staticmethod
    def _hint_rtk_install(config: Config, ui_bus: UIEventBus) -> None:
        """Explain the explicit-only RTK boundary without rewriting commands."""
        rtk_mode = getattr(config, "shell_rtk", "off")
        if rtk_mode == "off":
            return
        import shutil

        installed = shutil.which("rtk") is not None
        if installed:
            ui_bus.info(
                "[rtk] available for explicit use. Automatic command rewriting "
                "is disabled, so shell commands execute unchanged.",
                kind=UIEventKind.SYSTEM,
            )
            return
        if rtk_mode == "on":
            ui_bus.warning(
                "[rtk] shell.rtk=on no longer rewrites commands automatically, "
                "and rtk is not installed. Shell commands still execute unchanged.",
                kind=UIEventKind.SYSTEM,
            )

    def _init_remote_relay(self, config: Config, ui_bus: UIEventBus) -> None:
        init_remote_relay(self, config, ui_bus)

    def _bind_remote_chat_handler(
        self, agent: Agent, action_registry: ActionRegistry
    ) -> None:
        bind_remote_chat_handler(self, agent, action_registry)

    def _register_hooks(self, agent: Agent, config: Config) -> None:
        """Register hooks discovered via decorator mechanism."""
        specs = discover_hook_specs()
        hooks = instantiate_hooks(specs, config)
        for hook_point, hook in hooks:
            agent.register_hook(hook_point, hook)

    def _init_lsp(self, config: Config, agent: Agent, ui_bus: UIEventBus) -> None:
        """Initialize LSP infrastructure if the [lsp] section is configured."""
        lsp_config = LspConfig.from_config(config)
        if not lsp_config.enabled:
            return
        if any(
            getattr(tool, "backend_id", "local") == "remote_relay"
            for tool in agent.tools
        ):
            ui_bus.info(
                "LSP: Host diagnostics disabled for the remote workspace target.",
                kind=UIEventKind.SYSTEM,
            )
            return

        manager = LspManager(
            lsp_config,
            workspace_cwd=Path.cwd(),
            ui_bus=ui_bus,
            runtime_event_sink=ui_bus.emit_runtime,
        )
        report = manager.health_check()

        if report.available == 0:
            ui_bus.info(
                "LSP: No language servers found on PATH. "
                "Install pyright, rust-analyzer, gopls, etc. for diagnostics.",
                kind=UIEventKind.SYSTEM,
            )
            return

        _MAX_CMD_LEN = 55

        def _fmt(cmd: str) -> str:
            return cmd if len(cmd) <= _MAX_CMD_LEN else cmd[:_MAX_CMD_LEN] + "..."

        available_lines = [
            f"  ✓ {lang_name} ({_fmt(details)})"
            for lang_name, available, details in report.languages
            if available
        ]
        missing_lines = [
            f"  ✗ {lang_name} ({_fmt(details)})"
            for lang_name, available, details in report.languages
            if not available
        ]

        ui_bus.info(
            f"LSP: {report.available}/{report.total} language servers ready\n"
            + "\n".join(available_lines),
            kind=UIEventKind.SYSTEM,
        )
        if missing_lines:
            ui_bus.debug(
                "LSP: unavailable servers\n" + "\n".join(missing_lines),
                kind=UIEventKind.SYSTEM,
            )

        manager.start_worker()
        self._lsp_manager = manager
        agent.lsp_manager = manager

        agent.hook_registry.bind_runtime_service("lsp_manager", manager)
        for tool in agent.tools:
            bind = getattr(tool, "bind_lsp_manager", None)
            if callable(bind):
                bind(manager)

    def _init_git_monitor(self, agent: Agent) -> None:
        """Bind root-local Git observation to its request-tail hook."""
        if self._relay_server is not None:
            self._git_monitor = None
        else:
            self._git_monitor = GitMonitor(Path.cwd())
        agent.hook_registry.bind_runtime_service("git_monitor", self._git_monitor)

    @staticmethod
    def _wire_agent_tools(agent: Agent) -> None:
        """Bind root agent/config services into tools that declare adapters."""
        for tool in agent.tools:
            tool._agent_config = agent.config
            bind_agent = getattr(tool, "bind_agent", None)
            if callable(bind_agent):
                bind_agent(agent)

    @staticmethod
    def _emit_process_event(ui_bus: UIEventBus, event: ProcessEvent) -> None:
        snapshot = event.snapshot
        ui_bus.emit_runtime(
            RuntimeEvent(
                payload=ProcessSessionChanged(
                    change=event.kind.value,
                    process_session_id=snapshot.session_id,
                    state=snapshot.state.value,
                    stream_mode=snapshot.stream_mode.value,
                    backend=snapshot.backend,
                    command=event.command,
                    cwd=event.cwd,
                    elapsed_seconds=snapshot.elapsed_seconds,
                    exit_code=snapshot.exit_code,
                    termination_reason=snapshot.termination_reason,
                    output_truncated=snapshot.output_truncated,
                    output_decode_replaced=snapshot.output_decode_replaced,
                ),
                agent_id=event.owner_agent_id,
                session_generation=event.session_generation,
                session_id=event.owner_session_id,
                turn_id=event.origin_turn_id,
                correlation_id=snapshot.session_id,
            )
        )

    def _attach_mcp_if_configured(
        self,
        config: Config,
        agent: Agent,
        ui_bus: UIEventBus,
    ) -> MCPManager | None:
        """Initialize and attach MCP runtime if servers are configured."""
        mcp_manager = None
        if config.mcp_servers:
            mcp_manager = self._init_mcp(config.mcp_servers, agent, ui_bus)
        agent.mcp_manager = mcp_manager
        return mcp_manager

    def _init_skills(
        self, config: Config, agent: Agent, ui_bus: UIEventBus
    ) -> SkillsService:
        """Initialize skills service and attach stable catalog to the agent."""
        skills_service = SkillsService(
            workspace_dir=Path.cwd(),
            home_dir=Path.home(),
            enabled=config.skills.enabled,
            scan_project=config.skills.scan_project,
            scan_user=config.skills.scan_user,
            disabled_names=list(config.skills.disabled),
        )
        reload_result = skills_service.reload()
        agent.skills_service = skills_service
        agent.skills_catalog = reload_result.catalog

        if not config.skills.enabled:
            ui_bus.info("Skills disabled by config.", kind=UIEventKind.SYSTEM)
            return skills_service

        ui_bus.info(
            f"Skills loaded: {len(reload_result.all_skills)} discovered, {len(reload_result.active_skills)} active.",
            kind=UIEventKind.SYSTEM,
        )
        if reload_result.added:
            ui_bus.info(
                "Skills added: " + ", ".join(reload_result.added),
                kind=UIEventKind.SYSTEM,
            )
        for name in reload_result.removed:
            ui_bus.warning(f"Skill removed: {name}", kind=UIEventKind.SYSTEM)
        for name in reload_result.missing:
            ui_bus.warning(
                f"Skill not found and skipped: {name}", kind=UIEventKind.SYSTEM
            )
        for diagnostic in reload_result.diagnostics:
            emit = ui_bus.warning if diagnostic.level == "warning" else ui_bus.error
            emit(diagnostic.message, kind=UIEventKind.SYSTEM)
        return skills_service

    def _restore_session(
        self,
        config: Config,
        agent: Agent,
        ui_bus: UIEventBus,
    ) -> tuple[str | None, str | None, Path | None]:
        return restore_session(
            self.options,
            self.dependencies,
            config,
            agent,
            ui_bus,
            progress=self._report_startup,
        )

    def cleanup(
        self,
        agent: Agent | None = None,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        """Clean up resources (MCP connections, remote relay, etc.)."""
        shutdown_id = f"shutdown-{uuid.uuid4().hex}"
        shutdown_started_monotonic = time.monotonic()
        phase_by_message = {
            "Closing pending interactions...": "stop_interactions",
            "Stopping remote chat handler...": "stop_remote_chat",
            "Stopping sub-agent workers...": "stop_subagents",
            "Stopping shell process sessions...": "stop_processes",
            "Disposing runtime extensions...": "dispose_extensions",
            "Stopping remote relay HTTP service...": "stop_remote_relay",
            "Stopping remote relay peers...": "stop_remote_relay",
            "Disconnecting MCP servers...": "disconnect_mcp",
            "Stopping language servers...": "stop_lsp",
            "Releasing workspace monitors...": "release_monitors",
        }

        def report(message: str) -> None:
            if self._ui_bus is not None:
                phase = phase_by_message.get(message)
                if phase is not None:
                    self._ui_bus.emit_operation_phase(
                        operation_id=shutdown_id,
                        operation="shutdown",
                        phase=phase,
                        started_at=time.time(),
                        cancelable=True,
                        agent_id=getattr(agent or self._agent, "agent_id", None),
                        session_generation=getattr(
                            agent or self._agent, "session_generation", None
                        ),
                        session_id=getattr(
                            agent or self._agent, "current_session_id", None
                        ),
                    )
            if progress is None:
                return
            try:
                progress(message)
            except Exception:
                pass

        agent = agent or self._agent
        if agent is not None:
            report("Closing pending interactions...")
            shutdown_interactions = getattr(
                getattr(agent, "ui_interactor", None), "shutdown", None
            )
            if callable(shutdown_interactions):
                shutdown_interactions(reason="application shutdown")
        if self._remote_chat_cleanup is not None:
            report("Stopping remote chat handler...")
            self._remote_chat_cleanup()
            self._remote_chat_cleanup = None
        if agent is not None:
            subagent_manager = getattr(agent, "_subagent_manager", None)
            if subagent_manager is not None:
                report("Stopping sub-agent workers...")
                # Jobs receive their cancellation signal above. Do not let an
                # uncooperative provider/tool hold the foreground exit path.
                subagent_manager.shutdown(wait=False)
        if self._process_manager is not None:
            report("Stopping shell process sessions...")
            process_report = self._process_manager.shutdown()
            if progress is not None and process_report.total:
                report(
                    "Shell process cleanup finished "
                    f"({process_report.total} tracked, "
                    f"{process_report.already_exited} already exited, "
                    f"{process_report.interrupted} soft-interrupt request(s), "
                    f"{process_report.terminated} force-termination request(s), "
                    f"{process_report.unknown} unknown, "
                    f"{process_report.reap_timeouts} reap timeout(s))."
                )
            if agent is not None:
                agent.process_manager = None
                agent.hook_registry.bind_runtime_service("process_manager", None)
            self._process_manager = None
        report("Disposing runtime extensions...")
        extension_diagnostics = self._extension_manager.dispose_all()
        if self._ui_bus is not None:
            for diagnostic in extension_diagnostics:
                self._ui_bus.warning(
                    f"Extension {diagnostic.extension_id} {diagnostic.phase} failed: "
                    f"{diagnostic.message}"
                )
        if self._relay_http_service is not None:
            report("Stopping remote relay HTTP service...")
            artifact_provider = getattr(
                self._relay_http_service, "artifact_provider", None
            )
            build_dir = (
                getattr(artifact_provider, "_build_dir", None)
                if artifact_provider is not None
                else None
            )
            self._relay_http_service.stop()
            self._relay_http_service = None
            if isinstance(build_dir, Path):
                shutil.rmtree(build_dir, ignore_errors=True)
        if self._relay_server is not None:
            report("Stopping remote relay peers...")
            for peer in self._relay_server.registry.list_online():
                try:
                    self._relay_server.request_cleanup(peer.peer_id, timeout_sec=5)
                except Exception:
                    pass
            self._relay_server.stop()
            self._relay_server = None
        if self._mcp_manager:
            report("Disconnecting MCP servers...")
            self._mcp_manager.stop()
            self._mcp_manager = None
        if self._lsp_manager:
            report("Stopping language servers...")
            if self._agent is not None:
                self._agent.hook_registry.bind_runtime_service("lsp_manager", None)
                for tool in self._agent.tools:
                    bind = getattr(tool, "bind_lsp_manager", None)
                    if callable(bind):
                        bind(None)
            self._lsp_manager.shutdown_all()
            self._lsp_manager = None
            if self._agent is not None:
                self._agent.lsp_manager = None
        if self._agent is not None:
            report("Releasing workspace monitors...")
            self._agent.hook_registry.bind_runtime_service("git_monitor", None)
        self._git_monitor = None
        elapsed = time.monotonic() - shutdown_started_monotonic
        if self._ui_bus is not None:
            self._ui_bus.emit_operation_phase(
                operation_id=shutdown_id,
                operation="shutdown",
                phase="completed",
                status="completed",
                elapsed_ms=int(elapsed * 1000),
                agent_id=getattr(agent, "agent_id", None),
                session_generation=getattr(agent, "session_generation", None),
                session_id=getattr(agent, "current_session_id", None),
            )
        report(f"Background services stopped in {elapsed:.1f}s.")
        self._agent = None
        self._ui_bus = None

    def _init_mcp(
        self, mcp_servers: list[Any], agent: Agent, ui_bus: UIEventBus
    ) -> MCPManager:
        """Initialize MCP manager and connect to servers."""
        manager = self.dependencies.create_mcp_manager(ui_bus)

        enabled_servers = [s for s in mcp_servers if getattr(s, "enabled", True)]
        manager.connect_servers_async(enabled_servers)
        ui_bus.info(
            f"Connecting {len(enabled_servers)} MCP server(s) in the background.",
            kind=UIEventKind.MCP,
        )

        self._mcp_manager = manager
        return manager
