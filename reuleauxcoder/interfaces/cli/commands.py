"""CLI command handlers."""

from reuleauxcoder.domain.context.manager import estimate_tokens
from reuleauxcoder.services.sessions.manager import list_sessions, save_session
from reuleauxcoder.interfaces.cli.render import show_help
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


def handle_command(
    user_input: str,
    agent,
    config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
):
    if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
        if agent.messages:
            sid = save_session(agent.messages, config.model, current_session_id)
            ui_bus.info(f"Session auto-saved: {sid}", kind=UIEventKind.SESSION)
        return {"action": "exit", "session_id": current_session_id}

    if user_input == "/help":
        show_help()
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/reset":
        agent.reset()
        ui_bus.warning("Conversation reset.")
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/tokens":
        p = agent.llm.total_prompt_tokens
        c = agent.llm.total_completion_tokens
        ui_bus.info(
            f"Tokens used this session: {p} prompt + {c} completion = {p + c} total"
        )
        return {"action": "continue", "session_id": current_session_id}

    if user_input.startswith("/model "):
        new_model = user_input[7:].strip()
        if new_model:
            agent.llm.model = new_model
            config.model = new_model
            ui_bus.success(f"Switched to {new_model}")
        return {"action": "continue", "session_id": current_session_id}

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

    if user_input == "/save":
        sid = save_session(agent.messages, config.model, current_session_id)
        current_session_id = sid
        ui_bus.success(f"Session saved: {sid}", kind=UIEventKind.SESSION)
        ui_bus.info(f"Resume with: rcoder -r {sid}", kind=UIEventKind.SESSION)
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/sessions":
        sessions = list_sessions()
        if not sessions:
            ui_bus.info("No saved sessions.", kind=UIEventKind.SESSION)
        else:
            for s in sessions:
                ui_bus.info(
                    f"  {s.id} ({s.model}, {s.saved_at}) {s.preview}",
                    kind=UIEventKind.SESSION,
                )
        return {"action": "continue", "session_id": current_session_id}

    return {"action": "chat", "session_id": current_session_id}
