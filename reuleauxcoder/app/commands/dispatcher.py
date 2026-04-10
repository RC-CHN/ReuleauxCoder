"""Shared command dispatcher."""

from __future__ import annotations

from reuleauxcoder.app.commands.handlers.approval import handle_set_approval_rule, handle_show_approval
from reuleauxcoder.app.commands.handlers.mcp import handle_show_mcp_servers, handle_toggle_mcp_server
from reuleauxcoder.app.commands.handlers.model import handle_show_model, handle_switch_model
from reuleauxcoder.app.commands.handlers.sessions import (
    handle_list_sessions,
    handle_new_session,
    handle_resume_session,
    handle_save_session,
)
from reuleauxcoder.app.commands.handlers.system import (
    handle_compact_context,
    handle_exit,
    handle_reset_conversation,
    handle_show_help,
    handle_show_tokens,
)
from reuleauxcoder.app.commands.models import (
    Command,
    CommandContext,
    CommandResult,
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


def dispatch_command(command: Command, ctx: CommandContext) -> CommandResult:
    """Dispatch a structured command to its shared handler."""
    if isinstance(command, ShowHelpCommand):
        return handle_show_help(command, ctx)
    if isinstance(command, ExitCommand):
        return handle_exit(command, ctx)
    if isinstance(command, ResetConversationCommand):
        return handle_reset_conversation(command, ctx)
    if isinstance(command, CompactContextCommand):
        return handle_compact_context(command, ctx)
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
    if isinstance(command, ShowMCPServersCommand):
        return handle_show_mcp_servers(command, ctx)
    if isinstance(command, ToggleMCPServerCommand):
        return handle_toggle_mcp_server(command, ctx)
    return CommandResult(action="continue")
