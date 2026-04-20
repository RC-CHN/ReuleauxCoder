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

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Callable

from reuleauxcoder.app.runtime.session_state import (
    apply_session_runtime_state,
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.hooks import (
    HookPoint,
    RunnerShutdownContext,
    RunnerStartupContext,
    SessionSaveContext,
    SessionStartContext,
    discover_hook_specs,
    instantiate_hooks,
)
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.registry import build_tools
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.interactions import UIInteractor
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.llm.client import LLM


def _default_load_config(path: Path | None) -> Config:
    return ConfigLoader.from_path(path)


def _default_create_llm(config: Config) -> LLM:
    return LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        preserve_reasoning_content=getattr(config, "preserve_reasoning_content", True),
        backfill_reasoning_content_for_tool_calls=getattr(
            config, "backfill_reasoning_content_for_tool_calls", False
        ),
        debug_trace=getattr(config, "llm_debug_trace", False),
    )


def _default_create_tool_backend(config: Config, ui_bus: UIEventBus) -> ToolBackend:
    return LocalToolBackend()


def _default_load_tools(tool_backend: ToolBackend) -> list[Any]:
    return build_tools(tool_backend)


def _default_create_agent(llm: LLM, tools: list[Any], config: Config) -> Agent:
    return Agent(
        llm=llm,
        tools=tools,
        config=config,
        max_context_tokens=config.max_context_tokens,
        available_modes=getattr(config, "modes", {}) or {},
        active_mode=getattr(config, "active_mode", None),
    )


def _default_create_session_store(sessions_dir: Path | None) -> SessionStore:
    return SessionStore(sessions_dir)


def _default_create_mcp_manager(ui_bus: UIEventBus) -> MCPManager:
    return MCPManager(ui_bus=ui_bus)


def _default_create_remote_relay_server(config: Config) -> RelayServer | None:
    if not getattr(config, "remote_exec", None) or not config.remote_exec.enabled:
        return None
    if not config.remote_exec.host_mode:
        return None
    return RelayServer(
        heartbeat_interval_sec=config.remote_exec.heartbeat_interval_sec,
        heartbeat_timeout_sec=config.remote_exec.heartbeat_timeout_sec,
        default_tool_timeout_sec=config.remote_exec.default_tool_timeout_sec,
        shell_timeout_sec=config.remote_exec.shell_timeout_sec,
    )


def _default_create_remote_artifact_provider(ui_bus: UIEventBus) -> Callable[[str, str, str], tuple[bytes, str] | None]:
    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "reuleauxcoder-agent"
    build_dir = Path(tempfile.mkdtemp(prefix="rcoder-peer-build-"))
    build_lock = threading.Lock()
    cache: dict[tuple[str, str], Path] = {}

    def provide(os_name: str, arch: str, artifact_name: str) -> tuple[bytes, str] | None:
        if artifact_name != "rcoder-peer":
            return None
        if os_name not in {"linux", "darwin", "windows"}:
            return None
        if arch not in {"amd64", "arm64"}:
            return None

        target_key = (os_name, arch)
        with build_lock:
            binary_path = cache.get(target_key)
            if binary_path is None or not binary_path.exists():
                output_name = artifact_name + (".exe" if os_name == "windows" else "")
                binary_path = build_dir / f"{os_name}-{arch}-{output_name}"
                env = dict(os.environ)
                env["GOOS"] = os_name
                env["GOARCH"] = arch
                subprocess.run(
                    ["go", "build", "-o", str(binary_path), "./cmd/reuleauxcoder-agent"],
                    cwd=agent_dir,
                    env=env,
                    check=True,
                    timeout=180,
                )
                cache[target_key] = binary_path
                ui_bus.info(
                    f"Built peer artifact for {os_name}/{arch}: {binary_path.name}",
                    kind=UIEventKind.REMOTE,
                    os=os_name,
                    arch=arch,
                )
        return binary_path.read_bytes(), "application/octet-stream"

    setattr(provide, "_build_dir", build_dir)
    return provide


def _default_create_remote_http_service(
    config: Config,
    relay_server: RelayServer,
    ui_bus: UIEventBus,
) -> RemoteRelayHTTPService | None:
    if not getattr(config, "remote_exec", None) or not config.remote_exec.enabled:
        return None
    if not config.remote_exec.host_mode:
        return None
    return RemoteRelayHTTPService(
        relay_server=relay_server,
        bind=config.remote_exec.relay_bind,
        ui_bus=ui_bus,
        artifact_provider=_default_create_remote_artifact_provider(ui_bus),
    )


