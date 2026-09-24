"""Tool execution - handles tool calls."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator
from copy import deepcopy
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from difflib import get_close_matches
import time
from types import MappingProxyType
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
from reuleauxcoder.domain.tools import InterruptMode, Tool
from reuleauxcoder.domain.agent.tool_outcome_validation import (
    _POST_EFFECT_FAILURE_LIMIT as _POST_EFFECT_FAILURE_LIMIT,
    _POST_EFFECT_FAILURE_COUNT_LIMIT as _POST_EFFECT_FAILURE_COUNT_LIMIT,
    _METADATA_MAX_DEPTH as _METADATA_MAX_DEPTH,
    _METADATA_MAX_CONTAINER_ITEMS as _METADATA_MAX_CONTAINER_ITEMS,
    _METADATA_MAX_NODES as _METADATA_MAX_NODES,
    _METADATA_MAX_INT_BITS as _METADATA_MAX_INT_BITS,
    _METADATA_MAX_STRING_BYTES as _METADATA_MAX_STRING_BYTES,
    _METADATA_MAX_TOTAL_STRING_BYTES as _METADATA_MAX_TOTAL_STRING_BYTES,
    _STRUCTURED_FACT_MAX_STRING_BYTES as _STRUCTURED_FACT_MAX_STRING_BYTES,
    _STRUCTURED_FACT_MAX_TOTAL_STRING_BYTES as _STRUCTURED_FACT_MAX_TOTAL_STRING_BYTES,
    _TOOL_DIAGNOSTIC_LIMIT as _TOOL_DIAGNOSTIC_LIMIT,
    _TEXT_VALIDATION_CHUNK_CHARS as _TEXT_VALIDATION_CHUNK_CHARS,
    _FAILURE_FACT_KEYS as _FAILURE_FACT_KEYS,
    _RUNTIME_METADATA_KEYS as _RUNTIME_METADATA_KEYS,
    _REPORTED_EFFECT_STATES as _REPORTED_EFFECT_STATES,
    _VALID_TRUNCATION_STRATEGIES as _VALID_TRUNCATION_STRATEGIES,
    InvalidAfterToolPrimaryOutcomeTransition as InvalidAfterToolPrimaryOutcomeTransition,
    InvalidToolResultProjection as InvalidToolResultProjection,
    InvalidToolOutcomeProtocol as InvalidToolOutcomeProtocol,
    _tool_call_signature as _tool_call_signature,
    _safe_exception_type as _safe_exception_type,
    _safe_failure_phase as _safe_failure_phase,
    _safe_failure_error_type as _safe_failure_error_type,
    _PostEffectFailureCollector as _PostEffectFailureCollector,
    _SnapshotBudget as _SnapshotBudget,
    _validate_unbounded_text as _validate_unbounded_text,
    _validate_fact_text as _validate_fact_text,
    _optional_int as _optional_int,
    _snapshot_json_value as _snapshot_json_value,
    _check_tool_outcome_protocol as _check_tool_outcome_protocol,
    _normalize_model_projection as _normalize_model_projection,
    _normalize_tool_outcome as _normalize_tool_outcome,
    _AFTER_FIXED_OUTCOME_FIELDS as _AFTER_FIXED_OUTCOME_FIELDS,
    _AFTER_FIXED_CONTEXT_FIELDS as _AFTER_FIXED_CONTEXT_FIELDS,
    _accepted_after_transform as _accepted_after_transform,
    _safe_metadata_fact as _safe_metadata_fact,
    _with_failure_facts as _with_failure_facts,
    _cancelled_before_outcome as _cancelled_before_outcome,
    _with_canonical_failure_projection as _with_canonical_failure_projection,
    _coerce_returned_outcome as _coerce_returned_outcome,
    _completed_result_failure as _completed_result_failure,
    _record_hook_diagnostics as _record_hook_diagnostics,
    _with_post_effect_failures as _with_post_effect_failures,
    InvalidContextContributionResult as InvalidContextContributionResult,
)


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
_PENDING_RUNTIME_FAILURE_LIMIT = 8
_RUNTIME_FAILURE_PUBLISH_ATTEMPTS = 2
_UNRESOLVED_TOOL = object()


class MissingRuntimeIssueSink(RuntimeError):
    pass


@dataclass(slots=True)
class _PreEffectState:
    phase: str = "interrupt_check"
    effect_started: bool = False


class InvalidPreflightResult(RuntimeError):
    pass


class InvalidApprovalSubjectsResult(RuntimeError):
    pass


class InvalidApprovalScopeResult(RuntimeError):
    pass


class InvalidAuthorizationResult(RuntimeError):
    pass


class InvalidApprovalPreview(RuntimeError):
    pass


class InvalidApprovalDecisionResult(RuntimeError):
    pass


def _validated_preflight_failure(value: object) -> ToolOutcome | None:
    if value is None:
        return None
    if not isinstance(value, ToolOutcome):
        raise InvalidPreflightResult
    try:
        normalized = _normalize_tool_outcome(value)
    except Exception:
        raise InvalidPreflightResult from None
    if normalized.success:
        raise InvalidPreflightResult
    return normalized


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
    return _with_failure_facts(
        outcome.with_model_projection(
            f"{fact}\n\n{outcome.model_text}",
            truncation=outcome.truncation,
            archive_reference=outcome.archive_reference,
        ),
        phase=phase,
        error_type=error_type,
        effect_state="not_started",
        completion_state="not_started",
        retry_safety=(
            "do_not_retry_automatically"
            if outcome.status is ToolOutcomeStatus.DENIED
            else "safe_to_retry"
        ),
        replace_existing=True,
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
    raw_scopes = (
        build_scopes(deepcopy(tc.arguments), subjects) if callable(build_scopes) else ()
    )
    if not isinstance(raw_scopes, (tuple, list)):
        raise InvalidApprovalScopeResult
    scopes = tuple(raw_scopes)
    if not scopes and tool_source == "mcp":
        scopes = (
            ApprovalGrantScope(
                id="exact_tool",
                label="This MCP tool",
                description=(f"{mcp_server} · {tc.name}" if mcp_server else tc.name),
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
        _EXTERNAL_PATH_ARGUMENTS.get(tool_name) if isinstance(tool_name, str) else None
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
    access_scope = cast(AbstractContextManager[object], grant_external(external_target))
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
        self._pending_runtime_failures = _PostEffectFailureCollector(
            limit=_PENDING_RUNTIME_FAILURE_LIMIT
        )

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

    def _emit_post_effect_diagnostic(
        self,
        tc: "ToolCall",
        failure: tuple[str, str, int],
        failures: _PostEffectFailureCollector,
    ) -> None:
        phase, error_type, count = failure
        try:
            self.agent._emit_event(
                AgentEvent.diagnostic(
                    "Secondary tool processing failed after the primary outcome "
                    f"was fixed (phase={phase}, error_type={error_type}, "
                    f"count={count}).",
                    code="tool.post_effect_failure",
                    details={
                        "tool_name": tc.name,
                        "tool_call_id": tc.id,
                        "phase": phase,
                        "error_type": error_type,
                        "count": count,
                    },
                )
            )
        except BaseException as error:
            # The model-facing result still carries the same safe fact. A
            # broken event sink cannot replace the primary tool outcome.
            self._capture_post_effect_failure(
                failures,
                "post_effect_diagnostic",
                error,
            )

    def _emit_batch_post_effect_diagnostic(
        self,
        failure: tuple[str, str, int],
        failures: _PostEffectFailureCollector,
        *,
        tool_count: int,
    ) -> None:
        phase, error_type, count = failure
        try:
            self.agent._emit_event(
                AgentEvent.diagnostic(
                    "Secondary parallel-batch processing failed after all tool "
                    "outcomes were fixed "
                    f"(phase={phase}, error_type={error_type}, count={count}).",
                    code="tool.post_effect_failure",
                    details={
                        "scope": "parallel_batch",
                        "tool_count": tool_count,
                        "phase": phase,
                        "error_type": error_type,
                        "count": count,
                    },
                )
            )
        except BaseException as error:
            self._capture_post_effect_failure(
                failures,
                "post_effect_diagnostic",
                error,
            )

    def _retain_runtime_failures(
        self,
        facts: tuple[tuple[str, str, int], ...],
    ) -> None:
        for phase, error_type, count in facts:
            if error_type == "AdditionalFailuresOmitted":
                self._pending_runtime_failures.omit(count)
            else:
                self._pending_runtime_failures.record(
                    phase,
                    error_type,
                    count=count,
                )

    def _publish_runtime_failures(
        self,
        facts: tuple[tuple[str, str, int], ...],
    ) -> tuple[int, bool]:
        published = 0
        for index, (phase, error_type, count) in enumerate(facts):
            try:
                sink = getattr(self.agent, "record_runtime_issue", None)
                if not callable(sink):
                    raise MissingRuntimeIssueSink
                accepted = sink(phase, error_type, "parallel_batch", count)
                if accepted is False:
                    raise MissingRuntimeIssueSink
            except BaseException as error:
                self._retain_runtime_failures(facts[index:])
                sink_failures = _PostEffectFailureCollector()
                self._capture_post_effect_failure(
                    sink_failures,
                    "runtime_issue_publish",
                    error,
                )
                self._retain_runtime_failures(sink_failures.snapshot())
                return published, True
            published += 1
        return published, False

    def _queue_batch_runtime_failure(
        self,
        phase: str,
        error: BaseException,
        *,
        tool_count: int,
    ) -> None:
        failures = _PostEffectFailureCollector()
        self._capture_post_effect_failure(failures, phase, error)
        self._emit_batch_post_effect_diagnostic(
            failures.snapshot()[0],
            failures,
            tool_count=tool_count,
        )
        self._publish_runtime_failures(failures.snapshot())

    def flush_pending_runtime_issues(self) -> int | None:
        """Publish retained safe facts before another model request."""
        published = 0
        for _ in range(_RUNTIME_FAILURE_PUBLISH_ATTEMPTS):
            facts = self._pending_runtime_failures.drain()
            if not facts:
                return published
            delivered, failed = self._publish_runtime_failures(facts)
            published += delivered
            if not failed:
                return published

        stop_failures = _PostEffectFailureCollector()
        self._request_stop_safely(stop_failures)
        self._retain_runtime_failures(stop_failures.snapshot())
        return None

    def _request_stop_safely(
        self,
        failures: _PostEffectFailureCollector,
    ) -> None:
        """Request shutdown without allowing ordinary observer faults to win."""
        try:
            already_requested = self._stop_requested()
        except BaseException as error:
            failures.record("post_effect", _safe_exception_type(error))
            already_requested = False
        if already_requested:
            return
        try:
            request_stop = getattr(self.agent, "request_stop", None)
            if callable(request_stop):
                request_stop()
        except BaseException as error:
            failures.record("post_effect", _safe_exception_type(error))

    def _capture_post_effect_failure(
        self,
        failures: _PostEffectFailureCollector,
        phase: str,
        error: BaseException,
    ) -> None:
        failures.record(phase, _safe_exception_type(error))
        if not isinstance(error, Exception):
            self._request_stop_safely(failures)

    def _finalize_post_effect_projection(
        self,
        outcome: ToolOutcome,
        failures: _PostEffectFailureCollector,
    ) -> ToolOutcome:
        """Add runtime-owned facts once, after every secondary stage has run."""
        if not outcome.success:
            outcome = _with_failure_facts(
                outcome,
                phase="tool_result",
                error_type="ToolReportedFailure",
                effect_state="unknown",
                completion_state="uncertain",
                retry_safety="do_not_retry_automatically",
            )
            outcome = _with_canonical_failure_projection(outcome)
        return _with_post_effect_failures(outcome, failures)

    def _publish_post_effect_outcome(
        self,
        tc: "ToolCall",
        tool_name: str,
        outcome: ToolOutcome,
        failures: _PostEffectFailureCollector,
    ) -> ToolOutcome:
        published = self._finalize_post_effect_projection(outcome, failures)
        try:
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_name,
                    published.model_text,
                    tool_call_id=tc.id,
                    outcome=published,
                )
            )
        except BaseException as error:
            self._capture_post_effect_failure(failures, "tool_end_event", error)
            published = self._finalize_post_effect_projection(outcome, failures)
        return published

    @staticmethod
    def _pre_effect_failure_outcome(
        phase: str,
        error: BaseException,
        *,
        cancelled: bool = False,
    ) -> ToolOutcome:
        error_type = _safe_exception_type(error)
        action = "interrupted" if cancelled else "failed"
        message = (
            f"Tool execution {action} before effects began "
            f"(phase={phase}, error_type={error_type}, effect_state=not_started, "
            "completion_state=not_started, retry_safety=safe_to_retry)."
        )
        return _with_failure_facts(
            ToolOutcome(
                status=(
                    ToolOutcomeStatus.CANCELLED
                    if cancelled
                    else ToolOutcomeStatus.FAILED
                ),
                summary=f"Tool pre-execution {action}",
                content=message,
                model_content=message,
                error_kind=(
                    ToolErrorKind.INTERRUPTED if cancelled else ToolErrorKind.INTERNAL
                ),
            ),
            phase=phase,
            error_type=error_type,
            effect_state="not_started",
            completion_state="not_started",
            retry_safety="safe_to_retry",
            replace_existing=True,
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
        return _with_failure_facts(
            ToolOutcome(
                status=ToolOutcomeStatus.DENIED,
                summary="Tool execution denied",
                content=f"{facts}\n\n{message}",
                error_kind=ToolErrorKind.DENIED,
            ),
            phase=phase,
            error_type=error_type,
            effect_state="not_started",
            completion_state="not_started",
            retry_safety="do_not_retry_automatically",
            replace_existing=True,
        )

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
        return _with_failure_facts(
            ToolOutcome(
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
            ),
            phase="tool_lookup",
            error_type="UnknownTool",
            effect_state="not_started",
            completion_state="not_started",
            retry_safety="do_not_retry_automatically",
            replace_existing=True,
        )

    def execute(
        self,
        tc: "ToolCall",
        *,
        interrupt_baseline: int | None = None,
        _resolved_tool: object = _UNRESOLVED_TOOL,
        _resolution_error: BaseException | None = None,
    ) -> str:
        """Execute and publish exactly one authoritative result for one call."""
        started = time.monotonic()
        failures = _PostEffectFailureCollector()
        pre_effect = _PreEffectState()
        tool_call = tc
        try:
            pre_effect.phase = "tool_call_snapshot"
            _tool_call_signature(tc)
            tool_call = deepcopy(tc)
            _tool_call_signature(tool_call)
            outcome = self._execute_pipeline(
                tool_call,
                interrupt_baseline=interrupt_baseline,
                pre_effect=pre_effect,
                failures=failures,
                resolved_tool=_resolved_tool,
                resolution_error=_resolution_error,
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                self._request_stop_safely(failures)
            outcome = (
                self._execution_failure_outcome(pre_effect.phase, error)
                if pre_effect.effect_started
                else self._pre_effect_failure_outcome(
                    pre_effect.phase,
                    error,
                    cancelled=True,
                )
                if isinstance(error, KeyboardInterrupt)
                else self._pre_effect_failure_outcome(pre_effect.phase, error)
            )

        try:
            monitor = getattr(self.agent, "performance_monitor", None)
            if monitor is not None:
                monitor.record(
                    "tool",
                    "call_total",
                    (time.monotonic() - started) * 1000,
                    status="ok" if outcome.success else "error",
                    attributes={
                        "tool_name": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "turn_id": self.agent._current_turn_id,
                    },
                )
        except BaseException as error:
            self._capture_post_effect_failure(failures, "call_total_monitor", error)
        if failures:
            self._emit_post_effect_diagnostic(
                tool_call, failures.snapshot()[0], failures
            )
        published = self._publish_post_effect_outcome(
            tool_call,
            tool_call.name,
            outcome,
            failures,
        )
        return published.model_message_content

    @staticmethod
    def _execution_failure_outcome(
        phase: str,
        error: BaseException,
    ) -> ToolOutcome:
        interrupted = isinstance(error, KeyboardInterrupt)
        action = "interrupted" if interrupted else "failed"
        error_type = _safe_exception_type(error)
        message = (
            f"Tool execution {action} (phase={phase}, error_type={error_type}, "
            "effect_state=started, completion_state=uncertain, "
            "retry_safety=do_not_retry_automatically). The tool may have produced "
            "partial effects; do not retry automatically."
        )
        return _with_failure_facts(
            ToolOutcome(
                status=(
                    ToolOutcomeStatus.CANCELLED
                    if interrupted
                    else ToolOutcomeStatus.FAILED
                ),
                summary=f"Tool execution {action}",
                content=message,
                model_content=message,
                error_kind=(
                    ToolErrorKind.INTERRUPTED
                    if interrupted
                    else ToolErrorKind.EXECUTION
                ),
            ),
            phase=phase,
            error_type=error_type,
            effect_state="started",
            completion_state="uncertain",
            retry_safety="do_not_retry_automatically",
            replace_existing=True,
        )

    def _execute_pipeline(
        self,
        tc: "ToolCall",
        *,
        interrupt_baseline: int | None,
        pre_effect: _PreEffectState,
        failures: _PostEffectFailureCollector,
        resolved_tool: object,
        resolution_error: BaseException | None,
    ) -> ToolOutcome:
        """Execute a single tool call."""
        authorized_signature = _tool_call_signature(tc)
        if interrupt_baseline is not None and (
            self._stop_requested() or self._round_interrupt_epoch() > interrupt_baseline
        ):
            reason = (
                "user steering"
                if self._round_interrupt_epoch() > interrupt_baseline
                and not self._stop_requested()
                else "turn cancellation"
            )
            message = (
                f"Tool execution interrupted before execution ({reason}; "
                "phase=initial_cancel_check, error_type=ToolExecutionCancelled, "
                "effect_state=not_started, completion_state=not_started, "
                "retry_safety=safe_to_retry)."
            )
            return _cancelled_before_outcome(
                tc.name,
                "initial_cancel_check",
                message,
            )
        reviewed_diff: str | None = None
        approval_workspace_changes: list[str] = []
        expected_workspace_revision: WorkspaceRevision | None = None
        pre_effect.phase = "tool_lookup"
        if resolution_error is not None:
            raise resolution_error
        resolved = (
            self.agent.get_tool(tc.name)
            if resolved_tool is _UNRESOLVED_TOOL
            else resolved_tool
        )
        pre_effect.phase = "tool_scope"
        if resolved is None:
            has_registered_tool = getattr(self.agent, "has_registered_tool", None)
            if (
                callable(has_registered_tool)
                and has_registered_tool(tc.name)
                and not self.agent.is_tool_allowed_in_mode(tc.name)
            ):
                mode_name = self.agent.active_mode or "default"
                message = (
                    f"Tool '{tc.name}' is not available in current mode "
                    f"'{mode_name}'"
                )
                return self._pre_effect_denial_outcome(
                    message,
                    phase="mode_policy",
                    error_type="ToolModeDenied",
                )
            return self._unknown_tool_outcome(tc.name)
        tool = cast(Tool, resolved)

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
            return self._pre_effect_denial_outcome(
                message,
                phase="mode_policy",
                error_type="ToolModeDenied",
            )

        approval_subjects: tuple[str, ...] = ()
        if tool is not None:
            pre_effect.phase = "schema_validation"
            schema_failure = _validated_preflight_failure(
                tool.preflight_validate(
                    deepcopy(tc.arguments),
                    schema_only=True,
                )
            )
            if schema_failure is not None:
                return _with_pre_effect_facts(
                    schema_failure,
                    phase="schema_validation",
                    error_type="ToolPreflightRejected",
                )
            pre_effect.phase = "approval_subjects"
            build_subjects = getattr(tool, "approval_subjects", None)
            if callable(build_subjects):
                built_subjects = build_subjects(deepcopy(tc.arguments))
                if (
                    not isinstance(built_subjects, (tuple, list))
                    or any(
                        not isinstance(subject, str) or not subject.strip()
                        for subject in built_subjects
                    )
                    or len(set(built_subjects)) != len(built_subjects)
                ):
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
        authorization_context = deepcopy(before_context)
        raw_guard_decisions = self.agent.extension_runtime.authorize_tool(
            authorization_context
        )
        if _tool_call_signature(
            authorization_context.tool_call
        ) != _tool_call_signature(tc):
            raise InvalidAuthorizationResult
        if not isinstance(raw_guard_decisions, (tuple, list)) or any(
            not isinstance(decision, GuardDecision)
            or not isinstance(decision.allowed, bool)
            or (decision.reason is not None and not isinstance(decision.reason, str))
            or (decision.warning is not None and not isinstance(decision.warning, str))
            or not isinstance(decision.requires_approval, bool)
            for decision in raw_guard_decisions
        ):
            raise InvalidAuthorizationResult
        guard_decisions = tuple(raw_guard_decisions)
        denied = next((d for d in guard_decisions if not d.allowed), None)
        if denied is not None:
            message = denied.reason or f"Tool '{tc.name}' blocked by guard hook"
            return self._pre_effect_denial_outcome(
                message,
                phase="authorize",
                error_type="ToolAuthorizationDenied",
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
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "guard_warning_observer",
                        error,
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
                        tool.preflight_validate(deepcopy(tc.arguments))
                    )
                if preflight_failure is not None:
                    return _with_pre_effect_facts(
                        preflight_failure,
                        phase="environment_preflight",
                        error_type="ToolPreflightRejected",
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
                return self._pre_effect_denial_outcome(
                    message,
                    phase="approval_provider",
                    error_type="ApprovalProviderUnavailable",
                )
            try:
                # The arguments are canonical and identical across attempts;
                # only the preview is refreshed when the file changes on disk.
                approval_tool_args = deepcopy(tc.arguments)
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
                        tool_args=approval_tool_args,
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
                            approval_request.metadata["approval_operation"] = (
                                "Edit file"
                            )
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
                    except BaseException as error:
                        monitor = None
                        self._capture_post_effect_failure(
                            failures,
                            "approval_monitor",
                            error,
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
                            except BaseException as error:
                                self._capture_post_effect_failure(
                                    failures,
                                    "approval_monitor",
                                    error,
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
                        or (decision.mode == "allow_session" and decision.grant is None)
                    ):
                        raise InvalidApprovalDecisionResult
                    if not decision.approved:
                        break
                    pre_effect.phase = "approval_revalidation"
                    with _workspace_access_scope(workspace, external_target):
                        after_approval = capture_approval_document(
                            approval_request, workspace=workspace
                        )
                    if (before_approval is None and after_approval is None) or (
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
                    return _with_failure_facts(
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
                        ),
                        phase="approval_revalidation",
                        error_type="ApprovalTargetUnstable",
                        effect_state="not_started",
                        completion_state="not_started",
                        retry_safety="safe_to_retry",
                        replace_existing=True,
                    )
            except (KeyboardInterrupt, EOFError) as error:
                message = f"Tool '{tc.name}' approval interrupted by user"
                return _with_failure_facts(
                    ToolOutcome(
                        status=ToolOutcomeStatus.CANCELLED,
                        content=message,
                        error_kind=ToolErrorKind.INTERRUPTED,
                    ),
                    phase="approval_provider",
                    error_type=(
                        "ApprovalInterrupted"
                        if isinstance(error, KeyboardInterrupt)
                        else "ApprovalInputClosed"
                    ),
                    effect_state="not_started",
                    completion_state="not_started",
                    retry_safety="safe_to_retry",
                    replace_existing=True,
                )

            pre_effect.phase = "approval_decision"
            if not decision.approved:
                message = (
                    decision.reason or f"Tool '{tc.name}' denied by approval provider"
                )
                return self._pre_effect_denial_outcome(
                    message,
                    phase="approval_decision",
                    error_type="ApprovalDenied",
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
                        tool.preflight_validate(deepcopy(tc.arguments))
                    )
                if preflight_failure is not None:
                    return _with_pre_effect_facts(
                        preflight_failure,
                        phase="post_approval_preflight",
                        error_type="ToolPreflightRejected",
                    )

        pre_effect.phase = "context_contribution"
        contributed_context = self.agent.extension_runtime.contribute_tool_context(
            deepcopy(before_context)
        )
        if not isinstance(contributed_context, BeforeToolExecuteContext) or (
            _tool_call_signature(contributed_context.tool_call)
            != _tool_call_signature(tc)
        ):
            raise InvalidContextContributionResult
        before_context = contributed_context
        # Restore the canonical tool call into the context. ``tc`` is already
        # the pipeline's private snapshot, and the only consumer below is the
        # observer hand-off, which deep-copies the whole context again.
        before_context.tool_call = tc
        pre_effect.phase = "before_execute_observer"
        try:
            observer_diagnostics = self.agent.extension_runtime.observe(
                HookPoint.BEFORE_TOOL_EXECUTE, deepcopy(before_context)
            )
            _record_hook_diagnostics(
                failures,
                observer_diagnostics,
                hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
                default_phase="before_execute_observer",
            )
        except BaseException as error:
            self._capture_post_effect_failure(
                failures,
                "before_execute_observer",
                error,
            )

        pre_effect.phase = "context_result"
        tool_call = tc
        if _tool_call_signature(tool_call) != authorized_signature:
            raise InvalidAuthorizationResult

        pre_effect.phase = "final_cancel_check"
        stop_requested = getattr(self.agent, "stop_requested", None)
        if callable(stop_requested) and stop_requested():
            message = (
                f"Tool '{tc.name}' cancelled before execution "
                "(phase=final_cancel_check, error_type=ToolExecutionCancelled, "
                "effect_state=not_started, completion_state=not_started, "
                "retry_safety=safe_to_retry)."
            )
            return _cancelled_before_outcome(
                tc.name,
                "final_cancel_check",
                message,
            )

        pre_effect.phase = "execution_setup"
        tool_returned = False
        raw_result: object = _UNRESOLVED_TOOL
        execution_seconds = 0.0
        authoritative_outcome: ToolOutcome | None = None
        try:
            backend = getattr(tool, "backend", None)
            if interrupt_baseline is None:
                interrupt_baseline = self._round_interrupt_epoch()
            interrupt_mode = getattr(tool, "interrupt_mode", InterruptMode.LET_FINISH)
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
                try:
                    from reuleauxcoder.domain.process_output import (
                        terminal_safe_display,
                    )

                    self.agent._emit_event(
                        AgentEvent.tool_output_delta(
                            tool_name,
                            terminal_safe_display(str(getattr(chunk, "data", ""))),
                            stream=str(getattr(chunk, "chunk_type", "stdout")),
                            tool_call_id=tc.id,
                        )
                    )
                except BaseException as error:
                    self._capture_post_effect_failure(failures, "stream_event", error)
                if callable(outer_stream_handler):
                    try:
                        outer_stream_handler(tool_name, chunk)
                    except BaseException as error:
                        self._capture_post_effect_failure(
                            failures, "stream_observer", error
                        )

            execution_started = time.monotonic()
            primary_execution_error: BaseException | None = None
            try:
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
                                execution_workspace = getattr(
                                    backend, "workspace", None
                                )
                                with _workspace_access_scope(
                                    execution_workspace, external_target
                                ):
                                    pre_effect.phase = "execute"
                                    pre_effect.effect_started = True
                                    try:
                                        raw_result = tool.execute(**tool_call.arguments)
                                    except BaseException as error:
                                        primary_execution_error = error
                                        raise
                                    tool_returned = True
                except BaseException as error:
                    if primary_execution_error is not None:
                        if error is not primary_execution_error:
                            self._capture_post_effect_failure(
                                failures,
                                "execution_cleanup",
                                error,
                            )
                        raise primary_execution_error
                    if not tool_returned:
                        raise
                    self._capture_post_effect_failure(
                        failures,
                        "execution_cleanup",
                        error,
                    )
                if primary_execution_error is not None:
                    # A compatibility context manager may suppress the tool's
                    # exception. The tool failure remains the primary fact.
                    raise primary_execution_error
            finally:
                execution_seconds = time.monotonic() - execution_started
                try:
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
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "execute_monitor",
                        error,
                    )
                try:
                    git_monitor = getattr(self.agent, "git_monitor", None)
                    if git_monitor is not None and (
                        getattr(tool, "effect_class", None)
                        in {"filesystem_mutation", "process_execution"}
                        or tool_call.name == "shell_session"
                    ):
                        git_monitor.invalidate()
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "git_invalidate",
                        error,
                    )
            outcome = _coerce_returned_outcome(raw_result, execution_seconds)
            authoritative_outcome = outcome
            if approval_workspace_changes:
                try:
                    change_report = "\n\n".join(
                        item for item in approval_workspace_changes if item
                    )
                    notice = (
                        "[workspace changed while approval was pending; "
                        "preview was refreshed]"
                    )
                    if change_report:
                        notice += f"\n{change_report}"
                    outcome = replace(
                        outcome.with_model_projection(
                            f"{outcome.model_text}\n\n{notice}",
                            truncation=outcome.truncation,
                            archive_reference=outcome.archive_reference,
                        ),
                        metadata=MappingProxyType(
                            {
                                **outcome.metadata,
                                "workspace_changed_during_approval": True,
                            }
                        ),
                    )
                    authoritative_outcome = outcome
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "approval_projection",
                        error,
                    )
            try:
                if (shell_cwd := getattr(tool, "_cwd", None)) is not None:
                    self.agent.runtime_working_directory = str(shell_cwd)
            except BaseException as error:
                self._capture_post_effect_failure(
                    failures,
                    "working_directory_sync",
                    error,
                )

            after_context: AfterToolExecuteContext | None = None
            try:
                after_context = AfterToolExecuteContext(
                    hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                    agent_id=self.agent.agent_id,
                    session_generation=self.agent.session_generation,
                    session_id=self.agent.current_session_id,
                    turn_id=self.agent._current_turn_id,
                    # No copy needed here: no extension code runs between this
                    # construction and the transform/observer hand-offs below,
                    # and each of those receives its own deep copy.
                    tool_call=tool_call,
                    result=outcome.model_text,
                    outcome=outcome,
                    round_index=self.agent.state.current_round,
                    cancellation=cancellation,
                )
            except BaseException as error:
                self._capture_post_effect_failure(
                    failures,
                    "after_tool_context",
                    error,
                )

            if after_context is not None:
                transform_input_outcome = outcome
                try:
                    transformed_context = (
                        self.agent.extension_runtime.process_tool_outcome(
                            replace(
                                after_context,
                                tool_call=deepcopy(after_context.tool_call),
                            )
                        )
                    )
                    outcome = _accepted_after_transform(
                        after_context,
                        transformed_context,
                    )
                    if reviewed_diff is not None and (
                        outcome.diff is not None
                        and outcome.diff.unified == reviewed_diff
                    ):
                        outcome = replace(
                            outcome,
                            metadata=MappingProxyType(
                                {**outcome.metadata, "diff_reviewed": True}
                            ),
                        )
                    authoritative_outcome = outcome
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "after_tool_transform",
                        error,
                    )
                    outcome = transform_input_outcome
                    authoritative_outcome = outcome

                try:
                    observer_context = replace(
                        after_context,
                        tool_call=deepcopy(tool_call),
                        result=outcome.model_text,
                        outcome=outcome,
                    )
                    observer_diagnostics = self.agent.extension_runtime.observe(
                        HookPoint.AFTER_TOOL_EXECUTE, observer_context
                    )
                    _record_hook_diagnostics(
                        failures,
                        observer_diagnostics,
                        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                        default_phase="after_tool_observer",
                    )
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "after_tool_observer",
                        error,
                    )
            if outcome.archive_reference is not None:
                try:
                    self.agent.history_ledger.append(
                        "artifact_stored",
                        {
                            "tool_call_id": tc.id,
                            "tool_name": tool_call.name,
                            "artifact": {
                                "path": outcome.archive_reference.path,
                                "media_type": outcome.archive_reference.media_type,
                                "checksum_sha256": (
                                    outcome.archive_reference.checksum_sha256
                                ),
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
                            f"{self.agent._current_turn_id}:"
                            f"{self.agent.state.current_round}"
                            if self.agent._current_turn_id is not None
                            else None
                        ),
                        artifact_refs=(outcome.archive_reference.path,),
                    )
                except BaseException as error:
                    self._capture_post_effect_failure(
                        failures,
                        "artifact_ledger",
                        error,
                    )
            return outcome
        except BaseException as error:
            if not pre_effect.effect_started:
                raise

            if not isinstance(error, Exception):
                self._request_stop_safely(failures)
            result_failure = tool_returned and (
                isinstance(
                    error,
                    (InvalidToolOutcomeProtocol, InvalidToolResultProjection),
                )
                or (authoritative_outcome is None and not isinstance(error, Exception))
            )
            if authoritative_outcome is None and tool_returned:
                if result_failure:
                    authoritative_outcome = _completed_result_failure(error)
                else:
                    try:
                        authoritative_outcome = _coerce_returned_outcome(
                            raw_result,
                            execution_seconds,
                        )
                    except BaseException as result_error:
                        if not isinstance(result_error, Exception):
                            self._request_stop_safely(failures)
                        authoritative_outcome = _completed_result_failure(result_error)
            if authoritative_outcome is not None:
                if not result_failure:
                    self._capture_post_effect_failure(
                        failures,
                        "post_effect",
                        error,
                    )
                return authoritative_outcome
            return self._execution_failure_outcome(pre_effect.phase, error)

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
        baseline_error: BaseException | None = None
        try:
            baseline = (
                self._round_interrupt_epoch()
                if interrupt_baseline is None
                else interrupt_baseline
            )
        except BaseException as error:
            baseline = 0
            baseline_error = error
        results = [""] * len(tool_calls)
        resolved: list[tuple[object, BaseException | None, bool]] = []
        for tool_call in tool_calls:
            if baseline_error is not None:
                resolved.append((None, baseline_error, False))
                continue
            try:
                tool = self.agent.get_tool(tool_call.name)
                parallel_safe = bool(
                    tool is not None and getattr(tool, "parallel_safe", False)
                )
            except BaseException as error:
                resolved.append((None, error, False))
            else:
                resolved.append((tool, None, parallel_safe))

        def execute_submitted(
            index: int,
            scheduler_error: BaseException | None = None,
        ) -> str:
            tool, resolution_error, _ = resolved[index]
            return self.execute(
                tool_calls[index],
                interrupt_baseline=(None if scheduler_error is not None else baseline),
                _resolved_tool=tool,
                _resolution_error=(
                    scheduler_error if scheduler_error is not None else resolution_error
                ),
            )

        def publish_scheduler_failure(
            index: int,
            error: BaseException,
            phase: str,
            *,
            effect_started: bool,
        ) -> str:
            failures = _PostEffectFailureCollector()
            if not isinstance(error, Exception):
                self._request_stop_safely(failures)
            outcome = (
                self._execution_failure_outcome(phase, error)
                if effect_started
                else self._pre_effect_failure_outcome(
                    phase,
                    error,
                    cancelled=isinstance(error, KeyboardInterrupt),
                )
            )
            if failures:
                self._emit_post_effect_diagnostic(
                    tool_calls[index], failures.snapshot()[0], failures
                )
            return self._publish_post_effect_outcome(
                tool_calls[index],
                tool_calls[index].name,
                outcome,
                failures,
            ).model_text

        def execute_attempt(
            index: int,
            attempt: concurrent.futures.Future[str],
        ) -> str:
            if not attempt.set_running_or_notify_cancel():
                return ""
            try:
                result = execute_submitted(index)
            except BaseException as error:
                attempt.set_exception(error)
                raise
            attempt.set_result(result)
            return result

        def settle_failed_attempt(
            index: int,
            attempt: concurrent.futures.Future[str],
            boundary_error: BaseException,
            phase: str,
        ) -> str:
            if attempt.cancel():
                return publish_scheduler_failure(
                    index,
                    boundary_error,
                    phase,
                    effect_started=False,
                )
            self._queue_batch_runtime_failure(
                phase,
                boundary_error,
                tool_count=1,
            )
            try:
                return attempt.result()
            except BaseException as error:
                return publish_scheduler_failure(
                    index,
                    error,
                    phase,
                    effect_started=True,
                )

        index = 0
        while index < len(tool_calls):
            if not resolved[index][2]:
                results[index] = execute_submitted(index)
                index += 1
                continue

            end = index + 1
            while end < len(tool_calls) and resolved[end][2]:
                end += 1
            if end - index == 1:
                results[index] = execute_submitted(index)
            else:
                try:
                    pool = concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(8, end - index)
                    )
                    entered_pool = pool.__enter__()
                except BaseException as error:
                    for offset in range(index, end):
                        results[offset] = publish_scheduler_failure(
                            offset,
                            error,
                            "parallel_pool",
                            effect_started=False,
                        )
                    index = end
                    continue

                futures: dict[
                    int,
                    tuple[
                        concurrent.futures.Future[str],
                        concurrent.futures.Future[str],
                    ],
                ] = {}
                submit_error: BaseException | None = None
                next_offset = index
                for offset in range(index, end):
                    attempt: concurrent.futures.Future[str] = (
                        concurrent.futures.Future()
                    )
                    try:
                        future = entered_pool.submit(
                            execute_attempt,
                            offset,
                            attempt,
                        )
                    except BaseException as error:
                        submit_error = error
                        results[offset] = settle_failed_attempt(
                            offset,
                            attempt,
                            error,
                            "parallel_submit",
                        )
                        next_offset = offset + 1
                        break
                    futures[offset] = (future, attempt)
                    next_offset = offset + 1

                for offset, (future, attempt) in futures.items():
                    try:
                        results[offset] = future.result()
                    except BaseException as error:
                        results[offset] = settle_failed_attempt(
                            offset,
                            attempt,
                            error,
                            "parallel_future",
                        )

                exit_error: BaseException | None = None
                try:
                    pool.__exit__(None, None, None)
                except BaseException as error:
                    exit_error = error

                if submit_error is not None:
                    for offset in range(next_offset, end):
                        results[offset] = publish_scheduler_failure(
                            offset,
                            submit_error,
                            "parallel_submit",
                            effect_started=False,
                        )
                if exit_error is not None:
                    self._queue_batch_runtime_failure(
                        "parallel_scheduler_cleanup",
                        exit_error,
                        tool_count=end - index,
                    )
            index = end
        return results
