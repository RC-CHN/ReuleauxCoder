"""Core agent - the main agent class."""

from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional, List
from dataclasses import dataclass, field
import threading
import uuid

if TYPE_CHECKING:
    from reuleauxcoder.domain.approval import ApprovalProvider
    from reuleauxcoder.services.llm.client import LLM
    from reuleauxcoder.extensions.tools.base import Tool
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.domain.extensions import ToolExtensionRuntime

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ModeConfig
from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.domain.hooks import HookBase, HookDiagnostic, HookPoint, HookRegistry
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.domain.plan import PlanController
from reuleauxcoder.domain.extensions import HookExtensionAdapter, LifecycleCoordinator
from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency
from reuleauxcoder.extensions.subagent.manager import get_subagent_manager
from reuleauxcoder.infrastructure.platform import get_platform_info
from reuleauxcoder.services.prompt.builder import system_prompt


@dataclass
class AgentState:
    """State of the agent."""

    messages: list[dict] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    current_round: int = 0
    total_tool_calls: int = 0


class Agent:
    """The main agent class - orchestrates LLM and tools."""

    def __init__(
        self,
        llm: "LLM",
        tools: Optional[List["Tool"]] = None,
        config: "Config" | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        max_tool_calls: int | None = None,
        max_total_tokens: int | None = None,
        hook_registry: HookRegistry | None = None,
        approval_provider: "ApprovalProvider" | None = None,
        available_modes: dict[str, ModeConfig] | None = None,
        active_mode: str | None = None,
        loop: AgentLoop | None = None,
        executor: ToolExecutor | None = None,
        extension_runtime: "ToolExtensionRuntime" | None = None,
        agent_id: str | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else []
        self.config = config
        self.runtime_config = config
        self.max_context_tokens = max_context_tokens
        self.max_rounds = max_rounds
        self.max_tool_calls = max_tool_calls
        self.max_total_tokens = max_total_tokens

        # Explicit runtime bindings. Adapters may replace these values, but
        # must not inject new attributes dynamically.
        self.agent_id = agent_id or uuid.uuid4().hex
        self.session_generation = 0
        self.current_session_id: str | None = None
        self.session_fingerprint: str | None = getattr(
            config, "session_fingerprint", None
        )
        self.session_approval_rules: list = []
        self.active_main_model_profile: str | None = getattr(
            config, "active_main_model_profile", None
        )
        self.active_sub_model_profile: str | None = getattr(
            config, "active_sub_model_profile", None
        )
        self.runtime_working_directory: str | None = None
        self.mcp_manager = None
        self.skills_service = None
        self.skills_catalog: str = ""
        self.extension_manager = None
        self.extension_scope = None
        self.lsp_manager = None
        self.relay_server = None
        self._subagent_manager = None
        self.subagent_depth = 0
        self.strict_tool_scope = False
        self.ui_interactor = None
        self._subagent_approval_lock = None
        self._external_message_source = None
        self._steering_lock = threading.Lock()
        self._pending_user_steering: list[tuple[str, int, str]] = []
        self._accepting_user_steering = False

        # Mode state
        self.available_modes: dict[str, ModeConfig] = dict(available_modes or {})
        self.active_mode: str | None = None

        # Reasoning / thinking state
        self.last_reasoning_content: str | None = None
        self.reasoning_display_mode: str = "quiet"  # "quiet" | "inline"

        # State
        self.state = AgentState()
        self._state_lock = threading.Lock()
        # All RuntimeContextView mutations share this revision boundary. Worker
        # callbacks only enqueue ledger/mailbox data, so a candidate rewrite can
        # be committed without overwriting steering or child results.
        self._context_revision_lock = threading.RLock()
        self._context_revision = 0
        self._stop_event = threading.Event()
        self._current_turn_id: str | None = None
        self.history_ledger = HistoryLedger(
            generation=self.session_generation, agent_id=self.agent_id
        )
        self.history_completeness = "complete"
        self.replay_envelope = None
        self.request_envelopes: list = []
        self._restored_replay_envelope = None
        self._resume_runtime_descriptor_hash: str | None = None
        self.plan_controller = PlanController(self)
        self._session_persist_callback = None
        self._control_plane_recovery_required = False
        for tool in self.tools:
            backend = getattr(tool, "backend", None)
            backend_context = getattr(backend, "context", None)
            if backend_context is not None:
                backend_context.cancellation_event = self._stop_event

        # Context manager
        context_cfg = getattr(config, "context", None)
        if context_cfg:
            self.context = ContextManager(
                max_tokens=max_context_tokens,
                snip_keep_recent_tools=context_cfg.snip_keep_recent_tools,
                snip_threshold_chars=context_cfg.snip_threshold_chars,
                snip_min_lines=context_cfg.snip_min_lines,
                summarize_keep_recent_turns=context_cfg.summarize_keep_recent_turns,
                token_fudge_factor=getattr(context_cfg, "token_fudge_factor", 1.1),
                reserved_output_tokens=getattr(context_cfg, "reserved_output_tokens", 8192),
                fixed_prompt_tokens=getattr(context_cfg, "fixed_prompt_tokens", 0),
                tool_schema_tokens=getattr(context_cfg, "tool_schema_tokens", 0),
                safety_margin_tokens=getattr(context_cfg, "safety_margin_tokens", 2048),
            )
        else:
            self.context = ContextManager(max_tokens=max_context_tokens)

        # Event handlers are available before hook diagnostics are wired.
        self._event_handlers: List[Callable[[AgentEvent], None]] = []

        # Hook runtime
        self.hook_registry = hook_registry or HookRegistry()
        self.hook_registry.set_diagnostic_sink(self._emit_hook_diagnostic)
        self.extension_runtime = extension_runtime or HookExtensionAdapter(
            self.hook_registry
        )
        self.lifecycle = LifecycleCoordinator(
            self.hook_registry,
            notification_sink=self._emit_runtime_diagnostic,
        )

        # Execution components
        self.approval_provider = approval_provider
        if loop is not None:
            self._loop = loop
        else:
            shell = get_platform_info().get_preferred_shell().value
            self._loop = AgentLoop(self, prompt_fn=system_prompt, shell_name=shell)
        self._executor = executor or ToolExecutor(self)

        # Buffer for sub-agent injections that arrive during active tool execution.
        # Deferred to avoid interleaving assistant messages between a tool_calls
        # message and its corresponding tool responses (violates API contract).
        self._pending_subagent_injections: list[tuple] = []

        # Activate initial mode if available
        if self.available_modes:
            default_mode = active_mode or next(iter(self.available_modes.keys()), None)
            if default_mode in self.available_modes:
                self.active_mode = default_mode

    def _collect_pending_tool_calls(self) -> list[tuple[str, str]]:
        """Collect assistant tool calls that do not yet have matching tool outputs."""
        completed_ids = {
            msg.get("tool_call_id")
            for msg in self.state.messages
            if msg.get("role") == "tool" and msg.get("tool_call_id")
        }

        pending: list[tuple[str, str]] = []
        seen: set[str] = set()
        for msg in self.state.messages:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                tc_name = fn.get("name") or "unknown_tool"
                if not tc_id or tc_id in completed_ids or tc_id in seen:
                    continue
                pending.append((tc_id, tc_name))
                seen.add(tc_id)

        return pending

    def reconcile_pending_tool_calls(self, reason: str | None = None) -> int:
        """Repair adjacency and synthesize any dangling tool outputs.

        Returns the number of synthetic tool results appended.
        """
        suffix = f" {reason}" if reason else ""
        repaired, synthesized = reconcile_tool_call_adjacency(
            self.state.messages,
            missing_content=lambda _tool_call_id, tool_name: (
                f"Tool '{tool_name}' interrupted before returning output.{suffix}"
            ),
        )
        if repaired != self.state.messages:
            self._replace_context_messages(
                repaired,
                reason=reason or "tool adjacency reconciliation",
            )
        return synthesized

    def _append_message(self, message: dict, *, source: str) -> None:
        with self._context_revision_lock:
            api_round_id = (
                f"{self._current_turn_id}:{self.state.current_round}"
                if self._current_turn_id is not None
                else None
            )
            self.history_ledger.append_message(
                message,
                source=source,
                agent_id=self.agent_id,
                turn_id=self._current_turn_id,
                api_round_id=api_round_id,
            )
            self.state.messages.append(message)
            self._context_revision += 1
            self.persist_runtime_snapshot()

    def bind_session_persistence(self, *, events_path, callback) -> None:
        self.history_ledger.bind_context(
            session_id=getattr(self, "current_session_id", None),
            agent_id=self.agent_id,
        )
        self.history_ledger.bind_jsonl(events_path)
        self._session_persist_callback = callback

    def unbind_session_persistence(self) -> None:
        self._session_persist_callback = None

    def persist_runtime_snapshot(self) -> None:
        callback = self._session_persist_callback
        if callable(callback):
            callback()

    def recover_control_plane_if_required(self) -> bool:
        """Recover ledger-first control commits before another model request."""
        if not self._control_plane_recovery_required:
            return True
        self.plan_controller.recover_from_ledger()
        callback = self._session_persist_callback
        try:
            if callable(callback):
                callback()
        except Exception:
            return False
        self._control_plane_recovery_required = False
        self.history_ledger.append(
            "control_state_recovered",
            {
                "plan_revision": self.plan_controller.state.revision,
                "progress_revision": self.plan_controller.progress.revision,
            },
            agent_id=self.agent_id,
            turn_id=self._current_turn_id,
        )
        return True

    def _replace_context_messages(
        self,
        messages: list[dict],
        *,
        reason: str,
        checkpoint_id: str | None = None,
        record: bool = True,
    ) -> None:
        with self._context_revision_lock:
            replacement = [dict(message) for message in messages]
            if record:
                self.history_ledger.append_context_view(
                    replacement,
                    reason=reason,
                    history_version=self.context.history_version,
                    checkpoint_id=checkpoint_id,
                )
            self.state.messages[:] = replacement
            self._context_revision += 1
            if record:
                self.persist_runtime_snapshot()

    def maybe_compress_context(self, llm, *, reason: str) -> bool:
        with self._context_revision_lock:
            source_revision = self._context_revision
            candidate = [dict(message) for message in self.state.messages]
            if not self.context.maybe_compress(
                candidate,
                llm,
                history_events=self.history_ledger.events,
            ):
                return False
            if source_revision != self._context_revision:
                self.context._reset_compression_state()
                return False
            checkpoint = self.context.checkpoints[-1]
            self._replace_context_messages(
                candidate,
                reason=reason,
                checkpoint_id=checkpoint.id,
            )
            return True

    def force_compress_context(self, strategy: str, llm) -> bool:
        with self._context_revision_lock:
            candidate = [dict(message) for message in self.state.messages]
            if not self.context.force_compress(
                candidate,
                strategy,
                llm,
                history_events=self.history_ledger.events,
            ):
                return False
            checkpoint = self.context.checkpoints[-1]
            self._replace_context_messages(
                candidate,
                reason="manual compact command",
                checkpoint_id=checkpoint.id,
            )
            return True

    def restore_history_runtime(self, session) -> None:
        self.history_ledger = HistoryLedger(
            getattr(session, "history_events", ()),
            generation=self.session_generation,
            session_id=getattr(session, "id", None),
            agent_id=self.agent_id,
        )
        self._session_persist_callback = None
        self.replay_envelope = getattr(session, "replay_envelope", None)
        self._restored_replay_envelope = self.replay_envelope
        self._resume_runtime_descriptor_hash = None
        self.request_envelopes = list(getattr(session, "request_envelopes", ()))
        self.history_completeness = getattr(
            session, "history_completeness", "legacy_compacted_or_unknown"
        )
        self.context.clear_usage_observations()
        for event in self.history_ledger.events[-100:]:
            if event.kind != "usage_observed":
                continue
            payload = event.payload
            try:
                self.context.observe_usage(
                    actual_prompt_tokens=int(payload["actual_prompt_tokens"]),
                    cached_input_tokens=(
                        int(payload["cached_input_tokens"])
                        if payload.get("cached_input_tokens") is not None
                        else None
                    ),
                    local_request_estimate=int(payload["local_request_estimate"]),
                    local_history_estimate=int(payload["local_history_estimate"]),
                    request_boundary=str(payload["request_boundary"]),
                    model_profile=str(payload["model_profile"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        if self.replay_envelope is not None:
            self.context.restore_replay_state(
                history_version=self.replay_envelope.history_version,
                cache_epoch=self.replay_envelope.cache_epoch,
            )
        self.context.restore_checkpoints(
            list(getattr(session, "checkpoints", ()))
        )
        self._replace_context_messages(
            list(session.messages), reason="session resume", record=False
        )
        manager = get_subagent_manager(self)
        manager.restore_from_history(self, self.history_ledger.events)

    def start_new_history(self) -> None:
        self.history_ledger = HistoryLedger(
            generation=self.session_generation,
            session_id=getattr(self, "current_session_id", None),
            agent_id=self.agent_id,
        )
        self.replay_envelope = None
        self._restored_replay_envelope = None
        self._resume_runtime_descriptor_hash = None
        self.request_envelopes = []
        self.history_completeness = "complete"
        self._session_persist_callback = None
        self.context.restore_replay_state(history_version=0, cache_epoch=0)
        self.plan_controller.reset()

    def request_stop(self) -> None:
        """Request cooperative stop for the current/next agent loop iteration."""
        self._stop_event.set()

    def clear_stop_request(self) -> None:
        """Clear any pending cooperative stop request."""
        self._stop_event.clear()

    def stop_requested(self) -> bool:
        """Return True when cooperative stop has been requested."""
        return self._stop_event.is_set()

    def submit_user_steering(self, text: str) -> bool:
        """Queue user direction for the next protocol-safe inference boundary."""
        content = text.strip()
        if not content:
            return False
        with self._steering_lock:
            if not self._accepting_user_steering or self._current_turn_id is None:
                return False
            generation = self.session_generation
            turn_id = self._current_turn_id
            event = self.history_ledger.append_message(
                {"role": "user", "content": content},
                source="user_steering",
                agent_id=self.agent_id,
                turn_id=turn_id,
                api_round_id=f"{turn_id}:{self.state.current_round}",
            )
            self._pending_user_steering.append(
                (content, generation, event.event_id)
            )
        self.persist_runtime_snapshot()
        return True

    def _has_user_steering(self) -> bool:
        with self._steering_lock:
            return any(
                generation == self.session_generation
                for _, generation, _ in self._pending_user_steering
            )

    def _drain_user_steering(self) -> int:
        """Project already-ledgered user messages into the runtime context."""
        with self._steering_lock:
            pending = self._pending_user_steering
            self._pending_user_steering = []
        accepted = [
            content
            for content, generation, _event_id in pending
            if generation == self.session_generation
        ]
        if accepted:
            with self._context_revision_lock:
                self.state.messages.extend(
                    {"role": "user", "content": content} for content in accepted
                )
                self._context_revision += 1
                self.persist_runtime_snapshot()
        return len(accepted)

    def get_active_mode_config(self) -> ModeConfig | None:
        """Return active mode config if mode is enabled."""
        if not self.active_mode:
            return None
        return self.available_modes.get(self.active_mode)

    def set_mode(self, mode_name: str) -> None:
        """Switch active mode.

        Raises:
            ValueError: If mode does not exist.
        """
        if mode_name not in self.available_modes:
            raise ValueError(f"Unknown mode: {mode_name}")
        self.active_mode = mode_name

    def get_active_tools(self) -> list["Tool"]:
        """Return tools visible to the LLM in current mode."""
        mode = self.get_active_mode_config()
        if mode is None:
            return self.tools

        if not mode.tools or "*" in mode.tools:
            return self.tools

        allowed = set(mode.tools)
        return [tool for tool in self.tools if tool.name in allowed]

    def get_blocked_tools(self) -> list["Tool"]:
        """Return tools hidden/blocked by current mode."""
        mode = self.get_active_mode_config()
        if mode is None or not mode.tools or "*" in mode.tools:
            return []
        allowed = set(mode.tools)
        return [tool for tool in self.tools if tool.name not in allowed]

    def suggest_modes_for_tool(self, tool_name: str) -> list[str]:
        """Return mode names that allow the given tool."""
        suggestions: list[str] = []
        for mode_name, mode in self.available_modes.items():
            if not mode.tools or "*" in mode.tools or tool_name in set(mode.tools):
                suggestions.append(mode_name)
        return suggestions

    def is_tool_allowed_in_mode(self, tool_name: str) -> bool:
        """Return whether a tool can execute in current mode."""
        mode = self.get_active_mode_config()
        if mode is None:
            return True
        if not mode.tools or "*" in mode.tools:
            return True
        return tool_name in set(mode.tools)

    def add_event_handler(self, handler: Callable[[AgentEvent], None]) -> None:
        """Add an event handler."""
        self._event_handlers.append(handler)

    def _emit_event(self, event: AgentEvent) -> None:
        """Emit an event to all handlers."""
        if event.agent_id is None:
            event.agent_id = self.agent_id
        if event.session_generation is None:
            event.session_generation = self.session_generation
        if event.session_id is None:
            event.session_id = getattr(self, "current_session_id", None)
        if event.turn_id is None:
            event.turn_id = self._current_turn_id
        self._record_runtime_fact(event)
        for handler in self._event_handlers:
            try:
                handler(event)
            except Exception:
                pass  # Don't let handler errors break execution

    def _record_runtime_fact(self, event: AgentEvent) -> None:
        """Persist correctness-relevant runtime facts; omit high-rate chunks."""
        if event.event_type is AgentEventType.TOOL_CALL_START:
            self.history_ledger.append(
                "tool_call_started",
                {
                    "tool_call_id": event.correlation_id,
                    "tool_name": event.tool_name,
                    "arguments": dict(event.tool_args or {}),
                },
                agent_id=event.agent_id,
                turn_id=event.turn_id,
                api_round_id=(
                    f"{event.turn_id}:{self.state.current_round}"
                    if event.turn_id
                    else None
                ),
            )
            return
        if event.event_type is not AgentEventType.TOOL_CALL_END:
            return
        outcome = event.tool_outcome
        if outcome is None:
            return
        archive = outcome.archive_reference
        truncation = outcome.truncation
        self.history_ledger.append(
            "tool_call_finished",
            {
                "tool_call_id": event.correlation_id,
                "tool_name": event.tool_name,
                "status": outcome.status.value,
                "success": outcome.success,
                "summary": outcome.summary,
                "exit_code": outcome.exit_code,
                "duration_seconds": outcome.duration_seconds,
                "error_kind": (
                    outcome.error_kind.value if outcome.error_kind else None
                ),
                "truncation": (
                    {
                        "original_chars": truncation.original_chars,
                        "original_lines": truncation.original_lines,
                        "retained_chars": truncation.retained_chars,
                        "retained_lines": truncation.retained_lines,
                        "strategy": truncation.strategy,
                    }
                    if truncation
                    else None
                ),
                "archive": (
                    {
                        "path": archive.path,
                        "media_type": archive.media_type,
                        "checksum_sha256": archive.checksum_sha256,
                        "size_bytes": archive.size_bytes,
                    }
                    if archive
                    else None
                ),
            },
            agent_id=event.agent_id,
            turn_id=event.turn_id,
            api_round_id=(
                f"{event.turn_id}:{self.state.current_round}"
                if event.turn_id
                else None
            ),
            artifact_refs=((archive.path,) if archive else ()),
        )

    def _emit_hook_diagnostic(self, diagnostic: HookDiagnostic) -> None:
        self._emit_runtime_diagnostic(
            f"Hook '{diagnostic.hook_name}' failed during "
            f"{diagnostic.hook_point.value}: {diagnostic.message}",
            "hook.failure",
            diagnostic.severity,
            {
                "hook_name": diagnostic.hook_name,
                "hook_point": diagnostic.hook_point.value,
                "hook_kind": diagnostic.hook_kind.value,
            },
        )

    def _emit_runtime_diagnostic(
        self, message: str, code: str, severity: str, details: dict
    ) -> None:
        self._emit_event(
            AgentEvent.diagnostic(
                message,
                code=code,
                severity=severity,
                details=details,
            )
        )

    def register_hook(self, hook_point: HookPoint, hook: HookBase[object]) -> None:
        """Register a hook on the agent-scoped hook registry."""
        self.hook_registry.register(hook_point, hook)

    def list_hooks(self, hook_point: HookPoint | None = None) -> dict[str, list[str]]:
        """List registered hooks from the agent-scoped hook registry."""
        return self.hook_registry.list_hooks(hook_point)

    def add_tools(self, tools: List["Tool"]) -> None:
        """Add additional tools."""
        self.tools.extend(tools)

    def get_tool(self, name: str) -> Optional["Tool"]:
        """Look up a tool by name."""
        for t in self.tools:
            if t.name == name:
                return t
        return None

    @staticmethod
    def _format_subagent_job_message(job) -> tuple[str, bool]:
        if job.status == "completed":
            result = getattr(job, "structured_result", None)
            result_text = (
                result.model_text() if result is not None else job.result or "(empty)"
            )
            content = (
                "[Sub-agent result notification]\n"
                f"id={job.id}\n"
                f"mode={job.mode}\n"
                f"task={job.task}\n\n"
                f"{result_text}\n"
                "[/Sub-agent result notification]"
            )
            return content, True

        structured = getattr(job, "structured_result", None)
        detail = (
            structured.model_text()
            if structured is not None
            else job.error or "unknown error"
        )
        error_text = (
            "[Background sub-agent failed]\n"
            f"id={job.id}\n"
            f"mode={job.mode}\n"
            f"task={job.task}\n\n"
            f"{detail}\n"
            "[/Background sub-agent failed]"
        )
        return error_text, False

    def _emit_subagent_completion_events(
        self, job, content: str, success: bool
    ) -> None:
        """Emit UI events for a completed/failed sub-agent job."""
        if success:
            self._emit_event(
                AgentEvent.subagent_completed(
                    job_id=job.id,
                    mode=job.mode,
                    task=job.task,
                    status=job.status,
                    result=job.result,
                )
            )
        else:
            self._emit_event(
                AgentEvent.subagent_completed(
                    job_id=job.id,
                    mode=job.mode,
                    task=job.task,
                    status=job.status,
                    error=job.error,
                )
            )

    def inject_subagent_job_result(self, job) -> bool:
        """Inject one finished sub-agent job into parent history.

        If there are unresolved tool calls in the message history (e.g. the
        main loop is mid-execution of a prior tool_calls message), the
        injection is buffered to avoid interleaving assistant messages
        between a tool_calls message and its tool responses — which would
        violate the LLM API contract.
        """
        parent_agent_id = getattr(job, "parent_agent_id", None)
        if parent_agent_id is not None and parent_agent_id != self.agent_id:
            return False
        if getattr(job, "generation", self.session_generation) != self.session_generation:
            return False
        manager = getattr(self, "_subagent_manager", None)
        if (
            manager is not None
            and getattr(job, "generation", manager.generation) != manager.generation
        ):
            return False
        with self._state_lock:
            if getattr(job, "injected_to_parent", False):
                return False
            job.injected_to_parent = True
            content, success = self._format_subagent_job_message(job)

            # Defer if there are unresolved tool calls in the message history.
            if self._collect_pending_tool_calls():
                self._pending_subagent_injections.append((job, content, success))
                return True

            self._append_message(
                {"role": "system", "content": content}, source="subagent_result"
            )

        self._emit_subagent_completion_events(job, content, success)
        return True

    def _flush_pending_subagent_injections(self) -> int:
        """Flush buffered sub-agent injections. Safe to call after tool
        execution when no unresolved tool_calls remain.

        Returns the number of injections flushed.
        """
        pending: list[tuple] = []
        with self._state_lock:
            if not self._pending_subagent_injections:
                return 0
            pending = self._pending_subagent_injections[:]
            self._pending_subagent_injections.clear()
            for job, content, _success in pending:
                self._append_message(
                    {"role": "system", "content": content},
                    source="subagent_deferred",
                )

        for job, content, success in pending:
            if job is not None:
                self._emit_subagent_completion_events(job, content, success)
        return len(pending)

    def inject_subagent_communication(self, item) -> bool:
        """Commit one typed child-to-parent message at a protocol-safe boundary."""
        if getattr(item, "recipient_agent_id", None) != self.agent_id:
            return False
        if getattr(item, "generation", None) != self.session_generation:
            return False
        content = (
            "[Sub-agent context item]\n"
            f"item_id={item.item_id}\n"
            f"seq={item.seq}\n"
            f"kind={item.kind}\n"
            f"reply_to={item.reply_to or '-'}\n"
            f"sender_agent_id={item.sender_agent_id}\n"
            f"sender_job_id={item.sender_job_id or '-'}\n\n"
            f"content_hash={item.content_hash or '-'}\n\n"
            f"{item.content}\n"
            "[/Sub-agent context item]"
        )
        with self._state_lock:
            if self._collect_pending_tool_calls():
                return False
            self._append_message(
                {"role": "system", "content": content},
                source="subagent_communication",
            )
        return True

    def _inject_completed_subagent_jobs(self) -> int:
        """Drain typed child messages/results into parent history in sequence order."""
        if self.subagent_depth > 0:
            return 0
        try:
            manager = get_subagent_manager(self)
        except Exception:
            return 0

        jobs = manager.drain_completed_for_parent(
            parent_state_lock=self._state_lock,
            parent_agent_id=self.agent_id,
        )
        communications = manager.drain_parent_messages(self.agent_id)
        file_owners: dict[str, list[str]] = {}
        for job in jobs:
            structured = getattr(job, "structured_result", None)
            if job.mode != "execute" or structured is None:
                continue
            for path in sorted(set(structured.files)):
                file_owners.setdefault(path, []).append(job.id)
        conflicts = {
            path: tuple(sorted(owners))
            for path, owners in file_owners.items()
            if len(owners) > 1
        }
        if conflicts:
            conflict_lines = [
                f"- {path}: {', '.join(owners)}"
                for path, owners in sorted(conflicts.items())
            ]
            self._append_message(
                {
                    "role": "system",
                    "content": (
                        "[Sub-agent conflict]\n"
                        "Multiple execute jobs report overlapping files; inspect and "
                        "reconcile before accepting their changes.\n"
                        + "\n".join(conflict_lines)
                        + "\n[/Sub-agent conflict]"
                    ),
                },
                source="subagent_conflict",
            )
            self._emit_runtime_diagnostic(
                "Sub-agent file overlap requires reconciliation.",
                "subagent.conflict",
                "warning",
                {"files": conflicts},
            )
        ordered = [
            (getattr(job, "completion_seq", None) or 2**63, "job", job)
            for job in jobs
        ] + [(item.seq, "communication", item) for item in communications]

        injected = 0
        for _seq, kind, item in sorted(ordered, key=lambda entry: entry[0]):
            accepted = (
                self.inject_subagent_job_result(item)
                if kind == "job"
                else self.inject_subagent_communication(item)
            )
            if accepted:
                if kind == "communication":
                    manager.acknowledge_parent_message(item.item_id)
                injected += 1
            elif kind == "communication":
                manager.release_parent_message(item.item_id)
        return injected

    def _wait_for_subagent_activity(self, timeout: float = 0.1) -> bool:
        manager = getattr(self, "_subagent_manager", None)
        if manager is None:
            return False
        return manager.wait_for_parent_activity(self.agent_id, timeout=timeout)

    def _has_subagent_activity(self) -> bool:
        return self._wait_for_subagent_activity(timeout=0.0)

    def chat(self, user_input: str) -> str:
        """Process one user message."""
        self.clear_stop_request()
        self._current_turn_id = uuid.uuid4().hex
        with self._steering_lock:
            self._accepting_user_steering = True
        self.history_ledger.append(
            "agent_lifecycle",
            {"state": "turn_started"},
            agent_id=self.agent_id,
            turn_id=self._current_turn_id,
        )

        # Inject completed background sub-agent results before each new user turn.
        self._inject_completed_subagent_jobs()

        # Repair stale dangling tool calls (e.g. after previous crash/interruption)
        self.reconcile_pending_tool_calls(
            reason="Recovered from previous interrupted turn."
        )

        # Flush any sub-agent injections buffered because of the stale tool
        # calls that were just reconciled above.
        self._flush_pending_subagent_injections()

        self._emit_event(AgentEvent.chat_start(user_input))

        # Add user message
        self._append_message(
            {"role": "user", "content": user_input}, source="user_input"
        )

        # Run the loop
        try:
            while True:
                result = self._loop.run()
                with self._steering_lock:
                    pending = any(
                        generation == self.session_generation
                        for _, generation, _ in self._pending_user_steering
                    )
                    if not pending or self.stop_requested():
                        self._accepting_user_steering = False
                        if self.stop_requested():
                            self._pending_user_steering.clear()
                        break
        except BaseException as e:
            with self._steering_lock:
                self._accepting_user_steering = False
            # Ensure tool-call/response parity before bubbling the failure upward.
            self.reconcile_pending_tool_calls(
                reason=f"Interrupted due to {type(e).__name__}."
            )
            self.history_ledger.append(
                "agent_lifecycle",
                {"state": "turn_failed", "error_type": type(e).__name__},
                agent_id=self.agent_id,
                turn_id=self._current_turn_id,
            )
            raise

        self._emit_event(
            AgentEvent.chat_end(
                result,
                render_response=not getattr(
                    self._loop, "last_response_streamed", False
                ),
            )
        )
        self.history_ledger.append(
            "agent_lifecycle",
            {"state": "turn_completed"},
            agent_id=self.agent_id,
            turn_id=self._current_turn_id,
        )
        self._current_turn_id = None
        return result

    def reset(self) -> None:
        """Clear conversation history."""
        cancel_interactions = getattr(self.ui_interactor, "cancel_all", None)
        if callable(cancel_interactions):
            cancel_interactions(reason="session reset")
        self.session_generation += 1
        self.history_ledger.append(
            "runtime_reset", {"next_generation": self.session_generation}
        )
        self.history_ledger.append(
            "session_lifecycle",
            {"action": "runtime_reset", "generation": self.session_generation},
            agent_id=self.agent_id,
        )
        self.history_ledger.advance_generation(self.session_generation)
        lsp_manager = getattr(self, "lsp_manager", None)
        advance_lsp_generation = getattr(
            lsp_manager, "advance_session_generation", None
        )
        if callable(advance_lsp_generation):
            advance_lsp_generation(self.agent_id, self.session_generation)
        manager = getattr(self, "_subagent_manager", None)
        if manager is not None:
            manager.advance_generation(
                generation=self.session_generation, cancel_pending=True
            )
        self.context.invalidate_replay_prefix()
        self._restored_replay_envelope = None
        self._resume_runtime_descriptor_hash = None
        self.history_ledger.append_context_view(
            [],
            reason="runtime reset",
            history_version=self.context.history_version,
        )
        with self._context_revision_lock:
            self.state.messages.clear()
            self._context_revision += 1
        self.state.total_prompt_tokens = 0
        self.state.total_completion_tokens = 0
        self.state.current_round = 0
        self._current_turn_id = None
        self._pending_subagent_injections.clear()
        with self._steering_lock:
            self._pending_user_steering.clear()
            self._accepting_user_steering = False
        self.plan_controller.reset()
        self._control_plane_recovery_required = False
        self.persist_runtime_snapshot()

    @property
    def messages(self) -> list[dict]:
        """Get messages (for compatibility)."""
        return self.state.messages
