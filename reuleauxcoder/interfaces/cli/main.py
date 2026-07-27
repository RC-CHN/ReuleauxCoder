"""CLI entry point - thin wrapper around the shared entrypoint.

This module handles CLI-specific concerns:
- Argument parsing
- One-shot prompt mode
- REPL loop
"""

import signal
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.text import Text

from reuleauxcoder.interfaces.approval import make_approval_handler
from reuleauxcoder.interfaces.cli.args import parse_args
from reuleauxcoder.interfaces.cli.registration import create_cli_registration
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.presentation import PresentationPolicy
from reuleauxcoder.interfaces.cli.output import CLIOutputCoordinator
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.interfaces.cli.theme import DEFAULT_CLI_THEME
from reuleauxcoder.interfaces.entrypoint import AppRunner, AppOptions
from reuleauxcoder.interfaces.events import AgentEventBridge
from reuleauxcoder.interfaces.ui_registry import UIRegistry
from reuleauxcoder.presentation.semantics import DisplayTone
from reuleauxcoder.services.config.loader import ExampleConfigError


def _install_sigint_handler(agent):
    """Install a SIGINT handler that sets the agent's cooperative-stop flag.

    The handler sets ``agent.request_stop()`` so that the agent loop
    exits cleanly at its next check point, *then* re-raises
    ``KeyboardInterrupt`` to interrupt the currently-blocked operation
    (streaming, subprocess, etc.) immediately.
    """

    def handler(signum, frame):
        try:
            agent.request_stop()
        except Exception:
            pass  # best-effort; the agent reference may be stale
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handler)


def _run_once(agent, prompt: str, output: CLIOutputCoordinator):
    """Run a single prompt and exit."""
    agent.chat(prompt)
    output.drain()


