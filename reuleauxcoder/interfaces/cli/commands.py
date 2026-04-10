"""CLI command handlers."""

from pathlib import Path

from reuleauxcoder.app.commands import CommandContext, dispatch_command, parse_command
from reuleauxcoder.domain.context.manager import estimate_tokens
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.render import show_help
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


def handle_command(
    user_input: str,
    agent,
    config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    sessions_dir: Path | None = None,
):
    if user_input.lower() in ("/quit", "/exit"):
        if agent.messages:
            sid = SessionStore(sessions_dir).save(
                agent.messages,
                config.model,
                current_session_id,
                is_exit=True,
                total_prompt_tokens=agent.state.total_prompt_tokens,
                total_completion_tokens=agent.state.total_completion_tokens,
            )
            ui_bus.info(f"Session auto-saved: {sid}", kind=UIEventKind.SESSION)
        return {"action": "exit", "session_id": current_session_id}

    if user_input == "/help":
        show_help()
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/reset":
        agent.reset()
        ui_bus.warning("Conversation reset (in-memory only, does not delete saved sessions).")
        return {"action": "continue", "session_id": current_session_id}

    parsed_command = parse_command(user_input, current_session_id=current_session_id)
    if parsed_command is not None:
        result = dispatch_command(
            parsed_command,
            CommandContext(
                agent=agent,
                config=config,
                ui_bus=ui_bus,
                ui_interactor=getattr(agent, "ui_interactor", None),
                sessions_dir=sessions_dir,
            ),
        )
        return {
            "action": result.action,
            "session_id": result.session_id if result.session_id is not None else current_session_id,
            "session_exit_time": result.session_exit_time,
        }

    if user_input == "/compact":
        before = estimate_tokens(agent.messages)
        compressed = agent.context.maybe_compress(agent.messages, agent.llm)
        after = estimate_tokens(agent.messages)
        if compressed:
            ui_bus.success(
                f"Compressed: {before} → {after} tokens ({len(agent.messages)} messages)"
            )
        else:
            ui_bus.info(
                f"Nothing to compress ({before} tokens, {len(agent.messages)} messages)"
            )
        return {"action": "continue", "session_id": current_session_id}

    return {"action": "chat", "session_id": current_session_id}
