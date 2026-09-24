"""Helpers for session-scoped runtime state persistence and restore."""

from __future__ import annotations

from contextlib import nullcontext
import time

from reuleauxcoder.app.runtime.approval import (
    merge_approval_config,
    refresh_approval_runtime,
)
from reuleauxcoder.app.runtime.model_profiles import apply_main_model_profile
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
    resolve_context_strategies,
)
from reuleauxcoder.domain.session.models import (
    Session,
    SessionRestoreIssue,
    SessionRuntimeState,
)
from reuleauxcoder.infrastructure.persistence.session_store import (
    DEFAULT_SESSION_FINGERPRINT,
)

from reuleauxcoder.app.runtime.snapshot_writer import (
    SessionSnapshotWriter as _LiveSessionPersistence,
    _safe_persistence_error_type,
)

def _request_cooperative_stop(agent: Agent, error: BaseException) -> None:
    if not isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        return
    request_stop = getattr(agent, "request_stop", None)
    if not callable(request_stop):
        return
    try:
        request_stop()
    except BaseException:
        # The bounded incident fallback below remains the durable diagnostic.
        pass


def _retain_persistence_incident(
    agent: Agent,
    phase: str,
    error_type: str,
    ref: str,
) -> None:
    """Retain an observer failure even when the public incident sink is broken."""
    recorder = getattr(agent, "record_runtime_issue", None)
    recorder_error: BaseException | None = None
    if callable(recorder):
        try:
            if recorder(phase, error_type, ref) is not False:
                return
            recorder_error = RuntimeError("runtime issue recorder rejected incident")
        except BaseException as error:
            recorder_error = error
            _request_cooperative_stop(agent, error)
    try:
        # Bypass an overridden public recorder without recursively emitting.
        Agent.record_runtime_issue(agent, phase, error_type, ref)
        if recorder_error is not None:
            Agent.record_runtime_issue(
                agent,
                "runtime_issue_recorder",
                _safe_persistence_error_type(recorder_error),
                ref,
            )
        return
    except BaseException as fallback_error:
        _request_cooperative_stop(agent, fallback_error)

    # Last-resort fact carrier used by the model-facing session issue projection.
    # Preserve existing restore degradation instead of rewriting the primary facts.
    try:
        existing = tuple(getattr(agent, "session_restore_issues", ()))
        original = SessionRestoreIssue(
            phase=phase,
            error_type=error_type,
            ref=ref,
        )
        recorder_issue = SessionRestoreIssue(
            phase="runtime_issue_recorder",
            error_type=(
                _safe_persistence_error_type(recorder_error)
                if recorder_error is not None
                else "RuntimeIssueRecorderError"
            ),
            ref="session_persistence",
        )
        agent.session_restore_issues = (*existing, original, recorder_issue)
    except BaseException as final_error:
        _request_cooperative_stop(agent, final_error)
        agent._control_plane_recovery_required = True


def _record_persistence_bind_failure(
    agent: Agent,
    error: BaseException,
) -> SessionRestoreIssue:
    """Retain one post-switch bind failure without pretending to roll back."""
    issue = SessionRestoreIssue(
        phase="session_persistence_bind",
        error_type=_safe_persistence_error_type(error),
        ref="history_ledger",
    )
    agent._control_plane_recovery_required = True
    _retain_persistence_incident(agent, issue.phase, issue.error_type, issue.ref)
    return issue


def get_session_fingerprint(config: Config, agent: Agent) -> str:
    """Return the current session environment fingerprint."""
    return (
        getattr(agent, "session_fingerprint", None)
        or getattr(config, "session_fingerprint", None)
        or DEFAULT_SESSION_FINGERPRINT
    )