def _terminal_status(
    message: str,
    *,
    tone: DisplayTone = DisplayTone.NEUTRAL,
    console_override: Console | None = None,
) -> None:
    """Render styled status while no interactive renderer owns the terminal."""
    target = console_override or Console(
        file=sys.stderr,
        highlight=False,
        soft_wrap=True,
    )
    line = Text()
    line.append("rcoder", style=DEFAULT_CLI_THEME.style(DisplayTone.ACCENT))
    line.append(": ", style=DEFAULT_CLI_THEME.style(DisplayTone.MUTED))
    line.append(message, style=DEFAULT_CLI_THEME.style(tone))
    target.print(line, soft_wrap=True)


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
    startup_progress_active = True

    def report_startup(message: str) -> None:
        if startup_progress_active:
            _terminal_status(message, tone=DisplayTone.NEUTRAL)

    startup_progress = (
        report_startup
        if not getattr(args, "prompt", None)
        and not args.server
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        else None
    )

    # Initialize application using shared entrypoint
    runner = None
    try:
        runner = AppRunner(options, startup_progress=startup_progress)
        ctx = runner.initialize()
        startup_progress_active = False
    except ExampleConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        if runner is not None:
            try:
                runner.cleanup()
            except Exception:
                pass
        print("Interrupted.", file=sys.stderr)
        return 130

    ui_registry = UIRegistry([create_cli_registration(ctx.ui_bus)])
    cli_ui = ui_registry.require("cli")

    remote_exec = getattr(ctx.config, "remote_exec", None)
    is_host_mode = remote_exec and remote_exec.enabled and remote_exec.host_mode
    use_mini_tui = (
        not args.prompt
        and not args.server
        and not is_host_mode
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )

    if use_mini_tui:
        _terminal_status("Preparing terminal UI...", tone=DisplayTone.ACCENT)
        from reuleauxcoder.app.runtime.approval import (
            build_runtime_approval_provider,
        )
        from reuleauxcoder.app.runtime.interactions import InteractionCoordinator
        from reuleauxcoder.interfaces.cli.mini_tui import (
            MiniTUIApplication,
            MiniTUIEventAdapter,
            MiniTUIInteractor,
        )

        event_adapter = MiniTUIEventAdapter(root_agent_id=ctx.agent.agent_id)
        if ctx.current_session_id and ctx.agent.messages:
            from reuleauxcoder.domain.session.models import Session

            replay_session = Session(
                id=ctx.current_session_id,
                model=ctx.config.model,
                saved_at="",
                messages=list(ctx.agent.messages),
            )
            event_adapter.append_restored_conversation(
                replay_session.get_recent_conversation(max_user_turns=3)
            )
        mini_interactor = MiniTUIInteractor(ctx.ui_bus)
        interaction_coordinator = InteractionCoordinator(mini_interactor)
        ctx.ui_interactor = interaction_coordinator
        ctx.agent.ui_interactor = interaction_coordinator
        ctx.agent.approval_provider = build_runtime_approval_provider(
            ctx.agent, make_approval_handler(interaction_coordinator)
        )
        bridge = AgentEventBridge(ctx.ui_bus)
        ctx.agent.add_event_handler(bridge.on_agent_event)
        startup_events = ctx.ui_bus.history_snapshot()
        ctx.ui_bus.subscribe(event_adapter.on_ui_event, replay_history=False)
        from reuleauxcoder.domain.runtime.events import (
            PlanUpdated,
            ProgressReported,
            RuntimeEvent,
        )

        plan = ctx.agent.plan_controller.state
        if plan.revision:
            ctx.ui_bus.emit_runtime(
                RuntimeEvent(
                    payload=PlanUpdated(
                        revision=plan.revision,
                        items=tuple(
                            {
                                "step": item.step,
                                "active_form": item.active_form,
                                "status": item.status,
                            }
                            for item in plan.items
                        ),
                        explanation=plan.explanation,
                    ),
                    agent_id=ctx.agent.agent_id,
                    session_generation=ctx.agent.session_generation,
                    session_id=ctx.current_session_id,
                )
            )
        progress = ctx.agent.plan_controller.progress
        if progress.revision:
            ctx.ui_bus.emit_runtime(
                RuntimeEvent(
                    payload=ProgressReported(
                        revision=progress.revision,
                        phase=progress.phase,
                        summary=progress.summary,
                        next=progress.next,
                    ),
                    agent_id=ctx.agent.agent_id,
                    session_generation=ctx.agent.session_generation,
                    session_id=ctx.current_session_id,
                )
            )
        manager = getattr(ctx.agent, "_subagent_manager", None)
        if manager is not None:
            from reuleauxcoder.domain.agent.events import AgentEvent

            for job in manager.list_jobs():
                ctx.agent._emit_event(
                    AgentEvent.subagent_completed(
                        job_id=job.id,
                        mode=job.mode,
                        task=job.task,
                        status=job.status,
                        result=job.result,
                        error=job.error,
                    )
                )

        if not ctx.config.api_key:
            ctx.ui_bus.error("No API key found in config.yaml.")
            interaction_coordinator.shutdown()
            runner.cleanup()
            sys.exit(1)

        application = MiniTUIApplication(
            agent=ctx.agent,
            config=ctx.config,
            ui_bus=ctx.ui_bus,
            ui_profile=cli_ui.profile,
            action_registry=ctx.action_registry,
            interactor=mini_interactor,
            event_adapter=event_adapter,
            current_session_id=ctx.current_session_id,
            sessions_dir=ctx.sessions_dir,
            session_exit_time=ctx.session_exit_time,
            skills_service=ctx.skills_service,
            startup_events=startup_events,
            exit_progress=lambda message: _terminal_status(
                message, tone=DisplayTone.MUTED
            ),
        )
        _terminal_status("Starting terminal UI...", tone=DisplayTone.ACCENT)
        try:
            application.run()
        finally:
            _terminal_status("Terminal UI closed.", tone=DisplayTone.MUTED)
            if application.exit_session_saved:
                _terminal_status(
                    f"Session saved: {application.saved_session_id or ctx.current_session_id}.",
                    tone=DisplayTone.SUCCESS,
                )
            elif not ctx.config.session_auto_save:
                _terminal_status(
                    "Session autosave is disabled; no exit snapshot written.",
                    tone=DisplayTone.WARNING,
                )
            elif not ctx.agent.messages:
                _terminal_status("No conversation to save.", tone=DisplayTone.MUTED)
            else:
                _terminal_status(
                    "No exit snapshot was written.", tone=DisplayTone.WARNING
                )
            try:
                interaction_coordinator.shutdown()
                runner.cleanup(
                    progress=lambda message: _terminal_status(
                        message, tone=DisplayTone.MUTED
                    )
                )
            except Exception as error:
                _terminal_status(
                    "Background service cleanup failed: "
                    f"{type(error).__name__}: {error}",
                    tone=DisplayTone.ERROR,
                )
                raise
            else:
                _terminal_status("Exited.", tone=DisplayTone.SUCCESS)
        return

    renderer = CLIRenderer(
        view_registry=cli_ui.view_registry,
        policy=PresentationPolicy.from_ui_config(ctx.config.ui),
        root_agent_id=ctx.agent.agent_id,
    )
    output = CLIOutputCoordinator(renderer)
    group_startup = not args.prompt and not args.server and not is_host_mode
    startup_events = (
        tuple(
            event
            for event in ctx.ui_bus.history_snapshot()
            if event.payload is None
            and renderer.policy.should_render_notification(event.level.value)
        )
        if group_startup
        else ()
    )
    ctx.ui_bus.subscribe(output.on_ui_event, replay_history=not group_startup)

    if args.server or is_host_mode:
        ctx.ui_bus.info("Remote relay host mode active. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(0.1)
                output.drain()
        except KeyboardInterrupt:
            pass
        finally:
            output.close()
            runner.cleanup()
        return

    ctx.ui_interactor = cli_ui.interactor
    ctx.agent.ui_interactor = cli_ui.interactor
    from reuleauxcoder.app.runtime.approval import build_runtime_approval_provider

    ctx.agent.approval_provider = build_runtime_approval_provider(
        ctx.agent, make_approval_handler(cli_ui.interactor)
    )

    # Add CLI renderer and bridge agent events onto the UI bus
    bridge = AgentEventBridge(ctx.ui_bus)
    ctx.agent.add_event_handler(bridge.on_agent_event)

    # Check for API key
    if not ctx.config.api_key:
        ctx.ui_bus.error("No API key found in config.yaml.")
        output.close()
        sys.exit(1)

    try:
        # One-shot mode
        if args.prompt:
            _run_once(ctx.agent, args.prompt, output)
            return

        # Interactive REPL mode
        _install_sigint_handler(ctx.agent)
        if ctx.action_registry is None or ctx.current_session_id is None:
            raise RuntimeError("Interactive CLI runtime is missing command/session state")
        run_repl(
            ctx.agent,
            ctx.config,
            ctx.ui_bus,
            cli_ui.profile,
            ctx.action_registry,
            ctx.current_session_id,
            ctx.sessions_dir,
            ctx.session_exit_time,
            ctx.skills_service,
            output,
            cli_ui.interactor,
            startup_events,
        )
    finally:
        output.close()
        runner.cleanup()
