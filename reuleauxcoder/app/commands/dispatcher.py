"""Shared command dispatcher."""

from __future__ import annotations

from reuleauxcoder.app.commands.handlers.approval import handle_set_approval_rule, handle_show_approval
from reuleauxcoder.app.commands.handlers.model import handle_show_model, handle_switch_model
from reuleauxcoder.app.commands.handlers.sessions import (
    handle_list_sessions,
    handle_new_session,
    handle_resume_session,
    handle_save_session,
)
from reuleauxcoder.app.commands.handlers.system import handle_show_tokens
from reuleauxcoder.app.commands.models import (
    Command,
    CommandContext,
    CommandResult,
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


def dispatch_command(command: Command, ctx: CommandContext) -> CommandResult:
    """Dispatch a structured command to its shared handler."""
    if isinstance(command, ShowModelCommand):
        return handle_show_model(ctx)
    if isinstance(command, SwitchModelCommand):
        return handle_switch_model(command, ctx)
    if isinstance(command, ListSessionsCommand):
        return handle_list_sessions(command, ctx)
    if isinstance(command, ResumeSessionCommand):
        return handle_resume_session(command, ctx)
    if isinstance(command, SaveSessionCommand):
        return handle_save_session(command, ctx)
    if isinstance(command, NewSessionCommand):
        return handle_new_session(command, ctx)
    if isinstance(command, ShowTokensCommand):
        return handle_show_tokens(command, ctx)
    if isinstance(command, ShowApprovalCommand):
        return handle_show_approval(command, ctx)
    if isinstance(command, SetApprovalRuleCommand):
        return handle_set_approval_rule(command, ctx)
    return CommandResult(action="continue")
