"""Parser for shared slash commands."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import (
    Command,
    CompactContextCommand,
    ExitCommand,
    ListSessionsCommand,
    NewSessionCommand,
    ResetConversationCommand,
    ResumeSessionCommand,
    SaveSessionCommand,
    SetApprovalRuleCommand,
    ShowApprovalCommand,
    ShowHelpCommand,
    ShowMCPServersCommand,
    ShowModelCommand,
    ShowTokensCommand,
    SwitchModelCommand,
    ToggleMCPServerCommand,
)


_FORCE_COMPACT_STRATEGIES = {"snip", "summarize", "collapse"}


def parse_command(user_input: str, *, current_session_id: str | None = None) -> Command | None:
    """Parse a slash command into a structured command object."""
    lowered = user_input.lower()
    if lowered in {"/quit", "/exit"}:
        return ExitCommand(current_session_id=current_session_id)

    if user_input == "/help":
        return ShowHelpCommand()

    if user_input == "/reset":
        return ResetConversationCommand()

    if user_input == "/compact":
        return CompactContextCommand()

    if lowered.startswith("/compact force "):
        strategy = lowered[len("/compact force ") :].strip()
        if strategy in _FORCE_COMPACT_STRATEGIES:
            return CompactContextCommand(force_strategy=strategy)
        return CompactContextCommand(force_strategy="")

    if user_input == "/model":
        return ShowModelCommand()

    if user_input.startswith("/model "):
        target = user_input[7:].strip()
        if target in {"", "ls", "list", "show"}:
            return ShowModelCommand()
        return SwitchModelCommand(profile_name=target)

    if user_input == "/sessions":
        return ListSessionsCommand()

    if user_input.startswith("/session "):
        return ResumeSessionCommand(target=user_input[len("/session ") :].strip())

    if user_input == "/save":
        return SaveSessionCommand(current_session_id=current_session_id)

    if user_input == "/new":
        return NewSessionCommand(current_session_id=current_session_id)

    if user_input == "/tokens":
        return ShowTokensCommand()

    if user_input == "/approval" or user_input == "/approval show":
        return ShowApprovalCommand()

    if user_input.startswith("/approval set "):
        spec = user_input[len("/approval set ") :].strip().split()
        if len(spec) >= 2:
            return SetApprovalRuleCommand(target=spec[0], action=spec[1])
        return SetApprovalRuleCommand(target="", action="")

    if user_input == "/mcp" or user_input == "/mcp show":
        return ShowMCPServersCommand()

    if user_input.startswith("/mcp enable "):
        return ToggleMCPServerCommand(
            server_name=user_input[len("/mcp enable ") :].strip(),
            enabled=True,
        )

    if user_input.startswith("/mcp disable "):
        return ToggleMCPServerCommand(
            server_name=user_input[len("/mcp disable ") :].strip(),
            enabled=False,
        )

    return None
