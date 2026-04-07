import sys
from pathlib import Path

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.tools.registry import ALL_TOOLS
from reuleauxcoder.interfaces.cli.args import parse_args
from reuleauxcoder.interfaces.cli.render import console, CLIRenderer
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.interfaces.events import AgentEventBridge, UIEventBus, UIEventKind
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.llm.client import LLM
from reuleauxcoder.services.sessions.manager import load_session


def _init_mcp(mcp_servers, agent: Agent, ui_bus: UIEventBus):
    manager = MCPManager(ui_bus=ui_bus)
    manager.start()

    for server_config in mcp_servers:
        success = manager.connect_server(server_config)
        if not success:
            ui_bus.warning(
                f"Warning: Failed to connect to MCP server '{server_config.name}'",
                kind=UIEventKind.MCP,
            )

    if manager.tools:
        agent.add_tools(manager.tools)
        ui_bus.success(
            f"Loaded {len(manager.tools)} MCP tools from {len(mcp_servers)} server(s)",
            kind=UIEventKind.MCP,
        )

    return manager


def _cleanup_mcp(manager: MCPManager):
    manager.disconnect_all()
    manager.stop()


def _run_once(agent: Agent, prompt: str, ui_bus: UIEventBus):
    renderer = CLIRenderer()
    bridge = AgentEventBridge(ui_bus)
    agent.add_event_handler(bridge.on_agent_event)
    ui_bus.subscribe(renderer.on_ui_event)

    agent.chat(prompt)


def main():
    args = parse_args()
    config_path = Path(args.config) if args.config else None
    config = ConfigLoader.from_path(config_path)
    ui_bus = UIEventBus()
    renderer = CLIRenderer()
    ui_bus.subscribe(renderer.on_ui_event)

    if args.model:
        config.model = args.model

    if not config.api_key:
        ui_bus.error("No API key found in config.yaml.")
        sys.exit(1)

    llm = LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    agent = Agent(
        llm=llm, tools=list(ALL_TOOLS), max_context_tokens=config.max_context_tokens
    )
    for tool in agent.tools:
        if tool.name == "agent":
            tool._parent_agent = agent

    bridge = AgentEventBridge(ui_bus)
    agent.add_event_handler(bridge.on_agent_event)

    mcp_manager = None
    if config.mcp_servers:
        mcp_manager = _init_mcp(config.mcp_servers, agent, ui_bus)

    try:
        if args.resume:
            loaded = load_session(args.resume)
            if loaded:
                agent.state.messages, _loaded_model = loaded
                ui_bus.success(f"Resumed session: {args.resume}", kind=UIEventKind.SESSION)
            else:
                ui_bus.error(f"Session '{args.resume}' not found.", kind=UIEventKind.SESSION)
                sys.exit(1)

        if args.prompt:
            _run_once(agent, args.prompt, ui_bus)
            return

        run_repl(agent, config, ui_bus)
    finally:
        if mcp_manager:
            _cleanup_mcp(mcp_manager)