@dataclass
class AppDependencies:
    """Lightweight dependency providers for AppRunner.

    Keep defaults production-safe while allowing tests/entrypoints to override
    any component construction without a heavy DI framework.
    """

    load_config: Callable[[Path | None], Config] = _default_load_config
    create_ui_bus: Callable[[], UIEventBus] = UIEventBus
    create_llm: Callable[[Config], LLM] = _default_create_llm
    create_tool_backend: Callable[[Config, UIEventBus], ToolBackend] = _default_create_tool_backend
    load_tools: Callable[[ToolBackend], list[Any]] = _default_load_tools
    create_agent: Callable[[LLM, list[Any], Config], Agent] = _default_create_agent
    create_session_store: Callable[[Path | None], SessionStore] = _default_create_session_store
    create_mcp_manager: Callable[[UIEventBus], MCPManager] = _default_create_mcp_manager
    create_remote_relay_server: Callable[[Config], RelayServer | None] = _default_create_remote_relay_server
    create_remote_http_service: Callable[[Config, RelayServer, UIEventBus], RemoteRelayHTTPService | None] = (
        _default_create_remote_http_service
    )


@dataclass
class AppContext:
    """Context object containing all initialized application components."""

    config: Config
    """Loaded configuration."""

    llm: LLM
    """Initialized LLM client."""

    agent: Agent
    """Initialized Agent with tools and hooks."""

    ui_bus: UIEventBus
    """UI event bus for cross-component communication."""

    ui_interactor: UIInteractor | None = None
    """Optional UI interactor for synchronous interface prompts."""

    mcp_manager: MCPManager | None = None
    """MCP manager if MCP servers are configured."""

    skills_service: SkillsService | None = None
    """Skills service for discovery, reload, and catalog rendering."""

    current_session_id: str | None = None
    """Current session ID if resuming a session."""

    session_exit_time: str | None = None
    """Exit time of resumed session."""

    sessions_dir: Path | None = None
    """Directory for session storage."""


@dataclass
class AppOptions:
    """Options for application initialization."""

    config_path: Path | None = None
    """Path to config.yaml file."""

    model: str | None = None
    """Override model from config."""

    resume_session_id: str | None = None
    """Session ID to resume."""

    auto_resume_latest: bool = True
    """Whether to auto-resume the latest session."""

    server_mode: bool = False
    """Whether to run as a dedicated remote relay host."""


