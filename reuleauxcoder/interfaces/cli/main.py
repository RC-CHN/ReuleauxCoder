"""CLI entry point - thin wrapper around the shared entrypoint.

This module handles CLI-specific concerns:
- Argument parsing
- One-shot prompt mode
- REPL loop
"""

import sys
from pathlib import Path

from reuleauxcoder.interfaces.cli.args import parse_args
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.interfaces.entrypoint import AppRunner, AppOptions
from reuleauxcoder.interfaces.events import AgentEventBridge, UIEventBus


def _run_once(agent, prompt: str, ui_bus: UIEventBus):
    """Run a single prompt and exit."""
    renderer = CLIRenderer()
    bridge = AgentEventBridge(ui_bus)
    agent.add_event_handler(bridge.on_agent_event)
    ui_bus.subscribe(renderer.on_ui_event)
    agent.chat(prompt)


def main():
    """CLI main entry point."""
    args = parse_args()
    
    # Build options from CLI args
    options = AppOptions(
        config_path=Path(args.config) if args.config else None,
        model=args.model,
        resume_session_id=args.resume,
        auto_resume_latest=True,
    )
    
    # Initialize application using shared entrypoint
    runner = AppRunner(options)
    ctx = runner.initialize()
    
    # Add CLI renderer
    renderer = CLIRenderer()
    ctx.ui_bus.subscribe(renderer.on_ui_event)
    
    # Check for API key
    if not ctx.config.api_key:
        ctx.ui_bus.error("No API key found in config.yaml.")
        sys.exit(1)
    
    try:
        # One-shot mode
        if args.prompt:
            _run_once(ctx.agent, args.prompt, ctx.ui_bus)
            return
        
        # Interactive REPL mode
        run_repl(
            ctx.agent,
            ctx.config,
            ctx.ui_bus,
            ctx.current_session_id,
            ctx.sessions_dir,
            ctx.session_exit_time,
        )
    finally:
        runner.cleanup()