def build_session_persistence_kwargs(agent: Agent) -> dict:
    """Return canonical history/replay state for SessionStore.save."""
    ledger = getattr(agent, "history_ledger", None)
    context = getattr(agent, "context", None)
    return {
        "history_events": list(ledger.events) if ledger is not None else None,
        "history_next_seq_floor": (
            ledger.last_sequence if ledger is not None else 0
        ),
        "replay_envelope": getattr(agent, "replay_envelope", None),
        "request_envelopes": list(getattr(agent, "request_envelopes", ())),
        "history_completeness": getattr(
            agent,
            "history_completeness",
            "complete" if ledger is not None else "legacy_snapshot_only",
        ),
        "checkpoints": list(getattr(context, "checkpoints", ())),
        "restore_issues": list(getattr(agent, "session_restore_issues", ())),
    }


def save_session_snapshot(
    config, agent, store, session_id=None, *, fingerprint=None, is_exit=False,
    incremental=False,
):
    """Save through the live writer; store adapters never allocate live events."""
    scope = getattr(agent, "session_snapshot_scope", None)
    with scope() if callable(scope) else nullcontext():
        commit_exit = getattr(agent, "commit_session_exit", None)
        if is_exit and callable(commit_exit):
            commit_exit()
        return store.save(
            list(agent.messages),
            getattr(agent.llm, "model", config.model),
            session_id or agent.current_session_id,
            is_exit=is_exit and not callable(commit_exit),
            total_prompt_tokens=agent.state.total_prompt_tokens,
            total_completion_tokens=agent.state.total_completion_tokens,
            active_mode=getattr(agent, "active_mode", None),
            runtime_state=build_session_runtime_state(config, agent),
            fingerprint=fingerprint or get_session_fingerprint(config, agent),
            incremental=incremental,
            events_already_persisted=bool(
                getattr(agent, "_session_persist_callback", None)
            ),
            **build_session_persistence_kwargs(agent),
        )


def bind_session_persistence(
    config: Config,
    agent: Agent,
    store,
    session_id: str,
    *,
    fingerprint: str,
    events_path=None,
) -> SessionRestoreIssue | None:
    """Bind live ledger fsync and replay snapshots to the active session."""
    from reuleauxcoder.infrastructure.persistence.images import ImageStore

    if hasattr(agent, "image_store"):
        agent.image_store = ImageStore(store.sessions_dir, config.image)
        if hasattr(agent.llm, "image_store"):
            agent.llm.image_store = agent.image_store
    bind = getattr(agent, "bind_session_persistence", None)
    if not callable(bind):
        agent.current_session_id = session_id
        ledger = getattr(agent, "history_ledger", None)
        bind_context = getattr(ledger, "bind_context", None)
        if callable(bind_context):
            bind_context(
                session_id=session_id,
                agent_id=getattr(agent, "agent_id", None),
            )
        return None

    def persist_snapshot() -> None:
        context_lock = getattr(agent, "_context_revision_lock", None)
        with context_lock if context_lock is not None else nullcontext():
            messages = [dict(message) for message in agent.messages]
            model = getattr(agent.llm, "model", config.model)
            total_prompt_tokens = agent.state.total_prompt_tokens
            total_completion_tokens = agent.state.total_completion_tokens
            active_mode = getattr(agent, "active_mode", None)
            runtime_state = build_session_runtime_state(config, agent)
            persistence_kwargs = build_session_persistence_kwargs(agent)
        store.save(
            messages,
            model,
            session_id,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            active_mode=active_mode,
            runtime_state=runtime_state,
            fingerprint=fingerprint,
            incremental=True,
            events_already_persisted=True,
            **persistence_kwargs,
        )

    def persist() -> None:
        monitor = getattr(agent, "performance_monitor", None)
        if monitor is None:
            persist_snapshot()
            return
        started = time.monotonic()
        status = "error"
        try:
            persist_snapshot()
            status = "ok"
        finally:
            try:
                monitor.record(
                    "persistence",
                    "session_snapshot",
                    (time.monotonic() - started) * 1000,
                    status=status,
                    attributes={
                        "session_id": session_id,
                        "incremental": True,
                    },
                )
            except BaseException as error:
                # Monitoring is an observer. It may request cooperative stop,
                # but it cannot rewrite the persistence outcome.
                _request_cooperative_stop(agent, error)
                _retain_persistence_incident(
                    agent,
                    "persistence_monitor",
                    _safe_persistence_error_type(error),
                    "session_snapshot",
                )

    # Resolve and validate the filesystem target before changing live identity.
    if events_path is None:
        resolve_events_path = getattr(store, "get_session_events_path", None)
        events_path = (
            resolve_events_path(session_id)
            if callable(resolve_events_path)
            else store.sessions_dir / session_id / "events.jsonl"
        )
    agent.current_session_id = session_id
    ledger = getattr(agent, "history_ledger", None)
    bind_context = getattr(ledger, "bind_context", None)
    try:
        if callable(bind_context):
            bind_context(
                session_id=session_id,
                agent_id=getattr(agent, "agent_id", None),
            )
        bind(
            events_path=events_path,
            callback=_LiveSessionPersistence(
                persist,
                context_lock=getattr(agent, "_context_revision_lock", None),
                incident_sink=lambda phase, error_type, ref: (
                    _retain_persistence_incident(agent, phase, error_type, ref)
                ),
                stop_sink=getattr(agent, "request_stop", None),
            ),
        )
    except KeyboardInterrupt as error:
        _record_persistence_bind_failure(agent, error)
        raise
    except BaseException as error:
        return _record_persistence_bind_failure(agent, error)
    return None