class AppRunner:
    """Application runner that handles initialization and cleanup.

    Usage:
        runner = AppRunner(options)
        ctx = runner.initialize()
        try:
            # Use ctx.agent, ctx.llm, ctx.ui_bus, etc.
            ...
        finally:
            runner.cleanup()
    """

    def __init__(
        self,
        options: AppOptions | None = None,
        dependencies: AppDependencies | None = None,
    ):
        self.options = options or AppOptions()
        self.dependencies = dependencies or AppDependencies()
        self._mcp_manager: MCPManager | None = None
        self._relay_server: RelayServer | None = None
        self._relay_http_service: RemoteRelayHTTPService | None = None

    def initialize(self) -> AppContext:
        """Initialize all application components and return context.

        Returns:
            AppContext with all initialized components.
        """
        config = self.dependencies.load_config(self.options.config_path)
        ui_bus = self.dependencies.create_ui_bus()
        self._init_remote_relay(config, ui_bus)
        config, ui_bus, llm, agent = self._build_core(config, ui_bus)
        skills_service = self._init_skills(config, agent, ui_bus)
        mcp_manager = self._attach_mcp_if_configured(config, agent, ui_bus)
        current_session_id, session_exit_time, sessions_dir = self._restore_session(config, agent, ui_bus)

        app_ctx = AppContext(
            config=config,
            llm=llm,
            agent=agent,
            ui_bus=ui_bus,
            ui_interactor=None,
            mcp_manager=mcp_manager,
            skills_service=skills_service,
            current_session_id=current_session_id,
            session_exit_time=session_exit_time,
            sessions_dir=sessions_dir,
        )
        self._run_lifecycle_hooks(
            agent,
            HookPoint.RUNNER_STARTUP,
            RunnerStartupContext(
                hook_point=HookPoint.RUNNER_STARTUP,
                metadata={"ui_bus": ui_bus},
            ),
        )
        self._run_lifecycle_hooks(
            agent,
            HookPoint.SESSION_START,
            SessionStartContext(
                hook_point=HookPoint.SESSION_START,
                session_id=current_session_id,
                metadata={"ui_bus": ui_bus},
            ),
        )
        return app_ctx

    def _build_core(
        self,
        config: Config,
        ui_bus: UIEventBus,
    ) -> tuple[Config, UIEventBus, LLM, Agent]:
        """Build config + ui bus + llm + agent, with runtime hooks initialized."""
        if self.options.model:
            config.model = self.options.model

        llm = self.dependencies.create_llm(config)
        llm.ui_bus = ui_bus
        tool_backend = self.dependencies.create_tool_backend(config, ui_bus)
        # If relay server was started, prefer remote backend when a peer is available
        if self._relay_server is not None:
            tool_backend = RemoteRelayToolBackend(relay_server=self._relay_server)
        tools = self.dependencies.load_tools(tool_backend)
        agent = self.dependencies.create_agent(llm, tools, config)
        setattr(agent, "runtime_config", config)
        setattr(agent, "current_session_id", None)
        setattr(agent, "session_fingerprint", get_session_fingerprint(config, agent))
        agent.context._ui_bus = ui_bus

        self._register_hooks(agent, config)
        self._wire_agent_tool_parent(agent)
        return config, ui_bus, llm, agent

    def _init_remote_relay(self, config: Config, ui_bus: UIEventBus) -> None:
        """Initialize remote relay server if enabled and host_mode."""
        try:
            relay = self.dependencies.create_remote_relay_server(config)
        except Exception as e:
            ui_bus.warning(
                f"Remote relay initialization failed: {e}", kind=UIEventKind.REMOTE
            )
            return
        if relay is None:
            return
        try:
            relay.start()
            self._relay_server = relay
        except Exception as e:
            ui_bus.warning(
                f"Remote relay server failed to start: {e}", kind=UIEventKind.REMOTE
            )
            return

        try:
            http_service = self.dependencies.create_remote_http_service(config, relay, ui_bus)
        except Exception as e:
            relay.stop()
            self._relay_server = None
            ui_bus.warning(
                f"Remote relay HTTP service initialization failed: {e}", kind=UIEventKind.REMOTE
            )
            return

        if http_service is not None:
            try:
                http_service.start()
                self._relay_http_service = http_service
            except Exception as e:
                relay.stop()
                self._relay_server = None
                self._relay_http_service = None
                ui_bus.warning(
                    f"Remote relay HTTP service failed to start: {e}", kind=UIEventKind.REMOTE
                )
                return

        ui_bus.success(
            "Remote relay server started.",
            kind=UIEventKind.REMOTE,
            bind=getattr(config.remote_exec, "relay_bind", None),
            base_url=self._relay_http_service.base_url if self._relay_http_service else None,
        )

    def _register_hooks(self, agent: Agent, config: Config) -> None:
        """Register hooks discovered via decorator mechanism."""
        specs = discover_hook_specs()
        hooks = instantiate_hooks(specs, config)
        for hook_point, hook in hooks:
            agent.register_hook(hook_point, hook)

    @staticmethod
    def _wire_agent_tool_parent(agent: Agent) -> None:
        """Inject parent agent into the nested agent tool if present."""
        for tool in agent.tools:
            if tool.name == "agent":
                tool._parent_agent = agent

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
        setattr(agent, "mcp_manager", mcp_manager)
        return mcp_manager

    def _init_skills(self, config: Config, agent: Agent, ui_bus: UIEventBus) -> SkillsService:
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
        setattr(agent, "skills_service", skills_service)
        setattr(agent, "skills_catalog", reload_result.catalog)

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
            ui_bus.warning(f"Skill not found and skipped: {name}", kind=UIEventKind.SYSTEM)
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
        """Restore requested/latest session and return session runtime metadata."""
        current_session_id = None
        session_exit_time = None
        sessions_dir = Path(config.session_dir) if config.session_dir else None
        current_fingerprint = get_session_fingerprint(config, agent)

        session_store = self.dependencies.create_session_store(sessions_dir)
        if self.options.resume_session_id:
            loaded = session_store.load(self.options.resume_session_id)
            if loaded:
                if loaded.fingerprint != current_fingerprint:
                    ui_bus.warning(
                        f"Session '{self.options.resume_session_id}' belongs to fingerprint '{loaded.fingerprint}', current fingerprint is '{current_fingerprint}'.",
                        kind=UIEventKind.SESSION,
                    )
                apply_session_runtime_state(loaded, config, agent)
                setattr(agent, "session_fingerprint", loaded.fingerprint)
                current_session_id = self.options.resume_session_id
                setattr(agent, "current_session_id", current_session_id)
                session_exit_time = session_store.get_exit_time(loaded.messages)
                ui_bus.success(
                    f"Resumed session: {self.options.resume_session_id}",
                    kind=UIEventKind.SESSION,
                )
            else:
                ui_bus.error(
                    f"Session '{self.options.resume_session_id}' not found.",
                    kind=UIEventKind.SESSION,
                )
        elif self.options.auto_resume_latest:
            latest = session_store.get_latest(fingerprint=current_fingerprint)
            if latest:
                loaded = session_store.load(latest.id)
                if loaded:
                    apply_session_runtime_state(loaded, config, agent)
                    setattr(agent, "session_fingerprint", loaded.fingerprint)
                    current_session_id = latest.id
                    setattr(agent, "current_session_id", current_session_id)
                    session_exit_time = session_store.get_exit_time(loaded.messages)
                    ui_bus.info(
                        f"Auto-resumed latest session: {latest.id} ({latest.saved_at})",
                        kind=UIEventKind.SESSION,
                    )
                    if latest.preview:
                        ui_bus.info(
                            f"  Preview: {latest.preview}...",
                            kind=UIEventKind.SESSION,
                        )
        else:
            restore_config_runtime_defaults(config, agent)

        return current_session_id, session_exit_time, sessions_dir

    def cleanup(self, agent: Agent | None = None) -> None:
        """Clean up resources (MCP connections, remote relay, etc.)."""
        if agent is not None:
            self._run_lifecycle_hooks(
                agent,
                HookPoint.RUNNER_SHUTDOWN,
                RunnerShutdownContext(hook_point=HookPoint.RUNNER_SHUTDOWN),
            )
        if self._relay_http_service is not None:
            artifact_provider = getattr(self._relay_http_service, "artifact_provider", None)
            build_dir = getattr(artifact_provider, "_build_dir", None) if artifact_provider is not None else None
            self._relay_http_service.stop()
            self._relay_http_service = None
            if isinstance(build_dir, Path):
                shutil.rmtree(build_dir, ignore_errors=True)
        if self._relay_server is not None:
            # best-effort cleanup for any connected peers
            for peer in self._relay_server.registry.list_online():
                try:
                    self._relay_server.request_cleanup(peer.peer_id, timeout_sec=5)
                except Exception:
                    pass
            self._relay_server.stop()
            self._relay_server = None
        if self._mcp_manager:
            self._mcp_manager.disconnect_all()
            self._mcp_manager.stop()
            self._mcp_manager = None

    @staticmethod
    def _run_lifecycle_hooks(
        agent: Agent,
        hook_point: HookPoint,
        context: "RunnerStartupContext | RunnerShutdownContext | SessionStartContext | SessionSaveContext",
    ) -> None:
        """Run hooks for a lifecycle event without mutating control flow."""
        # Guards are informational for lifecycle hooks; log but don't block
        for decision in agent.hook_registry.run_guards(hook_point, context):
            if not decision.allowed:
                # Lifecycle guards should not block startup/shutdown
                break
        agent.hook_registry.run_transforms(hook_point, context)
        agent.hook_registry.run_observers(hook_point, context)

    def _init_mcp(self, mcp_servers: list, agent: Agent, ui_bus: UIEventBus) -> MCPManager:
        """Initialize MCP manager and connect to servers."""
        manager = self.dependencies.create_mcp_manager(ui_bus)
        manager.start()

        enabled_servers = [s for s in mcp_servers if getattr(s, "enabled", True)]
        for server_config in enabled_servers:
            success = manager.connect_server(server_config)
            if not success:
                ui_bus.warning(
                    f"Warning: Failed to connect to MCP server '{server_config.name}'",
                    kind=UIEventKind.MCP,
                )

        if manager.tools:
            agent.add_tools(manager.tools)
            ui_bus.success(
                f"Loaded {len(manager.tools)} MCP tools from {len(enabled_servers)} enabled server(s)",
                kind=UIEventKind.MCP,
            )

        self._mcp_manager = manager
        return manager
