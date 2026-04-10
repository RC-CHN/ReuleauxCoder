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

from dataclasses import dataclass, field
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
    
    def __init__(self, options: AppOptions | None = None):
        self.options = options or AppOptions()
        self._mcp_manager: MCPManager | None = None
    
    def initialize(self) -> AppContext:
        """Initialize all application components and return context.
        
        Returns:
            AppContext with all initialized components.
        """
        # Load configuration
        config = ConfigLoader.from_path(self.options.config_path)
        ui_bus = UIEventBus()
        
        # Override model if specified
        if self.options.model:
            config.model = self.options.model
        
        # Initialize LLM
        llm = LLM(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        
        # Initialize Agent
        agent = Agent(
            llm=llm, 
            tools=list(ALL_TOOLS), 
            max_context_tokens=config.max_context_tokens
        )
        
        # Register hooks
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
        
        # Set parent agent for agent tool
        for tool in agent.tools:
            if tool.name == "agent":
                tool._parent_agent = agent
        
        # Initialize MCP if configured
        mcp_manager = None
        if config.mcp_servers:
            mcp_manager = self._init_mcp(config.mcp_servers, agent, ui_bus)
        setattr(agent, "mcp_manager", mcp_manager)
        
        # Session management
        current_session_id = None
        session_exit_time = None
        sessions_dir = Path(config.session_dir) if config.session_dir else None
        
        session_store = SessionStore(sessions_dir)
        if self.options.resume_session_id:
            loaded = session_store.load(self.options.resume_session_id)
            if loaded:
                (
                    agent.state.messages,
                    _loaded_model,
                    agent.state.total_prompt_tokens,
                    agent.state.total_completion_tokens,
                ) = loaded
                current_session_id = self.options.resume_session_id
                session_exit_time = session_store.get_exit_time(agent.state.messages)
                ui_bus.success(
                    f"Resumed session: {self.options.resume_session_id}",
                    kind=UIEventKind.SESSION
                )
            else:
                ui_bus.error(
                    f"Session '{self.options.resume_session_id}' not found.",
                    kind=UIEventKind.SESSION
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
                    ) = loaded
                    current_session_id = latest.id
                    session_exit_time = session_store.get_exit_time(agent.state.messages)
                    ui_bus.info(
                        f"Auto-resumed latest session: {latest.id} ({latest.saved_at})",
                        kind=UIEventKind.SESSION,
                    )
                    if latest.preview:
                        ui_bus.info(f"  Preview: {latest.preview}...", kind=UIEventKind.SESSION)
        
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
    
    def cleanup(self) -> None:
        """Clean up resources (MCP connections, etc.)."""
        if self._mcp_manager:
            self._mcp_manager.disconnect_all()
            self._mcp_manager.stop()
            self._mcp_manager = None
    
    def _init_mcp(
        self, 
        mcp_servers: list, 
        agent: Agent, 
        ui_bus: UIEventBus
    ) -> MCPManager:
        """Initialize MCP manager and connect to servers."""
        manager = MCPManager(ui_bus=ui_bus)
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