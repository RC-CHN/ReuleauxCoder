"""Parser for shared slash commands."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import (
    Command,
    ListSessionsCommand,
    NewSessionCommand,
    ResumeSessionCommand,
    SaveSessionCommand,
    SetApprovalRuleCommand,
    ShowApprovalCommand,
    ShowModelCommand,
    ShowTokensCommand,
    SwitchModelCommand,
)


def parse_command(user_input: str, *, current_session_id: str | None = None) -> Command | None:
    """Parse a slash command into a structured command object."""
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

    return None
