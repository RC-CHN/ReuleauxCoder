"""Tool execution - handles tool calls."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from difflib import get_close_matches
from typing import TYPE_CHECKING, List, cast

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import ToolCall

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.approval import ApprovalRequest, ApprovalSectionKind
from reuleauxcoder.domain.approval_preview import (
    build_approval_preview,
    capture_approval_document,
    diff_approval_documents,
)
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    GuardDecision,
    HookPoint,
)
from reuleauxcoder.domain.workspace import WorkspaceError


_EXTERNAL_PATH_ARGUMENTS = {
    "edit_file": "file_path",
    "glob": "path",
    "grep": "path",
    "list_file": "path",
    "lsp": "filePath",
    "read_file": "file_path",
    "write_file": "file_path",
}
_EXTERNAL_MUTATION_TOOLS = frozenset({"edit_file", "write_file"})


def _external_workspace_target(tool, arguments: dict) -> str | None:
    """Detect an exact local file target outside the configured workspace."""
    tool_name = getattr(tool, "name", None)
    path_argument = (
        _EXTERNAL_PATH_ARGUMENTS.get(tool_name)
        if isinstance(tool_name, str)
        else None
    )
    if tool is None or path_argument is None:
        return None
    workspace = getattr(getattr(tool, "backend", None), "workspace", None)
    inspect_external = getattr(workspace, "external_path", None)
    grant_external = getattr(workspace, "grant_external_path", None)
    file_path = arguments.get(path_argument, ".")
    if not callable(inspect_external) or not callable(grant_external):
        return None
    if not isinstance(file_path, str) or not file_path:
        return None
    try:
        external = inspect_external(file_path)
    except (WorkspaceError, OSError, ValueError):
        return None
    return str(external) if external is not None else None


@contextmanager
def _workspace_access_scope(workspace, external_target: str | None) -> Iterator[None]:
    """Grant an approved exact path for the duration of one local operation."""
    grant_external = getattr(workspace, "grant_external_path", None)
    if external_target is None or not callable(grant_external):
        yield
        return
    access_scope = cast(
        AbstractContextManager[object], grant_external(external_target)
    )
    with access_scope:
        yield


@contextmanager
def _stream_handler_scope(
    backend,
    execution_context,
    handler,
) -> Iterator[None]:
    """Prefer backend-local execution state; preserve custom backend compatibility."""
    bind = getattr(backend, "stream_handler_scope", None)
    if callable(bind):
        scope = cast(AbstractContextManager[object], bind(handler))
        with scope:
            yield
        return
    previous = getattr(execution_context, "remote_stream_handler", None)
    if execution_context is not None:
        execution_context.remote_stream_handler = handler
    try:
        yield
    finally:
        if execution_context is not None:
            execution_context.remote_stream_handler = previous


class ToolExecutor:
    """Handles tool execution for the agent."""

    def __init__(self, agent: "Agent"):
        self.agent = agent

    def _finish_rejected_call(self, tc: "ToolCall", outcome: ToolOutcome) -> str:
        self.agent._emit_event(
            AgentEvent.tool_call_end(
                tc.name,
                outcome.display_text,
                success=False,
                tool_call_id=tc.id,
                outcome=outcome,
            )
        )
        return outcome.model_text

    def _unknown_tool_outcome(self, tool_name: str) -> ToolOutcome:
        available_names = sorted(
            {
                str(getattr(tool, "name", ""))
                for tool in self.agent.get_active_tools()
                if getattr(tool, "name", None)
            }
        )
        matches = get_close_matches(tool_name, available_names, n=3, cutoff=0.5)
        suggestion = (
            f" Closest available tool{'s' if len(matches) != 1 else ''}: "
            f"{', '.join(repr(name) for name in matches)}."
            if matches
            else ""
        )
        return ToolOutcome(
            status=ToolOutcomeStatus.FAILED,
            summary=f"Unknown tool: {tool_name}",
            content=f"Error: unknown tool '{tool_name}'",
            model_content=(
                f"Tool call rejected [unknown_tool]: '{tool_name}' is not available."
                f"{suggestion}\n"
                "Retry only with an exact currently available tool name, or continue "
                "without a tool. Do not repeat the unavailable tool call."
            ),
            error_kind=ToolErrorKind.NOT_FOUND,
            metadata={
                "preflight_code": "unknown_tool",
                "requested_tool": tool_name,
                "suggested_tools": tuple(matches),
            },
        )

    def execute(self, tc: "ToolCall") -> str:
        """Execute a single tool call."""
        reviewed_diff: str | None = None
        approval_workspace_changes: list[str] = []
        tool = self.agent.get_tool(tc.name)
        if tool is None and (
            getattr(self.agent, "strict_tool_scope", False)
            or self.agent.is_tool_in_scope(tc.name)
        ):
            return self._finish_rejected_call(tc, self._unknown_tool_outcome(tc.name))

        before_context = BeforeToolExecuteContext(
            hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
            agent_id=self.agent.agent_id,
            session_generation=self.agent.session_generation,
            session_id=self.agent.current_session_id,
            turn_id=self.agent._current_turn_id,
            tool_call=tc,
            round_index=self.agent.state.current_round,
            metadata={
                "tool_source": getattr(
                    tool, "tool_source", "builtin" if tool is not None else "unknown"
                ),
                "mcp_server": getattr(tool, "server_name", None),
                "tool_description": getattr(tool, "description", None),
                "tool_schema": getattr(tool, "parameters", None),
                "effect_class": getattr(tool, "effect_class", None),
                "profile": getattr(tool, "approval_profile", None),
            },
        )

        # Fixed core pipeline: authorize -> validate -> approve -> contribute
        # -> execute -> process outcome -> observe -> publish. Extension code
        # cannot reorder or bypass the core validation and approval stages.
        guard_decisions = self.agent.extension_runtime.authorize_tool(before_context)
        denied = next((d for d in guard_decisions if not d.allowed), None)
        if denied is not None:
            message = denied.reason or f"Tool '{tc.name}' blocked by guard hook"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message, success=False, error_kind=ToolErrorKind.DENIED
                    ),
                )
            )
            return message

        for decision in guard_decisions:
            if decision.warning:
                self.agent._emit_event(
                    AgentEvent.diagnostic(
                        decision.warning,
                        code="tool.guard_warning",
                        details={"tool_name": tc.name, "tool_call_id": tc.id},
                    )
                )

        if not self.agent.is_tool_allowed_in_mode(tc.name):
            mode_name = self.agent.active_mode or "default"
            suggested_modes = self.agent.suggest_modes_for_tool(tc.name)
            if suggested_modes:
                suggestions = ", ".join(
                    f"/mode switch {name}" for name in suggested_modes
                )
                message = (
                    f"Tool '{tc.name}' is not available in current mode '{mode_name}'. "
                    f"Ask user to switch mode first: {suggestions}"
                )
            else:
                message = (
                    f"Tool '{tc.name}' is not available in current mode '{mode_name}'"
                )
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name, message, success=False, tool_call_id=tc.id
                )
            )
            return message

        external_target = _external_workspace_target(tool, tc.arguments)
        external_mutation = (
            external_target is not None and tc.name in _EXTERNAL_MUTATION_TOOLS
        )
        backend = getattr(tool, "backend", None)
        workspace = getattr(backend, "workspace", None)
        if tool is not None:
            preflight_target = None if external_mutation else external_target
            with _workspace_access_scope(workspace, preflight_target):
                preflight_failure = tool.preflight_validate(
                    tc.arguments,
                    schema_only=external_mutation,
                )
            if preflight_failure is not None:
                return self._finish_rejected_call(tc, preflight_failure)

        if external_mutation:
            approval_required = GuardDecision.require_approval(
                "Target is outside the workspace. Approval grants this tool call "
                f"access to one exact file only: {external_target}"
            )
        else:
            approval_required = next(
                (d for d in guard_decisions if d.requires_approval), None
            )
        if approval_required is not None:
            provider = self.agent.approval_provider
            if provider is None:
                message = (
                    approval_required.reason
                    or f"Tool '{tc.name}' requires approval, but no approval provider is configured"
                )
                self.agent._emit_event(
                    AgentEvent.tool_call_end(
                        tc.name, message, success=False, tool_call_id=tc.id
                    )
                )
                return message
            try:
                for approval_attempt in range(3):
                    approval_request = ApprovalRequest(
                        tool_name=tc.name,
                        tool_args=dict(tc.arguments),
                        tool_source=(
                            getattr(tool, "tool_source", "builtin_tool")
                            if tool is not None
                            else "unknown"
                        ),
                        reason=approval_required.reason,
                        effect_class=before_context.metadata.get("effect_class"),
                        profile=before_context.metadata.get("profile"),
                        metadata={
                            "agent_id": self.agent.agent_id,
                            "session_generation": self.agent.session_generation,
                            "turn_id": self.agent._current_turn_id,
                            "tool_call_id": tc.id,
                            "approval_attempt": approval_attempt,
                            "workspace_changed_during_approval": bool(
                                approval_workspace_changes
                            ),
                            "invocation_reason": tc.arguments.get("reason"),
                            "policy_reason": approval_required.reason,
                            "is_subagent": bool(
                                getattr(self.agent, "subagent_job_id", None)
                            ),
                            "subagent_job_id": getattr(
                                self.agent, "subagent_job_id", None
                            ),
                            "subagent_mode": getattr(self.agent, "subagent_mode", None),
                            "subagent_task": getattr(self.agent, "subagent_task", None),
                            "external_workspace_path": external_target,
                            "workspace_root": (
                                str(getattr(workspace, "root", ""))
                                if external_target is not None
                                else None
                            ),
                            "force_human_review": external_mutation,
                        },
                    )
                    if not external_mutation and isinstance(
                        tc.arguments.get("reason"), str
                    ):
                        approval_request.reason = tc.arguments["reason"].strip()
                    with _workspace_access_scope(workspace, external_target):
                        before_approval = capture_approval_document(
                            approval_request, workspace=workspace
                        )
                        approval_request.preview = build_approval_preview(
                            approval_request, workspace=workspace
                        )
                    decision = provider.request_approval(approval_request)
                    if not decision.approved:
                        break
                    with _workspace_access_scope(workspace, external_target):
                        after_approval = capture_approval_document(
                            approval_request, workspace=workspace
                        )
                    if before_approval == after_approval:
                        break
                    if before_approval is not None and after_approval is not None:
                        approval_workspace_changes.append(
                            diff_approval_documents(before_approval, after_approval)
                        )
                else:
                    message = (
                        f"Tool '{tc.name}' target kept changing during approval; "
                        "retry after editor changes settle"
                    )
                    self.agent._emit_event(
                        AgentEvent.tool_call_end(
                            tc.name, message, success=False, tool_call_id=tc.id
                        )
                    )
                    return message
            except (KeyboardInterrupt, EOFError):
                message = f"Tool '{tc.name}' approval interrupted by user"
                self.agent._emit_event(
                    AgentEvent.tool_call_end(
                        tc.name, message, success=False, tool_call_id=tc.id
                    )
                )
                return message

            if not decision.approved:
                message = (
                    decision.reason or f"Tool '{tc.name}' denied by approval provider"
                )
                self.agent._emit_event(
                    AgentEvent.tool_call_end(
                        tc.name, message, success=False, tool_call_id=tc.id
                    )
                )
                return message
            if decision.reviewed and approval_request.preview is not None:
                reviewed_diff = next(
                    (
                        str(section.content)
                        for section in approval_request.preview.sections
                        if section.kind is ApprovalSectionKind.DIFF
                    ),
                    None,
                )

            if external_mutation and tool is not None:
                with _workspace_access_scope(workspace, external_target):
                    preflight_failure = tool.preflight_validate(tc.arguments)
                if preflight_failure is not None:
                    return self._finish_rejected_call(tc, preflight_failure)

        try:
            before_context = self.agent.extension_runtime.contribute_tool_context(
                before_context
            )
        except Exception as exc:
            message = f"Tool '{tc.name}' context contribution failed: {exc}"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.INTERNAL,
                    ),
                )
            )
            return message
        self.agent.extension_runtime.observe(
            HookPoint.BEFORE_TOOL_EXECUTE, before_context
        )

        tool_call = before_context.tool_call or tc

        # Tool availability is scoped by composition. Never reconstruct a
        # builtin with a different backend after authorization.
        tool = self.agent.get_tool(tool_call.name)

        if tool is None:
            return self._finish_rejected_call(
                tc, self._unknown_tool_outcome(tool_call.name)
            )

        stop_requested = getattr(self.agent, "stop_requested", None)
        if callable(stop_requested) and stop_requested():
            message = f"Tool '{tc.name}' cancelled before execution."
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome(
                        status=ToolOutcomeStatus.CANCELLED,
                        content=message,
                        error_kind=ToolErrorKind.INTERRUPTED,
                    ),
                )
            )
            return message

        try:
            backend = getattr(tool, "backend", None)
            execution_context = getattr(backend, "context", None)
            outer_stream_handler = getattr(
                execution_context, "remote_stream_handler", None
            )

            def stream_handler(tool_name, chunk) -> None:
                from reuleauxcoder.domain.process_output import (
                    terminal_safe_display,
                )

                self.agent._emit_event(
                    AgentEvent.tool_output_delta(
                        tool_name,
                        terminal_safe_display(
                            str(getattr(chunk, "data", ""))
                        ),
                        stream=str(getattr(chunk, "chunk_type", "stdout")),
                        tool_call_id=tc.id,
                    )
                )
                if callable(outer_stream_handler):
                    outer_stream_handler(tool_name, chunk)

            with _stream_handler_scope(
                backend,
                execution_context,
                stream_handler,
            ):
                bind_execution = getattr(tool, "bind_execution", None)
                if callable(bind_execution):
                    bind_execution(
                        tool_call_id=tc.id,
                        session_generation=self.agent.session_generation,
                    )
                execution_workspace = getattr(backend, "workspace", None)
                with _workspace_access_scope(execution_workspace, external_target):
                    raw_result = tool.execute(**tool_call.arguments)
            outcome = (
                raw_result
                if isinstance(raw_result, ToolOutcome)
                else ToolOutcome.from_legacy(raw_result)
            )
            if approval_workspace_changes:
                change_report = "\n\n".join(
                    item for item in approval_workspace_changes if item
                )
                notice = "[workspace changed while approval was pending; preview was refreshed]"
                if change_report:
                    notice += f"\n{change_report}"
                outcome = outcome.with_model_projection(
                    f"{outcome.model_text}\n\n{notice}"
                ).with_metadata(workspace_changed_during_approval=True)
            if (shell_cwd := getattr(tool, "_cwd", None)) is not None:
                self.agent.runtime_working_directory = str(shell_cwd)
            after_context = AfterToolExecuteContext(
                hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                agent_id=self.agent.agent_id,
                session_generation=self.agent.session_generation,
                session_id=self.agent.current_session_id,
                turn_id=self.agent._current_turn_id,
                tool_call=tool_call,
                result=outcome.model_text,
                outcome=outcome,
                round_index=self.agent.state.current_round,
            )
            after_context = self.agent.extension_runtime.process_tool_outcome(
                after_context
            )
            outcome = after_context.outcome or ToolOutcome.from_legacy(
                after_context.result
            )
            if (
                reviewed_diff is not None
                and outcome.diff is not None
                and outcome.diff.unified == reviewed_diff
            ):
                outcome = outcome.with_metadata(diff_reviewed=True)
                after_context.outcome = outcome
            # Legacy transforms may still replace ``result``.  Preserve that
            # compatibility at this single hook boundary.
            if after_context.result != outcome.model_text:
                outcome = outcome.with_model_projection(after_context.result)
                after_context.outcome = outcome
            self.agent.extension_runtime.observe(
                HookPoint.AFTER_TOOL_EXECUTE, after_context
            )
            if outcome.archive_reference is not None:
                self.agent.history_ledger.append(
                    "artifact_stored",
                    {
                        "tool_call_id": tc.id,
                        "tool_name": tool_call.name,
                        "artifact": {
                            "path": outcome.archive_reference.path,
                            "media_type": outcome.archive_reference.media_type,
                            "checksum_sha256": outcome.archive_reference.checksum_sha256,
                            "size_bytes": outcome.archive_reference.size_bytes,
                        },
                        "original_lines": (
                            outcome.truncation.original_lines
                            if outcome.truncation
                            else None
                        ),
                        "original_chars": (
                            outcome.truncation.original_chars
                            if outcome.truncation
                            else None
                        ),
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=(
                        f"{self.agent._current_turn_id}:{self.agent.state.current_round}"
                        if self.agent._current_turn_id is not None
                        else None
                    ),
                    artifact_refs=(outcome.archive_reference.path,),
                )
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    outcome.model_text,
                    tool_call_id=tc.id,
                    outcome=outcome,
                )
            )
            return outcome.model_text
        except KeyboardInterrupt:
            message = f"Tool '{tool_call.name}' interrupted by user."
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.INTERRUPTED,
                    ),
                )
            )
            if not self.agent.stop_requested():
                self.agent.request_stop()
            raise
        except TypeError as e:
            message = f"Error: bad arguments for {tool_call.name}: {e}"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
                    ),
                )
            )
            return message
        except Exception as e:
            message = f"Error executing {tool_call.name}: {e}"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.EXECUTION,
                    ),
                )
            )
            return message

    def execute_parallel(self, tool_calls: List["ToolCall"]) -> List[str]:
        """Execute multiple tool calls in parallel."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.execute, tc) for tc in tool_calls]
            return [f.result() for f in futures]
