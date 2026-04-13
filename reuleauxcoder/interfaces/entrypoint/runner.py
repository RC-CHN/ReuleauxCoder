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
from pathlib import Path
from typing import Any, Callable

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.hooks import HookPoint
from reuleauxcoder.domain.hooks.builtin import ToolOutputTruncationHook, ToolPolicyGuardHook
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.tools.registry import ALL_TOOLS
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.interactions import UIInteractor
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.llm.client import LLM


def _default_load_config(path: Path | None):
    return ConfigLoader.from_path(path)


def _default_create_llm(config: Any) -> LLM:
    return LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )


def _default_load_tools() -> list[Any]:
    return list(ALL_TOOLS)


def _default_create_agent(llm: LLM, tools: list[Any], config: Any) -> Agent:
    return Agent(
        llm=llm,
        tools=tools,
        max_context_tokens=config.max_context_tokens,
        available_modes=getattr(config, "modes", {}) or {},
        active_mode=getattr(config, "active_mode", None),
    )


def _default_create_session_store(sessions_dir: Path | None) -> SessionStore:
    return SessionStore(sessions_dir)


def _default_create_mcp_manager(ui_bus: UIEventBus) -> MCPManager:
    return MCPManager(ui_bus=ui_bus)


@dataclass
class AppDependencies:
    """Lightweight dependency providers for AppRunner.

    Keep defaults production-safe while allowing tests/entrypoints to override
    any component construction without a heavy DI framework.
    """

    load_config: Callable[[Path | None], Any] = _default_load_config
    create_ui_bus: Callable[[], UIEventBus] = UIEventBus
    create_llm: Callable[[Any], LLM] = _default_create_llm
    load_tools: Callable[[], list[Any]] = _default_load_tools
    create_agent: Callable[[LLM, list[Any], Any], Agent] = _default_create_agent
    create_session_store: Callable[[Path | None], SessionStore] = _default_create_session_store
    create_mcp_manager: Callable[[UIEventBus], MCPManager] = _default_create_mcp_manager


@dataclass
class AppContext:
    """Context object containing all initialized application components."""

    config: Any
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

    def initialize(self) -> AppContext:
        """Initialize all application components and return context.

        Returns:
            AppContext with all initialized components.
        """
        config, ui_bus, llm, agent = self._build_core()
        mcp_manager = self._attach_mcp_if_configured(config, agent, ui_bus)
        current_session_id, session_exit_time, sessions_dir = self._restore_session(config, agent, ui_bus)

        return AppContext(
            config=config,
            llm=llm,
            agent=agent,
            ui_bus=ui_bus,
            ui_interactor=None,
            mcp_manager=mcp_manager,
            current_session_id=current_session_id,
            session_exit_time=session_exit_time,
            sessions_dir=sessions_dir,
        )

    def _build_core(self) -> tuple[Any, UIEventBus, LLM, Agent]:
        """Build config + ui bus + llm + agent, with runtime hooks initialized."""
        config = self.dependencies.load_config(self.options.config_path)
        ui_bus = self.dependencies.create_ui_bus()

        if self.options.model:
            config.model = self.options.model

        llm = self.dependencies.create_llm(config)
        tools = self.dependencies.load_tools()
        agent = self.dependencies.create_agent(llm, tools, config)
        agent.context._ui_bus = ui_bus

        self._register_hooks(agent, config)
        self._wire_agent_tool_parent(agent)
        return config, ui_bus, llm, agent

    def _register_hooks(self, agent: Agent, config: Any) -> None:
        """Register default runtime hooks on the agent."""
        agent.register_hook(
            HookPoint.BEFORE_TOOL_EXECUTE,
            ToolPolicyGuardHook(approval_config=config.approval, priority=100),
        )
        agent.register_hook(
            HookPoint.AFTER_TOOL_EXECUTE,
            ToolOutputTruncationHook(
                max_chars=config.tool_output_max_chars,
                max_lines=config.tool_output_max_lines,
                store_full_output=config.tool_output_store_full,
                store_dir=config.tool_output_store_dir,
                priority=0,
            ),
        )

    @staticmethod
    def _wire_agent_tool_parent(agent: Agent) -> None:
        """Inject parent agent into the nested agent tool if present."""
        for tool in agent.tools:
            if tool.name == "agent":
                tool._parent_agent = agent

    def _attach_mcp_if_configured(
        self,
        config: Any,
        agent: Agent,
        ui_bus: UIEventBus,
    ) -> MCPManager | None:
        """Initialize and attach MCP runtime if servers are configured."""
        mcp_manager = None
        if config.mcp_servers:
            mcp_manager = self._init_mcp(config.mcp_servers, agent, ui_bus)
        setattr(agent, "mcp_manager", mcp_manager)
        return mcp_manager

    def _restore_session(
        self,
        config: Any,
        agent: Agent,
        ui_bus: UIEventBus,
    ) -> tuple[str | None, str | None, Path | None]:
        """Restore requested/latest session and return session runtime metadata."""
        current_session_id = None
        session_exit_time = None
        sessions_dir = Path(config.session_dir) if config.session_dir else None

        session_store = self.dependencies.create_session_store(sessions_dir)
        if self.options.resume_session_id:
            loaded = session_store.load(self.options.resume_session_id)
            if loaded:
                (
                    agent.state.messages,
                    _loaded_model,
                    agent.state.total_prompt_tokens,
                    agent.state.total_completion_tokens,
                    loaded_mode,
                ) = loaded
                if loaded_mode and loaded_mode in getattr(agent, "available_modes", {}):
                    agent.active_mode = loaded_mode
                    config.active_mode = loaded_mode
                current_session_id = self.options.resume_session_id
                session_exit_time = session_store.get_exit_time(agent.state.messages)
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
            latest = session_store.get_latest()
            if latest:
                loaded = session_store.load(latest.id)
                if loaded:
                    (
                        agent.state.messages,
                        _loaded_model,
                        agent.state.total_prompt_tokens,
                        agent.state.total_completion_tokens,
                        loaded_mode,
                    ) = loaded
                    if loaded_mode and loaded_mode in getattr(agent, "available_modes", {}):
                        agent.active_mode = loaded_mode
                        config.active_mode = loaded_mode
                    current_session_id = latest.id
                    session_exit_time = session_store.get_exit_time(agent.state.messages)
                    ui_bus.info(
                        f"Auto-resumed latest session: {latest.id} ({latest.saved_at})",
                        kind=UIEventKind.SESSION,
                    )
                    if latest.preview:
                        ui_bus.info(
                            f"  Preview: {latest.preview}...",
                            kind=UIEventKind.SESSION,
                        )

        return current_session_id, session_exit_time, sessions_dir

    def cleanup(self) -> None:
        """Clean up resources (MCP connections, etc.)."""
        if self._mcp_manager:
            self._mcp_manager.disconnect_all()
            self._mcp_manager.stop()
            self._mcp_manager = None

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
