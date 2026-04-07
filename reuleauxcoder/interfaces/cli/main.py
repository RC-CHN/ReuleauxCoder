import sys
from pathlib import Path

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.tools.registry import ALL_TOOLS
from reuleauxcoder.interfaces.cli.args import parse_args
from reuleauxcoder.interfaces.cli.render import console, CLIRenderer
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.llm.client import LLM
from reuleauxcoder.services.sessions.manager import load_session


def _init_mcp(mcp_servers, agent: Agent):
    manager = MCPManager()
    manager.start()

    for server_config in mcp_servers:
        success = manager.connect_server(server_config)
        if not success:
            console.print(
                f"[yellow]Warning: Failed to connect to MCP server '{server_config.name}'[/yellow]"
            )

    if manager.tools:
        agent.add_tools(manager.tools)
        console.print(
            f"[green]Loaded {len(manager.tools)} MCP tools from {len(mcp_servers)} server(s)[/green]"
        )

    return manager


def _cleanup_mcp(manager: MCPManager):
    manager.disconnect_all()
    manager.stop()


def _run_once(agent: Agent, prompt: str):
    renderer = CLIRenderer()
    agent.add_event_handler(renderer.on_event)

    agent.chat(prompt)


def main():
    args = parse_args()
    config_path = Path(args.config) if args.config else None
    config = ConfigLoader.from_path(config_path)

    if args.model:
        config.model = args.model

    if not config.api_key:
        console.print("[red bold]No API key found in config.yaml.[/]")
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

    mcp_manager = None
    if config.mcp_servers:
        mcp_manager = _init_mcp(config.mcp_servers, agent)

    try:
        if args.resume:
            loaded = load_session(args.resume)
            if loaded:
                agent.state.messages, _loaded_model = loaded
                console.print(f"[green]Resumed session: {args.resume}[/green]")
            else:
                console.print(f"[red]Session '{args.resume}' not found.[/red]")
                sys.exit(1)

        if args.prompt:
            _run_once(agent, args.prompt)
            return

        run_repl(agent, config)
    finally:
        if mcp_manager:
            _cleanup_mcp(mcp_manager)