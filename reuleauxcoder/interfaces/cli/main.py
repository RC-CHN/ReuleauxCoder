"""CLI entry point - thin wrapper around the shared entrypoint.

This module handles CLI-specific concerns:
- Argument parsing
- One-shot prompt mode
- REPL loop
"""

import sys
import time
from pathlib import Path

from reuleauxcoder.interfaces.cli.approval import CLIApprovalProvider
from reuleauxcoder.interfaces.cli.args import parse_args
from reuleauxcoder.interfaces.cli.registration import create_cli_registration
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.interfaces.entrypoint import AppRunner, AppOptions
from reuleauxcoder.interfaces.events import AgentEventBridge
from reuleauxcoder.interfaces.ui_registry import UIRegistry


def _run_once(agent, prompt: str):
    """Run a single prompt and exit."""
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
        server_mode=args.server,
    )

    # Initialize application using shared entrypoint
    runner = AppRunner(options)
    ctx = runner.initialize()

    ui_registry = UIRegistry([create_cli_registration(ctx.ui_bus)])
    cli_ui = ui_registry.require("cli")

    renderer = CLIRenderer(view_registry=cli_ui.view_registry)
    ctx.ui_bus.subscribe(renderer.on_ui_event)

    if args.server:
        ctx.ui_bus.info("Remote relay host mode active. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            renderer.close()
            runner.cleanup()
        return

    ctx.ui_interactor = cli_ui.interactor
    setattr(ctx.agent, "ui_interactor", cli_ui.interactor)
    ctx.agent.approval_provider = CLIApprovalProvider(cli_ui.interactor)

    # Add CLI renderer and bridge agent events onto the UI bus
    bridge = AgentEventBridge(ctx.ui_bus)
    ctx.agent.add_event_handler(bridge.on_agent_event)

    # Check for API key
    if not ctx.config.api_key:
        ctx.ui_bus.error("No API key found in config.yaml.")
        renderer.close()
        sys.exit(1)

    try:
        # One-shot mode
        if args.prompt:
            _run_once(ctx.agent, args.prompt)
            return

        # Interactive REPL mode
        run_repl(
            ctx.agent,
            ctx.config,
            ctx.ui_bus,
            cli_ui.profile,
            ctx.current_session_id,
            ctx.sessions_dir,
            ctx.session_exit_time,
            ctx.skills_service,
        )
    finally:
        renderer.close()
        runner.cleanup()
