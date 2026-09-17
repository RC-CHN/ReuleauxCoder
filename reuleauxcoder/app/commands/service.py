"""Application boundary shared by slash input, panels and other frontends."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
import logging
from pathlib import Path
import threading
import time
from typing import TYPE_CHECKING

from reuleauxcoder.app.commands.capabilities import UIProfile
from reuleauxcoder.app.commands.models import CommandContext, CommandEffect
from reuleauxcoder.app.commands.panels import CommandPanelRegistry, PanelPresentation
from reuleauxcoder.app.commands.registry import ActionRegistry, ParsedAction
from reuleauxcoder.app.commands.requests import ActionRequest, CommandResult
from reuleauxcoder.app.commands.specs import ActionCatalog, DuringTurnPolicy
from reuleauxcoder.app.commands.text import _invalid_command_usage, _suggest_command
from reuleauxcoder.app.ui_events import UIEventBus, UIEventKind, ViewEventPayload
from reuleauxcoder.app.runtime.session_state import (
    build_session_runtime_state,
    build_session_persistence_kwargs,
    get_session_fingerprint,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.app.interaction_contracts import UIInteractor
from reuleauxcoder.domain.images import ChatInput

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.skills.service import SkillsService


_logger = logging.getLogger(__name__)


class CommandService:
    """Own command dispatch, queue policy, audit and session transitions.

    The runtime owns workers; this service owns command policy and effects.
    """

    def __init__(
        self,
        agent: Agent,
        config: Config,
        ui_bus: UIEventBus,
        ui_profile: UIProfile,
        registry: ActionRegistry,
        *,
        sessions_dir: Path | None = None,
        session_exit_time: str | None = None,
        skills_service: SkillsService | None = None,
        interactions: UIInteractor | None = None,
        panels: CommandPanelRegistry | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.ui_bus = ui_bus
        self.ui_profile = ui_profile
        self.registry = registry
        self.sessions_dir = sessions_dir
        self.session_exit_time = session_exit_time
        self.skills_service = skills_service
        self.interactions = interactions
        self.panels = panels
        self.exit_saved_session_id: str | None = None
        self._pending: deque[tuple[ActionRequest | str | ChatInput, str]] = deque()
        self._queue_lock = threading.Lock()
        self._execution_lock = threading.RLock()

    @property
    def session_id(self) -> str | None:
        return self.agent.current_session_id

    @property
    def catalog(self) -> ActionCatalog:
        return self.registry.catalog

    @property
    def pending_commands(self) -> tuple[str, ...]:
        with self._queue_lock:
            return tuple(
                label
                for request, label in self._pending
                if isinstance(request, ActionRequest)
            )

    @property
    def pending_inputs(self) -> tuple[str, ...]:
        with self._queue_lock:
            return tuple(
                label
                for request, label in self._pending
                if isinstance(request, (str, ChatInput))
            )

    def queue_input(self, text: str | ChatInput) -> None:
        with self._queue_lock:
            self._pending.append(
                (text, text.display_text if isinstance(text, ChatInput) else text)
            )

    def build_panel(self, payload: ViewEventPayload) -> PanelPresentation | None:
        if self.panels is None:
            return None
        spec = self.panels.get(payload.view_type)
        if spec is None:
            return None
        definition = spec.build_for(payload.view_model, payload.title)
        if definition is None:
            return None
        return PanelPresentation(definition, spec.refresh)

    def clear_pending(self) -> None:
        with self._queue_lock:
            self._pending.clear()

    def next_pending(self) -> ActionRequest | str | ChatInput | None:
        with self._queue_lock:
            if not self._pending:
                return None
            request, label = self._pending.popleft()
        self.ui_bus.info(
            f"Applying queued {'prompt' if isinstance(request, (str, ChatInput)) else 'command'} now: {label}",
            kind=UIEventKind.COMMAND,
        )
        return request

    def prepare_during_turn(self, value: str | ActionRequest) -> ActionRequest | None:
        """Enqueue before the UI returns to its event loop, or return immediate work."""
        prepared = self._prepare(value, during_turn=True)
        return prepared.request if not isinstance(prepared, CommandResult) else None

    def _prepare(
        self, value: str | ActionRequest | ChatInput, *, during_turn: bool
    ) -> ParsedAction | CommandResult:
        if isinstance(value, ChatInput):
            return CommandResult(control="chat", session_id=self.session_id)
        if isinstance(value, str):
            if not value.startswith("/"):
                return CommandResult(control="chat", session_id=self.session_id)
            parsed = self.registry.parse(value, ui_profile=self.ui_profile)
            if parsed is None:
                message = (
                    _suggest_command(value, self.registry, self.ui_profile)
                    or _invalid_command_usage(value, self.registry, self.ui_profile)
                    or "Unknown slash command. Use /help to list available commands."
                )
                self.ui_bus.warning(message, kind=UIEventKind.COMMAND)
                return CommandResult(session_id=self.session_id)
            request, label = parsed.request, value
        else:
            request, label = value, value.action_id
            parsed = self.registry.resolve(request, self.ui_profile)

        if (
            during_turn
            and parsed.action.during_turn is DuringTurnPolicy.DEFER_UNTIL_IDLE
        ):
            with self._queue_lock:
                self._pending.append((request, label))
            self.ui_bus.info(
                f"Queued command: {label}\nIt will run when the current turn becomes idle.",
                kind=UIEventKind.COMMAND,
            )
            return CommandResult(control="queued", session_id=self.session_id)
        return parsed

    def submit(
        self, value: str | ActionRequest | ChatInput, *, during_turn: bool = False
    ) -> CommandResult:
        parsed = self._prepare(value, during_turn=during_turn)
        if isinstance(parsed, CommandResult):
            return parsed
        with self._execution_lock:
            if not during_turn:
                clear_stop = getattr(self.agent, "clear_stop_request", None)
                if clear_stop is not None:
                    clear_stop()
            before = (self.session_id, getattr(self.agent, "session_generation", None))
            try:
                effect = CommandEffect()
                ctx = CommandContext(
                    agent=self.agent,
                    config=self.config,
                    effect=effect,
                    ui_profile=self.ui_profile,
                    action_registry=self.registry,
                    ui_interactor=self.interactions,
                    sessions_dir=self.sessions_dir,
                    skills_service=self.skills_service,
                    exit_session=self.save_exit,
                )
                effect = self.registry.dispatch(parsed, ctx)
                if effect.session_id is not None:
                    self.agent.current_session_id = effect.session_id
                changed = before != (
                    self.session_id,
                    getattr(self.agent, "session_generation", None),
                )
                if changed or effect.session_exit_time is not None:
                    self.session_exit_time = effect.session_exit_time
                if effect.control == "exit":
                    self.clear_pending()
                else:
                    self.exit_saved_session_id = None
                record_command_control_event(self.agent, parsed.action, effect)
                apply_command_effect(effect, self.ui_bus)
                controller = getattr(self.agent, "plan_controller", None)
                return CommandResult(
                    control=effect.control,
                    session_id=self.session_id,
                    session_changed=changed,
                    clear_transcript=effect.clear_transcript,
                    plan=controller.state
                    if changed and controller is not None
                    else None,
                    progress=controller.progress
                    if changed and controller is not None
                    else None,
                )
            except KeyboardInterrupt:
                raise
            except BaseException:
                _logger.exception("Command failed: %s", parsed.action.action_id)
                raise

    def prepare_chat_input(self, text: str | ChatInput) -> str | ChatInput:
        """Consume the restore marker once, from the backend's current session."""
        self.exit_saved_session_id = None
        if self.session_exit_time is None:
            return text
        exit_time, self.session_exit_time = self.session_exit_time, None
        now = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        prefix = (
            f"[SESSION_RESUME] User returned to the session at {now} "
            f"(last left at {exit_time}).\n\n"
        )
        return (
            replace(text, text=prefix + text.text)
            if isinstance(text, ChatInput)
            else prefix + text
        )

    def save_exit(self, *, progress: Callable[[str], None] | None = None) -> str | None:
        with self._execution_lock:
            return self._save_exit(progress=progress)

    def record_chat_failure(self, error: Exception) -> None:
        path = getattr(error, "llm_diagnostic_path", None)
        if path and self.session_id:
            store = SessionStore(self.sessions_dir)
            store.append_system_message(
                self.session_id,
                self.config.model,
                f"[LLM_ERROR_DIAGNOSTIC] path={path} error={type(error).__name__}: {error}",
                active_mode=getattr(self.agent, "active_mode", None),
            )
            ledger = getattr(self.agent, "history_ledger", None)
            if ledger is not None:
                ledger.raise_floor(store.last_persisted_sequence(self.session_id))

    def _save_exit(self, *, progress=None) -> str | None:
        if (
            self.exit_saved_session_id is not None
            or not self.agent.messages
            or not self.config.session_auto_save
        ):
            return self.exit_saved_session_id
        operation_id = f"session-save:{self.session_id or 'new'}"
        started = time.monotonic()
        if progress is not None:
            progress("Saving session snapshot...")
        ui_bus = self.ui_bus
        ui_bus.emit_operation_phase(
            operation_id=operation_id,
            operation="shutdown",
            phase="save_session",
            started_at=time.time(),
            cancelable=False,
            agent_id=getattr(self.agent, "agent_id", None),
            session_generation=getattr(self.agent, "session_generation", None),
            session_id=self.session_id,
        )
        store = SessionStore(self.sessions_dir)
        store.set_progress_callback(progress)
        try:
            sid = store.save(
                self.agent.messages,
                self.agent.llm.model,
                self.session_id,
                is_exit=True,
                total_prompt_tokens=self.agent.state.total_prompt_tokens,
                total_completion_tokens=self.agent.state.total_completion_tokens,
                active_mode=getattr(self.agent, "active_mode", None),
                fingerprint=get_session_fingerprint(self.config, self.agent),
                runtime_state=build_session_runtime_state(self.config, self.agent),
                incremental=True,
                events_already_persisted=True,
                **build_session_persistence_kwargs(self.agent),
            )
        except Exception as error:
            elapsed = time.monotonic() - started
            ui_bus.emit_operation_phase(
                operation_id=operation_id,
                operation="shutdown",
                phase="save_session",
                status="failed",
                detail=str(error)[:160] or type(error).__name__,
                elapsed_ms=int(elapsed * 1000),
                error_type=type(error).__name__,
                agent_id=getattr(self.agent, "agent_id", None),
                session_generation=getattr(self.agent, "session_generation", None),
                session_id=self.session_id,
            )
            if progress is not None:
                progress(
                    f"Session snapshot failed after {elapsed:.1f}s: "
                    f"{type(error).__name__}: {error}"
                )
            raise
        self.exit_saved_session_id = sid
        self.agent.current_session_id = sid
        self.agent.lifecycle.session_saved(sid)
        elapsed = time.monotonic() - started
        ui_bus.emit_operation_phase(
            operation_id=operation_id,
            operation="shutdown",
            phase="save_session",
            status="completed",
            elapsed_ms=int(elapsed * 1000),
            agent_id=getattr(self.agent, "agent_id", None),
            session_generation=getattr(self.agent, "session_generation", None),
            session_id=sid,
        )
        if progress is not None:
            progress(f"Session snapshot committed in {elapsed:.1f}s.")
        return sid


def record_command_control_event(agent, action, result: CommandEffect) -> None:
    ledger = getattr(agent, "history_ledger", None)
    if ledger is None or action.audit is None:
        return
    ledger.append(
        action.audit,
        {
            "action_id": action.action_id,
            "state_changes": result.state,
            "control": result.control,
            "session_id": result.session_id,
        },
        agent_id=getattr(agent, "agent_id", None),
        turn_id=getattr(agent, "_current_turn_id", None),
    )
    agent.persist_runtime_snapshot()


def apply_command_effect(result: CommandEffect, ui_bus: UIEventBus) -> None:
    for notice in result.notifications:
        getattr(ui_bus, notice.level)(
            notice.message,
            kind=UIEventKind(notice.kind),
            payload=notice.payload,
            **notice.metadata,
        )
    for view in result.views:
        if view.action == "refresh":
            ui_bus.refresh_view(
                view.view_model, title=view.title, reuse_key=view.reuse_key
            )
        else:
            ui_bus.open_view(
                view.view_model,
                title=view.title,
                focus=view.focus,
                reuse_key=view.reuse_key,
            )