def get_runtime_approval_config(config: Config, agent: Agent) -> ApprovalConfig:
    """Return the effective approval config from baseline + session rule overrides."""
    session_rules = getattr(agent, "session_approval_rules", None)
    return merge_approval_config(config.approval, session_rules)


def build_session_runtime_state(config: Config, agent: Agent) -> SessionRuntimeState:
    """Capture session-scoped runtime overrides from the live host runtime."""
    session_rules = getattr(agent, "session_approval_rules", None) or []
    skills_service = getattr(agent, "skills_service", None)
    disabled_names = getattr(skills_service, "disabled_names", None)
    if disabled_names is None:
        disabled_names = getattr(getattr(config, "skills", None), "disabled", []) or []
    goal_controller = getattr(agent, "goal_controller", None)
    goal = goal_controller.state if goal_controller is not None else None
    return SessionRuntimeState(
        goal=goal.to_dict() if goal else None,
        model=getattr(agent.llm, "model", None) or getattr(config, "model", None),
        active_mode=getattr(agent, "active_mode", None),
        llm_debug_trace=getattr(agent.llm, "debug_trace", None),
        active_main_model_profile=getattr(agent, "active_main_model_profile", None),
        active_sub_model_profile=getattr(agent, "active_sub_model_profile", None)
        or getattr(config, "active_sub_model_profile", None),
        skills_disabled=sorted(str(name) for name in disabled_names),
        approval_rules=[rule.to_dict() for rule in session_rules],
        plan_state=(
            agent.plan_controller.state.to_dict()
            if hasattr(agent, "plan_controller")
            else {}
        ),
        progress_state=(
            agent.plan_controller.progress.to_dict()
            if hasattr(agent, "plan_controller")
            else {}
        ),
    )


def restore_config_runtime_defaults(config: Config, agent: Agent) -> None:
    """Reset live runtime state back to config defaults for a fresh session."""
    agent.session_restore_issues = ()
    agent.session_inventory_issues = ()
    profiles = getattr(config, "model_profiles", {}) or {}
    main_profile_name = getattr(config, "active_main_model_profile", None) or getattr(
        config, "active_model_profile", None
    )
    if main_profile_name and main_profile_name in profiles:
        apply_main_model_profile(
            config,
            agent,
            main_profile_name,
            profiles[main_profile_name],
            debug_trace=getattr(config, "llm_debug_trace", False),
        )
    else:
        agent.llm.debug_trace = getattr(config, "llm_debug_trace", False)
        agent.context.reconfigure(
            config.max_context_tokens,
            **resolve_context_strategies(config.context),
        )
    agent.active_main_model_profile = main_profile_name
    agent.active_sub_model_profile = getattr(config, "active_sub_model_profile", None)
    agent.session_approval_rules = []
    refresh_approval_runtime(agent, config.approval)

    default_mode = getattr(config, "active_mode", None)
    if default_mode and default_mode in getattr(agent, "available_modes", {}):
        agent.set_mode(default_mode)
    else:
        agent.active_mode = default_mode


