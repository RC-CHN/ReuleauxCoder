"""Tool execution - handles tool calls."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from difflib import get_close_matches
import logging
import time
from typing import TYPE_CHECKING, List, cast

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import ToolCall

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.cancellation import CancellationView
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalGrantCandidate,
    ApprovalGrantScope,
    ApprovalPreview,
    ApprovalRequest,
    ApprovalSectionKind,
)
from reuleauxcoder.domain.approval_subjects import approval_scope_key
from reuleauxcoder.domain.config.models import ApprovalRuleConfig
from reuleauxcoder.domain.approval_preview import (
    build_approval_preview,
    capture_approval_document,
    capture_workspace_document,
    diff_approval_documents,
)
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    GuardDecision,
    HookPoint,
)
from reuleauxcoder.domain.workspace import WorkspaceRevision
from reuleauxcoder.extensions.tools.base import InterruptMode


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
_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class _PreEffectState:
    phase: str = "interrupt_check"
    effect_started: bool = False


class InvalidPreflightResult(RuntimeError):
    """A preflight callback returned a value that cannot safely authorize work."""


class InvalidApprovalSubjectsResult(RuntimeError):
    """An approval-subject callback returned malformed resource identities."""


class InvalidApprovalScopeResult(RuntimeError):
    """An approval-scope callback returned malformed reusable grants."""


class InvalidAuthorizationResult(RuntimeError):
    """An authorization callback returned malformed guard decisions."""


class InvalidApprovalPreview(RuntimeError):
    """An approval preview callback returned an invalid review payload."""


class InvalidApprovalDecisionResult(RuntimeError):
    """An approval provider returned a value outside its typed contract."""


class InvalidContextContributionResult(RuntimeError):
    """A context contributor returned a value outside its typed contract."""


def _safe_pre_effect_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if (
        not name
        or len(name) > 64
        or not name.isascii()
        or not name.replace("_", "").isalnum()
    ):
        return "Exception"
    return name


def _validated_preflight_failure(value: object) -> ToolOutcome | None:
    if value is None:
        return None
    if not isinstance(value, ToolOutcome) or value.success:
        raise InvalidPreflightResult
    return value


def _with_pre_effect_facts(
    outcome: ToolOutcome,
    *,
    phase: str,
    error_type: str,
) -> ToolOutcome:
    fact = (
        "Tool execution stopped before effects began "
        f"(phase={phase}, error_type={error_type}, effect_state=not_started)."
    )
    return outcome.with_model_projection(
        f"{fact}\n\n{outcome.model_text}",
        truncation=outcome.truncation,
        archive_reference=outcome.archive_reference,
    ).with_metadata(
        failure_phase=phase,
        error_type=error_type,
        effect_state="not_started",
    )


def _approval_grant_candidates(
    tool,
    tc: "ToolCall",
    *,
    tool_source: str,
    mcp_server: str | None,
    profile: str | None,
    subjects: tuple[str, ...],
    scope_key: str,
) -> tuple[ApprovalGrantCandidate, ...]:
    build_scopes = getattr(tool, "approval_grant_scopes", None)
    raw_scopes = build_scopes(tc.arguments, subjects) if callable(build_scopes) else ()
    if not isinstance(raw_scopes, (tuple, list)):
        raise InvalidApprovalScopeResult
    scopes = tuple(raw_scopes)
    if not scopes and tool_source == "mcp":
        scopes = (
            ApprovalGrantScope(
                id="exact_tool",
                label="This MCP tool",
                description=(
                    f"{mcp_server} · {tc.name}" if mcp_server else tc.name
                ),
            ),
        )

    candidates: list[ApprovalGrantCandidate] = []
    seen_ids: set[str] = set()
    for scope in scopes:
        if (
            not isinstance(scope, ApprovalGrantScope)
            or not isinstance(scope.id, str)
            or not scope.id.strip()
            or scope.id in seen_ids
            or not isinstance(scope.label, str)
            or not scope.label.strip()
            or not isinstance(scope.description, str)
            or not isinstance(scope.patterns, (tuple, list))
            or any(
                not isinstance(pattern, str) or not pattern
                for pattern in scope.patterns
            )
            or not isinstance(scope.broad, bool)
        ):
            raise InvalidApprovalScopeResult
        seen_ids.add(scope.id)
        patterns: tuple[str | None, ...] = (
            tuple(scope.patterns) if scope.patterns else (None,)
        )
        rules = tuple(
            ApprovalRuleConfig(
                tool_name=tc.name,
                tool_source=tool_source,
                mcp_server=mcp_server,
                profile=profile,
                pattern=pattern,
                scope_key=scope_key,
                action="allow",
            )
            for pattern in patterns
        )
        if not rules:
            continue
        candidates.append(
            ApprovalGrantCandidate(
                id=scope.id,
                label=scope.label,
                description=scope.description,
                proposed_rules=rules,
                scope_key=scope_key,
                broad=scope.broad,
            )
        )
    return tuple(candidates)


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
    external = inspect_external(file_path)
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


@contextmanager
def _workspace_revision_scope(
    backend,
    revision: WorkspaceRevision | None,
) -> Iterator[None]:
    """Bind one call's prepared revision without mutating model arguments."""
    bind = getattr(backend, "workspace_revision_scope", None)
    if not callable(bind):
        yield
        return
    scope = cast(AbstractContextManager[object], bind(revision))
    with scope:
        yield


