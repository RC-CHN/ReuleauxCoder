"""Helpers for session-scoped runtime state persistence and restore."""

from __future__ import annotations

from contextlib import nullcontext
import threading

from reuleauxcoder.app.runtime.approval import (
    merge_approval_config,
    refresh_approval_runtime,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
)
from reuleauxcoder.domain.session.models import Session, SessionRuntimeState
from reuleauxcoder.infrastructure.persistence.session_store import (
    DEFAULT_SESSION_FINGERPRINT,
)
from reuleauxcoder.services.llm.factory import reconfigure_llm_from_settings

_LIVE_SNAPSHOT_DELAY_SECONDS = 0.15


class _LiveSessionPersistence:
    """Coalesce full snapshots while the append-only ledger stays durable."""

    def __init__(self, persist, *, delay: float = _LIVE_SNAPSHOT_DELAY_SECONDS):
        self._persist = persist
        self._delay = max(0.0, delay)
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._closed = False
        self._generation = 0

    def __call__(self, *, deferred: bool = False) -> None:
        if not deferred:
            self.flush()
            return
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            generation = self._generation
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(
                self._delay,
                self._run_deferred,
                args=(generation,),
            )
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _run_deferred(self, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._generation:
                return
            self._timer = None
        with self._write_lock:
            with self._lock:
                if self._closed or generation != self._generation:
                    return
            try:
                self._persist()
            except Exception:
                # The event itself was fsync'd before this best-effort snapshot.
                # A later forced flush or restore-tail reconstruction recovers.
                return

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            timer, self._timer = self._timer, None
            if timer is not None:
                timer.cancel()
        with self._write_lock:
            self._persist()

    def close(self) -> None:
        self.flush()
        with self._lock:
            self._closed = True


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


def bind_session_persistence(
    config: Config,
    agent: Agent,
    store,
    session_id: str,
    *,
    fingerprint: str,
) -> None:
    """Bind live ledger fsync and replay snapshots to the active session."""
    agent.current_session_id = session_id
    ledger = getattr(agent, "history_ledger", None)
    bind_context = getattr(ledger, "bind_context", None)
    if callable(bind_context):
        bind_context(
            session_id=session_id,
            agent_id=getattr(agent, "agent_id", None),
        )
    bind = getattr(agent, "bind_session_persistence", None)
    if not callable(bind):
        return

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
        with monitor.measure(
            "persistence",
            "session_snapshot",
            attributes={
                "session_id": session_id,
                "incremental": True,
            },
        ):
            persist_snapshot()

    resolve_events_path = getattr(store, "get_session_events_path", None)
    events_path = (
        resolve_events_path(session_id)
        if callable(resolve_events_path)
        else store.sessions_dir / session_id / "events.jsonl"
    )
    bind(events_path=events_path, callback=_LiveSessionPersistence(persist))


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
    return SessionRuntimeState(
        model=getattr(agent.llm, "model", None) or getattr(config, "model", None),
        active_mode=getattr(agent, "active_mode", None),
        llm_debug_trace=getattr(agent.llm, "debug_trace", None),
        active_main_model_profile=getattr(agent, "active_main_model_profile", None),
        active_sub_model_profile=getattr(agent, "active_sub_model_profile", None)
        or getattr(config, "active_sub_model_profile", None),
        skills_disabled=sorted(str(name) for name in disabled_names),
        approval_rules=[
            {
                "tool_name": rule.tool_name,
                "tool_source": rule.tool_source,
                "mcp_server": rule.mcp_server,
                "effect_class": rule.effect_class,
                "profile": rule.profile,
                "pattern": rule.pattern,
                "scope_key": rule.scope_key,
                "action": rule.action,
            }
            for rule in session_rules
        ],
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
        profile = profiles[main_profile_name]
        reconfigure_llm_from_settings(
            agent.llm,
            profile,
            debug_trace=getattr(config, "llm_debug_trace", False),
        )
        agent.context.reconfigure(profile.max_context_tokens)
    else:
        agent.llm.debug_trace = getattr(config, "llm_debug_trace", False)
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
        restore_history(session)
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
            ApprovalRuleConfig(
                tool_name=rule.get("tool_name"),
                tool_source=rule.get("tool_source"),
                mcp_server=rule.get("mcp_server"),
                effect_class=rule.get("effect_class"),
                profile=rule.get("profile"),
                pattern=rule.get("pattern"),
                scope_key=rule.get("scope_key"),
                action=rule.get("action", config.approval.default_mode),
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
        profile = profiles[main_profile]
        agent.llm.reconfigure(
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            preserve_reasoning_content=profile.preserve_reasoning_content,
            backfill_reasoning_content_for_tool_calls=profile.backfill_reasoning_content_for_tool_calls,
            reasoning_effort=profile.reasoning_effort,
            reasoning_effort_values=profile.reasoning_effort_values,
            reasoning_effort_param=profile.reasoning_effort_param,
            thinking_enabled=profile.thinking_enabled,
            reasoning_replay_mode=profile.reasoning_replay_mode,
            reasoning_replay_placeholder=profile.reasoning_replay_placeholder,
            debug_trace=agent.llm.debug_trace,
        )
        agent.context.reconfigure(profile.max_context_tokens)
        agent.active_main_model_profile = main_profile
    elif runtime.model:
        agent.llm.model = runtime.model
        agent.active_main_model_profile = None

    agent.active_sub_model_profile = runtime.active_sub_model_profile
    plan_controller = getattr(agent, "plan_controller", None)
    if plan_controller is not None:
        plan_controller.restore(runtime.plan_state, runtime.progress_state)
    restore_checkpoints = getattr(agent.context, "restore_checkpoints", None)
    if callable(restore_checkpoints):
        restore_checkpoints(list(getattr(session, "checkpoints", ())))