def apply_session_runtime_state(session: Session, config: Config, agent: Agent) -> None:
    """Apply persisted session runtime state onto the live host runtime."""
    discard_steering = getattr(agent, "discard_pending_user_steering", None)
    if callable(discard_steering):
        discard_steering(reason="session_exit")
    unbind = getattr(agent, "unbind_session_persistence", None)
    if callable(unbind):
        unbind()
    reset = getattr(agent, "reset", None)
    if callable(reset):
        reset()
    restore_config_runtime_defaults(config, agent)
    agent.session_restore_issues = tuple(getattr(session, "restore_issues", ()))
    runtime = session.runtime_state

    restore_history = getattr(agent, "restore_history_runtime", None)
    if callable(restore_history):
        behavioral_events = restore_history(session) or ()
        restorable_subagent_kinds = {
            "subagent_job_changed", "subagent_communication_queued",
            "subagent_communication_delivered",
        }
        if any(event.kind in restorable_subagent_kinds for event in behavioral_events):
            from reuleauxcoder.extensions.subagent.manager import get_subagent_manager

            get_subagent_manager(agent).restore_from_history(agent, behavioral_events)
    else:
        agent.state.messages = list(session.messages)
    agent.state.total_prompt_tokens = session.total_prompt_tokens
    agent.state.total_completion_tokens = session.total_completion_tokens
    agent.state.current_round = 0

    loaded_mode = runtime.active_mode or session.active_mode
    if loaded_mode and loaded_mode in getattr(agent, "available_modes", {}):
        agent.set_mode(loaded_mode)

    loaded_debug = runtime.llm_debug_trace
    if loaded_debug is not None:
        agent.llm.debug_trace = loaded_debug

    skills_disabled = list(getattr(runtime, "skills_disabled", []) or [])
    config_skills = getattr(config, "skills", None)
    if config_skills is not None:
        config_skills.disabled = list(skills_disabled)
    skills_service = getattr(agent, "skills_service", None)
    restore_disabled = getattr(skills_service, "restore_disabled_names", None)
    if callable(restore_disabled) and restore_disabled(skills_disabled):
        # Skills feed the system prompt; refresh the cached catalog so the
        # restored session prompts match what the session was saved with.
        build_catalog = getattr(skills_service, "build_catalog", None)
        if callable(build_catalog):
            catalog = build_catalog()
            if isinstance(catalog, str):
                agent.skills_catalog = catalog

    if runtime.approval_rules:
        session_rules = [
            ApprovalRuleConfig.from_dict(
                rule, default_action=config.approval.default_mode
            )
            for rule in runtime.approval_rules
        ]
        agent.session_approval_rules = session_rules
        refresh_approval_runtime(
            agent, merge_approval_config(config.approval, session_rules)
        )

    main_profile = runtime.active_main_model_profile
    profiles = getattr(config, "model_profiles", {}) or {}
    if main_profile and main_profile in profiles:
        apply_main_model_profile(config, agent, main_profile, profiles[main_profile])
    elif runtime.model:
        agent.llm.model = runtime.model
        agent.active_main_model_profile = None

    agent.active_sub_model_profile = runtime.active_sub_model_profile
    plan_controller = getattr(agent, "plan_controller", None)
    if plan_controller is not None:
        plan_controller.restore(runtime.plan_state, runtime.progress_state)
    goal_controller = getattr(agent, "goal_controller", None)
    if goal_controller is not None:
        goal_controller.restore(runtime.goal, session.history_events)
    restore_checkpoints = getattr(agent.context, "restore_checkpoints", None)
    if callable(restore_checkpoints):
        restore_checkpoints(list(getattr(session, "checkpoints", ())))