@contextmanager
def _tool_cancellation_scope(tool, backend, signal) -> Iterator[None]:
    """Install the same per-call signal on tool and backend compatibility paths."""
    tool_bind = getattr(tool, "execution_scope", None)
    backend_bind = getattr(backend, "cancellation_scope", None)
    tool_scope = (
        cast(AbstractContextManager[object], tool_bind(signal))
        if callable(tool_bind)
        else nullcontext()
    )
    backend_scope = (
        cast(AbstractContextManager[object], backend_bind(signal))
        if callable(backend_bind)
        else nullcontext()
    )
    with tool_scope:
        with backend_scope:
            yield


class ToolExecutor:
    """Handles tool execution for the agent."""

    def __init__(self, agent: "Agent"):
        self.agent = agent

    def _round_interrupt_epoch(self) -> int:
        read = getattr(self.agent, "round_interrupt_epoch", None)
        if not callable(read):
            return 0
        value = read()
        return value if isinstance(value, int) else 0

    def _stop_requested(self) -> bool:
        read = getattr(self.agent, "stop_requested", None)
        return bool(read()) if callable(read) else False

    def _stop_signal(self):
        signal = getattr(self.agent, "_stop_event", None)
        if signal is not None and callable(getattr(signal, "is_set", None)):
            return signal

        executor = self

        class _CompatibilityStopSignal:
            def is_set(self) -> bool:
                return executor._stop_requested()

        return _CompatibilityStopSignal()

    @staticmethod
    def _pre_effect_failure_outcome(
        phase: str,
        error: BaseException,
    ) -> ToolOutcome:
        error_type = _safe_pre_effect_error_type(error)
        message = (
            "Tool execution failed before effects began "
            f"(phase={phase}, error_type={error_type}, effect_state=not_started)."
        )
        return ToolOutcome(
            status=ToolOutcomeStatus.FAILED,
            summary="Tool pre-execution failed",
            content=message,
            model_content=message,
            error_kind=ToolErrorKind.INTERNAL,
            metadata={
                "failure_phase": phase,
                "error_type": error_type,
                "effect_state": "not_started",
            },
        )

    @staticmethod
    def _pre_effect_denial_outcome(
        message: str,
        *,
        phase: str,
        error_type: str,
    ) -> ToolOutcome:
        facts = (
            "Tool execution denied before effects began "
            f"(phase={phase}, error_type={error_type}, effect_state=not_started)."
        )
        return ToolOutcome(
            status=ToolOutcomeStatus.DENIED,
            summary="Tool execution denied",
            content=f"{facts}\n\n{message}",
            error_kind=ToolErrorKind.DENIED,
            metadata={
                "failure_phase": phase,
                "error_type": error_type,
                "effect_state": "not_started",
            },
        )

    def _record_secondary_failure(
        self,
        tc: "ToolCall",
        *,
        phase: str,
        error: BaseException,
    ) -> None:
        error_type = _safe_pre_effect_error_type(error)
        try:
            _LOG.warning(
                "Tool secondary observer failed: phase=%s error_type=%s tool_call_id=%s",
                phase,
                error_type,
                tc.id,
            )
        except Exception:
            pass
        try:
            self.agent._emit_event(
                AgentEvent.diagnostic(
                    "Tool secondary observer failed "
                    f"(phase={phase}, error_type={error_type}).",
                    code="tool.secondary_failure",
                    details={
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "failure_phase": phase,
                        "error_type": error_type,
                    },
                )
            )
        except Exception as diagnostic_error:
            try:
                _LOG.warning(
                    "Tool secondary diagnostic emission failed: error_type=%s",
                    _safe_pre_effect_error_type(diagnostic_error),
                )
            except Exception:
                pass

    def _finish_rejected_call(self, tc: "ToolCall", outcome: ToolOutcome) -> str:
        try:
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name,
                    outcome.display_text,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=outcome,
                )
            )
        except Exception as error:
            error_type = _safe_pre_effect_error_type(error)
            try:
                _LOG.warning(
                    "Tool rejection event emission failed: error_type=%s tool_call_id=%s",
                    error_type,
                    tc.id,
                )
            except Exception:
                pass
            return (
                f"{outcome.model_text}\n\nTool failure event emission failed "
                f"(phase=failure_event, error_type={error_type})."
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

    def execute(
        self,
        tc: "ToolCall",
        *,
        interrupt_baseline: int | None = None,
    ) -> str:
        """Execute one call while retaining a generic end-to-end timing."""
        started = time.monotonic()
        status = "ok"
        try:
            return self._execute(tc, interrupt_baseline=interrupt_baseline)
        except BaseException:
            status = "error"
            raise
        finally:
            monitor = getattr(self.agent, "performance_monitor", None)
            if monitor is not None:
                monitor.record(
                    "tool",
                    "call_total",
                    (time.monotonic() - started) * 1000,
                    status=status,
                    attributes={
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "turn_id": self.agent._current_turn_id,
                    },
                )

    def _execute(
        self,
        tc: "ToolCall",
        *,
        interrupt_baseline: int | None = None,
    ) -> str:
        pre_effect = _PreEffectState()
        try:
            return self._execute_pipeline(
                tc,
                interrupt_baseline=interrupt_baseline,
                pre_effect=pre_effect,
            )
        except Exception as error:
            if pre_effect.effect_started:
                raise
            return self._finish_rejected_call(
                tc,
                self._pre_effect_failure_outcome(pre_effect.phase, error),
            )

    def _execute_pipeline(
        self,
        tc: "ToolCall",
        *,
        interrupt_baseline: int | None,
        pre_effect: _PreEffectState,
    ) -> str:
        """Execute a single tool call."""
        if interrupt_baseline is not None and (
            self._stop_requested()
            or self._round_interrupt_epoch() > interrupt_baseline
        ):
            reason = (
                "user steering"
                if self._round_interrupt_epoch() > interrupt_baseline
                and not self._stop_requested()
                else "turn cancellation"
            )
            message = f"Tool execution interrupted ({reason})."
            return self._finish_rejected_call(
                tc,
                ToolOutcome(
                    status=ToolOutcomeStatus.CANCELLED,
                    summary=f"{tc.name} interrupted before execution",
                    content=message,
                    model_content=message,
                    error_kind=ToolErrorKind.INTERRUPTED,
                ),
            )
        reviewed_diff: str | None = None
        approval_workspace_changes: list[str] = []
        expected_workspace_revision: WorkspaceRevision | None = None
        pre_effect.phase = "tool_lookup"
        tool = self.agent.get_tool(tc.name)
        pre_effect.phase = "tool_scope"
        if tool is None and (
            getattr(self.agent, "strict_tool_scope", False)
            or self.agent.is_tool_in_scope(tc.name)
        ):
            return self._finish_rejected_call(tc, self._unknown_tool_outcome(tc.name))

        pre_effect.phase = "mode_policy"
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
            return self._finish_rejected_call(
                tc,
                self._pre_effect_denial_outcome(
                    message,
                    phase="mode_policy",
                    error_type="ToolModeDenied",
                ),
            )

        approval_subjects: tuple[str, ...] = ()
        if tool is not None:
            pre_effect.phase = "schema_validation"
            schema_failure = _validated_preflight_failure(
                tool.preflight_validate(
                    tc.arguments,
                    schema_only=True,
                )
            )
            if schema_failure is not None:
                return self._finish_rejected_call(
                    tc,
                    _with_pre_effect_facts(
                        schema_failure,
                        phase="schema_validation",
                        error_type="ToolPreflightRejected",
                    ),
                )
            pre_effect.phase = "approval_subjects"
            build_subjects = getattr(tool, "approval_subjects", None)
            if callable(build_subjects):
                built_subjects = build_subjects(tc.arguments)
                if not isinstance(built_subjects, (tuple, list)) or any(
                    not isinstance(subject, str) or not subject.strip()
                    for subject in built_subjects
                ) or len(set(built_subjects)) != len(built_subjects):
                    raise InvalidApprovalSubjectsResult
                approval_subjects = tuple(built_subjects)

        pre_effect.phase = "approval_scope"
        current_scope_key = approval_scope_key(
            tool,
            session_id=self.agent.current_session_id,
        )
        if not isinstance(current_scope_key, str) or not current_scope_key:
            raise InvalidApprovalScopeResult
        pre_effect.phase = "authorization_context"
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
                "approval_subjects": approval_subjects,
                "approval_scope_key": current_scope_key,
            },
        )

        # Fixed core pipeline: lookup -> schema validation -> approval subjects
        # -> authorize -> environment validation -> approve -> contribute ->
        # execute -> process outcome -> observe -> publish. Extension code
        # cannot reorder or bypass the core stages.
        pre_effect.phase = "authorize"
        raw_guard_decisions = self.agent.extension_runtime.authorize_tool(before_context)
        if not isinstance(raw_guard_decisions, (tuple, list)) or any(
            not isinstance(decision, GuardDecision)
            or not isinstance(decision.allowed, bool)
            or (
                decision.reason is not None
                and not isinstance(decision.reason, str)
            )
            or (
                decision.warning is not None
                and not isinstance(decision.warning, str)
            )
            or not isinstance(decision.requires_approval, bool)
            for decision in raw_guard_decisions
        ):
            raise InvalidAuthorizationResult
        guard_decisions = tuple(raw_guard_decisions)
        denied = next((d for d in guard_decisions if not d.allowed), None)
        if denied is not None:
            message = denied.reason or f"Tool '{tc.name}' blocked by guard hook"
            return self._finish_rejected_call(
                tc,
                self._pre_effect_denial_outcome(
                    message,
                    phase="authorize",
                    error_type="ToolAuthorizationDenied",
                ),
            )

        for decision in guard_decisions:
            if decision.warning:
                try:
                    self.agent._emit_event(
                        AgentEvent.diagnostic(
                            decision.warning,
                            code="tool.guard_warning",
                            details={"tool_name": tc.name, "tool_call_id": tc.id},
                        )
                    )
                except Exception as error:
                    self._record_secondary_failure(
                        tc,
                        phase="guard_warning_observer",
                        error=error,
                    )

        pre_effect.phase = "workspace_target"
        external_target = _external_workspace_target(tool, tc.arguments)
        external_mutation = (
            external_target is not None and tc.name in _EXTERNAL_MUTATION_TOOLS
        )
        pre_effect.phase = "tool_environment"
        backend = getattr(tool, "backend", None)
        workspace = getattr(backend, "workspace", None)
        if tool is not None:
            if not external_mutation:
                pre_effect.phase = "environment_preflight"
                with _workspace_access_scope(workspace, external_target):
                    preflight_failure = _validated_preflight_failure(
                        tool.preflight_validate(tc.arguments)
                    )
                if preflight_failure is not None:
                    return self._finish_rejected_call(
                        tc,
                        _with_pre_effect_facts(
                            preflight_failure,
                            phase="environment_preflight",
                            error_type="ToolPreflightRejected",
                        ),
                    )
                pre_effect.phase = "document_snapshot"
                with _workspace_access_scope(workspace, external_target):
                    prepared_document = capture_workspace_document(
                        tc.name,
                        tc.arguments,
                        workspace=workspace,
                    )
                if prepared_document is not None:
                    expected_workspace_revision = prepared_document.revision

        pre_effect.phase = "approval_policy"
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
            pre_effect.phase = "approval_provider"
            provider = self.agent.approval_provider
            if provider is None:
                message = (
                    approval_required.reason
                    or f"Tool '{tc.name}' requires approval, but no approval provider is configured"
                )
                return self._finish_rejected_call(
                    tc,
                    self._pre_effect_denial_outcome(
                        message,
                        phase="approval_provider",
                        error_type="ApprovalProviderUnavailable",
                    ),
                )
            try:
                for approval_attempt in range(3):
                    tool_source = str(
                        before_context.metadata.get("tool_source") or "unknown"
                    )
                    mcp_server = before_context.metadata.get("mcp_server")
                    profile = before_context.metadata.get("profile")
                    pre_effect.phase = "approval_scope"
                    grant_candidates = _approval_grant_candidates(
                        tool,
                        tc,
                        tool_source=tool_source,
                        mcp_server=(
                            str(mcp_server) if mcp_server is not None else None
                        ),
                        profile=str(profile) if profile is not None else None,
                        subjects=approval_subjects,
                        scope_key=current_scope_key,
                    )
                    if external_mutation:
                        grant_candidates = tuple(
                            candidate
                            for candidate in grant_candidates
                            if not candidate.broad
                        )
                    pre_effect.phase = "approval_request"
                    approval_request = ApprovalRequest(
                        tool_name=tc.name,
                        tool_args=dict(tc.arguments),
                        tool_source=tool_source,
                        mcp_server=(
                            str(mcp_server) if mcp_server is not None else None
                        ),
                        reason=approval_required.reason,
                        effect_class=before_context.metadata.get("effect_class"),
                        profile=str(profile) if profile is not None else None,
                        subjects=approval_subjects,
                        scope_key=current_scope_key,
                        grant_candidates=grant_candidates,
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
                            "approval_subjects": approval_subjects,
                        },
                    )
                    if not external_mutation and isinstance(
                        tc.arguments.get("reason"), str
                    ):
                        approval_request.reason = tc.arguments["reason"].strip()
                    pre_effect.phase = "approval_preview"
                    with _workspace_access_scope(workspace, external_target):
                        before_approval = capture_approval_document(
                            approval_request, workspace=workspace
                        )
                        if tc.name == "write_file" and before_approval is not None:
                            approval_request.metadata["approval_operation"] = (
                                "Create file"
                                if before_approval.content is None
                                else "Overwrite file"
                            )
                        elif tc.name == "edit_file":
                            approval_request.metadata["approval_operation"] = "Edit file"
                        elif tc.name == "shell":
                            approval_request.metadata["approval_operation"] = (
                                "Run command"
                            )
                        preview = build_approval_preview(
                            approval_request, workspace=workspace
                        )
                        if not isinstance(preview, ApprovalPreview):
                            raise InvalidApprovalPreview
                        approval_request.preview = preview
                    pre_effect.phase = "approval_provider"
                    try:
                        monitor = getattr(self.agent, "performance_monitor", None)
                    except Exception as error:
                        monitor = None
                        self._record_secondary_failure(
                            tc,
                            phase="approval_monitor",
                            error=error,
                        )
                    approval_started = time.monotonic()
                    approval_status = "ok"
                    try:
                        decision = provider.request_approval(approval_request)
                    except BaseException:
                        approval_status = "error"
                        raise
                    finally:
                        if monitor is not None:
                            try:
                                monitor.record(
                                    "tool",
                                    "approval_wait",
                                    (time.monotonic() - approval_started) * 1000,
                                    status=approval_status,
                                    attributes={
                                        "tool_name": tc.name,
                                        "tool_call_id": tc.id,
                                        "approval_attempt": approval_attempt + 1,
                                        "turn_id": self.agent._current_turn_id,
                                    },
                                )
                            except Exception as error:
                                self._record_secondary_failure(
                                    tc,
                                    phase="approval_monitor",
                                    error=error,
                                )
                    pre_effect.phase = "approval_decision"
                    if (
                        not isinstance(decision, ApprovalDecision)
                        or decision.mode
                        not in {"allow_once", "allow_session", "deny_once"}
                        or (
                            decision.reason is not None
                            and not isinstance(decision.reason, str)
                        )
                        or not isinstance(decision.reviewed, bool)
                        or (
                            decision.grant is not None
                            and not isinstance(decision.grant, ApprovalGrantCandidate)
                        )
                        or (
                            decision.mode == "allow_session"
                            and decision.grant is None
                        )
                    ):
                        raise InvalidApprovalDecisionResult
                    if not decision.approved:
                        break
                    pre_effect.phase = "approval_revalidation"
                    with _workspace_access_scope(workspace, external_target):
                        after_approval = capture_approval_document(
                            approval_request, workspace=workspace
                        )
                    if (
                        before_approval is None
                        and after_approval is None
                    ) or (
                        before_approval is not None
                        and after_approval is not None
                        and before_approval.same_content(after_approval)
                    ):
                        if after_approval is not None:
                            expected_workspace_revision = after_approval.revision
                        break
                    if before_approval is not None and after_approval is not None:
                        pre_effect.phase = "approval_revalidation"
                        approval_workspace_changes.append(
                            diff_approval_documents(before_approval, after_approval)
                        )
                else:
                    message = (
                        f"Tool '{tc.name}' target kept changing during approval; "
                        "retry after editor changes settle"
                    )
                    return self._finish_rejected_call(
                        tc,
                        ToolOutcome(
                            status=ToolOutcomeStatus.FAILED,
                            content=(
                                "Tool execution failed before effects began "
                                "(phase=approval_revalidation, "
                                "error_type=ApprovalTargetUnstable, "
                                "effect_state=not_started).\n\n"
                                f"{message}"
                            ),
                            error_kind=ToolErrorKind.EXECUTION,
                            metadata={
                                "failure_phase": "approval_revalidation",
                                "error_type": "ApprovalTargetUnstable",
                                "effect_state": "not_started",
                            },
                        ),
                    )
            except (KeyboardInterrupt, EOFError):
                message = f"Tool '{tc.name}' approval interrupted by user"
                return self._finish_rejected_call(
                    tc,
                    ToolOutcome(
                        status=ToolOutcomeStatus.CANCELLED,
                        content=message,
                        error_kind=ToolErrorKind.INTERRUPTED,
                        metadata={
                            "failure_phase": "approval_provider",
                            "error_type": "ApprovalInterrupted",
                            "effect_state": "not_started",
                        },
                    ),
                )

            pre_effect.phase = "approval_decision"
            if not decision.approved:
                message = (
                    decision.reason or f"Tool '{tc.name}' denied by approval provider"
                )
                return self._finish_rejected_call(
                    tc,
                    self._pre_effect_denial_outcome(
                        message,
                        phase="approval_decision",
                        error_type="ApprovalDenied",
                    ),
                )
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
                pre_effect.phase = "post_approval_preflight"
                with _workspace_access_scope(workspace, external_target):
                    preflight_failure = _validated_preflight_failure(
                        tool.preflight_validate(tc.arguments)
                    )
                if preflight_failure is not None:
                    return self._finish_rejected_call(
                        tc,
                        _with_pre_effect_facts(
                            preflight_failure,
                            phase="post_approval_preflight",
                            error_type="ToolPreflightRejected",
                        ),
                    )

        pre_effect.phase = "context_contribution"
        before_context = self.agent.extension_runtime.contribute_tool_context(
            before_context
        )
        if not isinstance(before_context, BeforeToolExecuteContext):
            raise InvalidContextContributionResult
        pre_effect.phase = "before_execute_observer"
        try:
            self.agent.extension_runtime.observe(
                HookPoint.BEFORE_TOOL_EXECUTE, before_context
            )
        except Exception as error:
            self._record_secondary_failure(
                tc,
                phase="before_execute_observer",
                error=error,
            )

        pre_effect.phase = "context_result"
        tool_call = before_context.tool_call or tc

        # Tool availability is scoped by composition. Never reconstruct a
        # builtin with a different backend after authorization.
        pre_effect.phase = "tool_lookup_after_context"
        tool = self.agent.get_tool(tool_call.name)

        if tool is None:
            return self._finish_rejected_call(
                tc, self._unknown_tool_outcome(tool_call.name)
            )

        pre_effect.phase = "final_cancel_check"
        stop_requested = getattr(self.agent, "stop_requested", None)
        if callable(stop_requested) and stop_requested():
            message = f"Tool '{tc.name}' cancelled before execution."
            return self._finish_rejected_call(
                tc,
                ToolOutcome(
                    status=ToolOutcomeStatus.CANCELLED,
                    content=message,
                    error_kind=ToolErrorKind.INTERRUPTED,
                    metadata={
                        "failure_phase": "final_cancel_check",
                        "error_type": "ToolExecutionCancelled",
                        "effect_state": "not_started",
                    },
                ),
            )

        pre_effect.phase = "execution_setup"
        try:
            backend = getattr(tool, "backend", None)
            if interrupt_baseline is None:
                interrupt_baseline = self._round_interrupt_epoch()
            interrupt_mode = getattr(
                tool, "interrupt_mode", InterruptMode.LET_FINISH
            )
            cancellation = (
                None
                if interrupt_mode is InterruptMode.DETACH
                else CancellationView(
                    self._stop_signal(),
                    self._round_interrupt_epoch,
                    interrupt_baseline,
                    include_round_interrupt=(
                        interrupt_mode is InterruptMode.CANCEL_WITH_PARTIAL
                    ),
                )
            )
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

            execution_started = time.monotonic()
            try:
                with _tool_cancellation_scope(tool, backend, cancellation):
                    with _stream_handler_scope(
                        backend,
                        execution_context,
                        stream_handler,
                    ):
                        with _workspace_revision_scope(
                            backend,
                            expected_workspace_revision,
                        ):
                            bind_execution = getattr(tool, "bind_execution", None)
                            if callable(bind_execution):
                                bind_execution(
                                    tool_call_id=tc.id,
                                    session_generation=self.agent.session_generation,
                                )
                            execution_workspace = getattr(backend, "workspace", None)
                            with _workspace_access_scope(
                                execution_workspace, external_target
                            ):
                                pre_effect.phase = "execute"
                                pre_effect.effect_started = True
                                raw_result = tool.execute(**tool_call.arguments)
            finally:
                execution_seconds = time.monotonic() - execution_started
                monitor = getattr(self.agent, "performance_monitor", None)
                if monitor is not None:
                    monitor.record(
                        "tool",
                        "execute",
                        execution_seconds * 1000,
                        attributes={
                            "tool_name": tool_call.name,
                            "tool_call_id": tc.id,
                            "turn_id": self.agent._current_turn_id,
                        },
                    )
                git_monitor = getattr(self.agent, "git_monitor", None)
                if git_monitor is not None and (
                    getattr(tool, "effect_class", None)
                    in {"filesystem_mutation", "process_execution"}
                    or tool_call.name == "shell_session"
                ):
                    git_monitor.invalidate()
            outcome = (
                raw_result
                if isinstance(raw_result, ToolOutcome)
                else ToolOutcome.from_legacy(raw_result).with_duration(
                    execution_seconds
                )
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
            if not self._stop_requested():
                self.agent.request_stop()
            raise
        except TypeError as e:
            if not pre_effect.effect_started:
                raise
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
            if not pre_effect.effect_started:
                raise
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

    def execute_parallel(
        self,
        tool_calls: List["ToolCall"],
        *,
        interrupt_baseline: int | None = None,
    ) -> List[str]:
        """Execute one provider batch without reordering observable effects.

        Contiguous calls whose resolved tools explicitly opt into
        ``parallel_safe`` may overlap.  Every other call is a singleton ordering
        barrier, so writers, shell commands, MCP calls without trustworthy
        annotations, and unknown tools retain provider order.
        """
        baseline = (
            self._round_interrupt_epoch()
            if interrupt_baseline is None
            else interrupt_baseline
        )
        results = [""] * len(tool_calls)

        def execute_submitted(tool_call: "ToolCall") -> str:
            interrupted = CancellationView(
                self._stop_signal(),
                self._round_interrupt_epoch,
                baseline,
            )
            if interrupted.is_set():
                reason = (
                    "user steering"
                    if self._round_interrupt_epoch() > baseline
                    and not self._stop_requested()
                    else "turn cancellation"
                )
                message = f"Tool execution interrupted ({reason})."
                outcome = ToolOutcome(
                    status=ToolOutcomeStatus.CANCELLED,
                    summary=f"{tool_call.name} interrupted before execution",
                    content=message,
                    model_content=message,
                    error_kind=ToolErrorKind.INTERRUPTED,
                )
                return self._finish_rejected_call(tool_call, outcome)
            return self.execute(tool_call, interrupt_baseline=baseline)

        index = 0
        while index < len(tool_calls):
            if not self._parallel_safe(tool_calls[index]):
                results[index] = execute_submitted(tool_calls[index])
                index += 1
                continue

            end = index + 1
            while end < len(tool_calls) and self._parallel_safe(tool_calls[end]):
                end += 1
            if end - index == 1:
                results[index] = execute_submitted(tool_calls[index])
            else:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, end - index)
                ) as pool:
                    futures = {
                        offset: pool.submit(execute_submitted, tool_calls[offset])
                        for offset in range(index, end)
                    }
                    for offset, future in futures.items():
                        results[offset] = future.result()
            index = end
        return results

    def _parallel_safe(self, tool_call: "ToolCall") -> bool:
        """Resolve one call's scheduling declaration conservatively."""
        tool = self.agent.get_tool(tool_call.name)
        return bool(tool is not None and getattr(tool, "parallel_safe", False))
