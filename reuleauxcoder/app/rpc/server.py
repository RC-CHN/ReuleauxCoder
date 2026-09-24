"""One backend session owns execution, admission and interaction lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import logging
from pathlib import Path
import threading
from typing import get_type_hints

from reuleauxcoder.app.commands.requests import ActionRequest, CommandResult
from reuleauxcoder.app.commands.capabilities import UIProfile
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.commands.view_models import GoalViewModel
from reuleauxcoder.app.rpc.codec import encode, decode
from reuleauxcoder.app.rpc.models import RuntimeSnapshot, Submission
from reuleauxcoder.app.rpc.remote_interactor import RemoteInteractor
from reuleauxcoder.app.rpc.submissions import SubmissionAdmissions
from reuleauxcoder.app.rpc.images import ImageUploads
from reuleauxcoder.app.rpc.attachments import AttachmentUploads
from reuleauxcoder.app.rpc.editor_documents import EditorDocuments
from reuleauxcoder.domain.images import ChatInput
from reuleauxcoder.infrastructure.persistence.images import ImageStore
from reuleauxcoder.app.runtime.approval import build_runtime_approval_provider
from reuleauxcoder.app.runtime.approval_interaction import make_approval_handler
from reuleauxcoder.app.runtime.interactions import InteractionCoordinator
from reuleauxcoder.app.ui_events import AgentEventBridge, UIEventKind
from reuleauxcoder.domain.session.models import Session
from reuleauxcoder.domain.runtime.events import RuntimeEvent, SubagentJobChanged
from reuleauxcoder.infrastructure.rpc.peer import RpcError, RpcPeer
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir
from reuleauxcoder.infrastructure.persistence.history_query import SessionHistory
from reuleauxcoder.infrastructure.platform import get_platform_info

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _GoalContinuation:
    goal_id: str
    generation: int




class RuntimeServer:
    def __init__(self, commands: CommandService, peer: RpcPeer, *, host_mode=False):
        self.commands, self.peer = commands, peer
        self.host_mode = host_mode
        self.agent, self.config, self.bus = (
            commands.agent,
            commands.config,
            commands.ui_bus,
        )
        self._lock = threading.Condition(threading.RLock())
        self._running = False
        self._closing = False
        self._initialized = False
        self._ready = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._snapshot: RuntimeSnapshot | None = None
        self._published_revision = 0
        self._workers: set[threading.Thread] = set()
        self._submissions = SubmissionAdmissions()
        self.editor_documents = EditorDocuments()
        self.agent.document_mutation_guard = self.editor_documents.guard
        if self.agent.image_store is None:
            self.agent.image_store = ImageStore(
                commands.sessions_dir or self.config.session_dir or get_sessions_dir(),
                self.config.image,
            )
        self.agent.llm.image_store = self.agent.image_store
        self.images = ImageUploads(self)
        self.attachments = AttachmentUploads(self)
        self.interactions = InteractionCoordinator(RemoteInteractor(peer))
        commands.interactions = self.interactions
        self.agent.ui_interactor = self.interactions
        self.agent.approval_provider = build_runtime_approval_provider(
            self.agent, make_approval_handler(self.interactions)
        )
        bridge = AgentEventBridge(
            self.bus, generation_owner_agent_id=self.agent.agent_id
        )
        self.agent.add_event_handler(bridge.on_agent_event)
        self.bus.subscribe(self._event, replay_history=False)
        self.agent.goal_controller.on_change = self._goal_changed
        peer.methods.update(
            {
                "initialize": self.initialize,
                "runtime.ready": self.ready,
                "goal.get": lambda: encode(self.agent.goal_controller.state),
                "runtime.submit": self.submit,
                "review.document": self.interactions.adapter.document,
                "runtime.editor_documents": self.editor_documents.update,
                "images.begin": self.images.begin,
                "images.append": self.images.append,
                "images.complete": self.images.complete,
                "images.cancel": self.images.cancel,
                "attachments.begin": self.attachments.begin,
                "attachments.append": self.attachments.append,
                "attachments.complete": self.attachments.complete,
                "attachments.cancel": self.attachments.cancel,
                "runtime.interrupt": self.interrupt,
                "runtime.admit_steering": self.admit_steering,
                "runtime.stop": self.stop,
                "runtime.resize": self.resize,
                "runtime.report_issue": self.agent.record_runtime_issue,
                "runtime.record_performance": self.record_performance,
                "runtime.shutdown": self.shutdown,
                "runtime.snapshot": self.snapshot_reply,
                "runtime.git": self.git_snapshot,
                "history.read": lambda **params: self.history_query("read", **params),
                "history.search": lambda **params: self.history_query(
                    "search", **params
                ),
                "history.artifact": lambda **params: self.history_query(
                    "artifact", **params
                ),
                "view.panel": lambda payload: encode(
                    commands.build_panel(decode(payload))
                ),
            }
        )

    def _notify(self, method, **params):
        try:
            self.peer.notify(method, params)
        except ConnectionError:
            if not self.peer.closed.is_set():
                raise

    def _event(self, event):
        if self._initialized:
            self._notify(
                "runtime.event",
                event=encode(event),
                session_generation=self.agent.session_generation,
            )

    def snapshot(self):
        agent = self.agent
        with self._lock:
            revision = self._snapshot.revision if self._snapshot else 0
            manager = agent.mcp_manager
            context = agent.context
            review = self.interactions.adapter.review_request
            state = RuntimeSnapshot(
                revision=revision,
                session_id=self.commands.session_id,
                agent_id=agent.agent_id,
                session_generation=agent.session_generation,
                running=self._running,
                stopping=self._running and agent.stop_requested(),
                interrupt_pending=agent.round_interrupt_pending(),
                queued_commands=self.commands.pending_commands,
                queued_steering=tuple(agent.pending_user_steering())
                + self.commands.pending_inputs,
                model=agent.llm.model,
                support_modal=tuple(getattr(agent.llm, "support_modal", ("text",))),
                context_tokens=context.predict_request_tokens(agent.messages),
                context_limit=context.request_input_limit,
                mcp_enabled=sum(server.enabled for server in self.config.mcp_servers),
                mcp_tools=manager.available_tool_count if manager else 0,
                mcp_state=manager.initial_state if manager else "ready",
                workspace=str(agent.runtime_working_directory or Path.cwd()),
                exit_saved_session_id=self.commands.exit_saved_session_id,
                approval_waiting=review.queue_status.waiting if review else 0,
                mode=agent.active_mode,
                approval_policy=self.config.approval.default_mode,
                goal=agent.goal_controller.state,
            )
            if state != self._snapshot:
                self._snapshot = replace(state, revision=revision + 1)
            return self._snapshot

    def snapshot_reply(self, known_revision=None):
        state = self.snapshot()
        return None if state.revision == known_revision else encode(state)

    def git_snapshot(self):
        monitor = self.agent.git_monitor
        return encode(monitor.workspace_snapshot() if monitor else None)

    def history_query(self, operation, session_id=None, **parameters):
        with self._lock:
            if not self._initialized:
                raise RpcError(-32002, "Initialize first")
            session_id = session_id or self.commands.session_id
            generation = self.agent.session_generation
        if session_id is None:
            raise RpcError(-32002, "No saved session is available yet")
        history = SessionHistory(
            self.commands.sessions_dir or self.config.session_dir or get_sessions_dir()
        )
        page = getattr(history, operation)(session_id, **parameters)
        return encode({"session_generation": generation, "page": page})

    def _publish_state(self):
        with self._lock:
            state = self.snapshot()
            if state.revision <= self._published_revision:
                return state
            self._published_revision = state.revision
        self._notify("runtime.state", state=encode(state))
        return state

    def initialize(self, profile, version=1):
        with self._lock:
            if version != 1:
                raise RpcError(-32001, "Unsupported protocol version")
            if self._initialized:
                raise RpcError(-32002, "Already initialized")
            profile = self._decode(profile)
            if not isinstance(profile, UIProfile):
                raise RpcError(-32602, "Expected a UI profile")
            self.commands.ui_profile = profile
            recent = Session(
                id=self.commands.session_id or "new",
                model=self.agent.llm.model,
                saved_at="",
                messages=list(self.agent.messages),
                history_events=list(self.agent.history_ledger.events),
                history_behavior_projection_safe=self.agent.history_completeness
                != "degraded",
            ).get_recent_conversation(max_user_turns=3)
            controller = self.agent.plan_controller
            result = encode(
                {
                    "version": 1,
                    "workspace_git": self.agent.git_monitor is not None,
                    "conditional_snapshots": True,
                    "submission_ids": True,
                    "review_documents": True,
                    "editor_documents": True,
                    "history_query": True,
                    "goals": True,
                    "image_uploads": True,
                    "attachment_uploads": True,
                    "catalog": self.commands.catalog,
                    "state": self.snapshot(),
                    "presentation": asdict(self.config.ui),
                    "host_mode": self.host_mode,
                    "model_configured": bool(self.config.api_key),
                    "runtime_environment": {
                        "system": get_platform_info().system,
                        "shell": get_platform_info().get_preferred_shell().value,
                    },
                    "base_url": self.config.base_url,
                    "startup_events": self.bus.history_snapshot(),
                    "recent_conversation": recent,
                    "plan": controller.state,
                    "progress": controller.progress,
                    "runtime_events": self._restored_job_events(),
                }
            )
            self._initialized = True
            return result

    def _restored_job_events(self):
        manager = self.agent._subagent_manager
        if manager is None:
            return ()
        return tuple(
            RuntimeEvent(
                payload=SubagentJobChanged(
                    job_id=job.id,
                    mode=job.mode,
                    task=job.task,
                    status=job.status,
                    result=job.result,
                    error=job.error,
                ),
                agent_id=self.agent.agent_id,
                session_generation=self.agent.session_generation,
                session_id=self.commands.session_id,
            )
            for job in manager.list_jobs()
        )

    def record_performance(
        self, category, name, elapsed_ms, status="ok", attributes=None
    ):
        monitor = self.agent.performance_monitor
        if monitor is not None:
            monitor.record(
                category, name, elapsed_ms, status=status, attributes=attributes
            )

    @staticmethod
    def _decode(value):
        try:
            return decode(value)
        except (KeyError, TypeError, ValueError) as error:
            raise RpcError(-32602, "Invalid wire value") from error

    def _input(self, value):
        value = self._decode(value)
        if isinstance(value, (str, ChatInput)):
            return value
        if not isinstance(value, ActionRequest) or not isinstance(value.command, dict):
            raise RpcError(-32602, "Expected text or an action request")
        action = next(
            (
                action
                for action in self.commands.registry.iter_actions(
                    self.commands.ui_profile
                )
                if action.action_id == value.action_id
            ),
            None,
        )
        if action is None:
            raise RpcError(-32602, "Unavailable action")
        try:
            command = action.command_type(**value.command)
            for name, expected in get_type_hints(action.command_type).items():
                # Builtin command parameters are primitives and optional primitives.
                if not isinstance(getattr(command, name), expected):
                    raise TypeError(name)
            return ActionRequest(value.action_id, command)
        except (TypeError, ValueError) as error:
            raise RpcError(-32602, "Invalid action parameters") from error

    def submit(self, value, submission_id=None, session_generation=None):
        if submission_id is None:
            return self._submit(value)
        if session_generation != self.agent.session_generation:
            raise RpcError(-32002, "Session changed; submission was not accepted")
        entry = self._submissions.get(
            submission_id, value, generation=session_generation,
        )
        # Waiting for another delivery of the same ID never owns admission.
        with entry.lock:
            if session_generation != self.agent.session_generation:
                raise RpcError(-32002, "Session changed; submission was not accepted")
            if entry.receipt is not None:
                if entry.status != "rejected":
                    return encode(entry.receipt)
                entry.receipt = None
                entry.status = None
            if entry.error is not None:
                raise entry.error
            try:
                if entry.status is None:
                    return self._submit(value, submission_id=submission_id, admission=entry)
                # A prior attempt admitted the input but failed its snapshot.
                if entry.status == "steering":
                    self.agent.persist_runtime_snapshot()
                entry.receipt = Submission(entry.status, self.snapshot(), submission_id)
                return encode(entry.receipt)
            except BaseException as error:
                if entry.status is None and not (
                    isinstance(error, RpcError) and error.code in {-32602, -32002}
                ):
                    entry.error = error
                raise

    def _submit(self, value, *, submission_id=None, admission=None):
        if not self._initialized:
            raise RpcError(-32002, "Initialize first")
        value = self._input(value)
        with self._lock:
            if self._closing:
                raise RpcError(-32002, "Session is closing")
            if admission is not None and admission.generation != self.agent.session_generation:
                raise RpcError(-32002, "Session changed; submission was not accepted")
            if submission_id is not None:
                if isinstance(value, str) and not value.startswith("/"):
                    value = ChatInput(text=value, submission_id=submission_id)
                elif isinstance(value, ChatInput):
                    value = replace(value, submission_id=submission_id)
            self._validate_chat_images(value)
            if isinstance(value, str) and value.startswith("/"):
                self._notify("runtime.command", text=value)
            if self._running:
                if (
                    isinstance(value, ChatInput)
                    or isinstance(value, str)
                    and not value.startswith("/")
                ):
                    accepted = self.agent.submit_user_steering(value, persist=False)
                    status = "steering" if accepted else "rejected"
                    if not accepted and not self.agent.stop_requested():
                        self.commands.queue_input(value)
                        status = "queued"
                else:
                    request = self.commands.prepare_during_turn(value)
                    status = "queued" if request is None else "running"
                    if request is not None:
                        self._spawn(request, concurrent=True)
            else:
                # Clear the previous turn's stop before admission becomes visible.
                # Workers must preserve interrupts received after this boundary.
                self.agent.clear_stop_request()
                self._running = True
                status = "running"
                self._spawn(value)
            if admission is not None:
                admission.status = status
            submission = Submission(status, self._publish_state(), submission_id)
        # Admission remains atomic and ledger-durable. Snapshot capture can
        # wait for context/goal work that itself publishes through this lock.
        if status == "steering":
            self.agent.persist_runtime_snapshot()
        if admission is not None:
            admission.receipt = submission
        return encode(submission)

    def _validate_chat_images(self, value, *, check_model=True):
        if not isinstance(value, ChatInput):
            return
        if not value.text.strip() and not value.images:
            raise RpcError(-32602, "Chat input is empty")
        if not value.images:
            return
        self.images._check_session(value.session_id, value.session_generation)
        if check_model and "image" not in getattr(self.agent.llm, "support_modal", ()):
            raise RpcError(
                -32602,
                "Current model does not support images. Switch model or detach the images; your draft is preserved.",
            )
        try:
            for image in value.images:
                self.agent.image_store.validate_reference(value.session_id, image)
        except (OSError, ValueError) as error:
            raise RpcError(-32602, str(error)) from error

    def _spawn(self, value, *, concurrent=False):
        worker = threading.Thread(
            target=self._run,
            args=(value, concurrent),
            name="runtime-operation",
            daemon=True,
        )
        self._workers.add(worker)
        worker.start()

    def _goal_changed(self):
        if self._initialized:
            self._publish_state()
            self.bus.refresh_view(
                GoalViewModel(
                    self.agent.goal_controller.state,
                    self.config.goal_default_token_budget,
                ),
                title="Goal",
                reuse_key="goal",
            )

    def ready(self):
        with self._lock:
            if not self._initialized:
                raise RpcError(-32002, "Initialize first")
            self._ready = True
            self._wake_goal()
            return encode(self._publish_state())

    def _next_goal(self):
        goal = self.agent.goal_controller.state
        if (
            not self._ready
            or self._closing
            or self.agent.stop_requested()
            or goal is None
            or goal.status != "active"
            or self.agent.active_mode == "planner"
            or not self.agent.is_tool_allowed_in_mode("update_goal")
        ):
            return None
        return _GoalContinuation(goal.id, self.agent.session_generation)

    def _wake_goal(self):
        # Called under the admission lock, after all user/client work is drained.
        if self._running or self._workers:
            return
        value = self._next_goal()
        if value is not None:
            self.agent.clear_stop_request()
            self._running = True
            self._spawn(value)

    def _run(self, value, concurrent):
        try:
            while value is not None:
                try:
                    continuation = isinstance(value, _GoalContinuation)
                    if continuation:
                        with self._lock:
                            current = self._next_goal()
                            allowed = (
                                current == value and not self.agent.stop_requested()
                            )
                        if allowed:
                            self.agent.chat(
                                "", goal_continuation=True, clear_stop=False
                            )
                        result = CommandResult(session_id=self.commands.session_id)
                    else:
                        result = self.commands.submit(
                            value, during_turn=concurrent, clear_stop=False
                        )
                    if result.control == "chat":
                        with self._lock:
                            # An accepted, queued image survives a later model switch.
                            # The request projection decides whether its bytes are sent.
                            self._validate_chat_images(value, check_model=False)
                        response = self.agent.chat(
                            self.commands.prepare_chat_input(value), clear_stop=False
                        )
                        result = CommandResult(
                            session_id=self.commands.session_id, response=response
                        )
                    self._notify("runtime.completed", result=encode(result))
                    if result.control == "exit":
                        with self._lock:
                            self._closing = True
                    if (
                        not concurrent
                        and not self._closing
                        and self.agent.stop_requested()
                    ):
                        self.agent.goal_controller.stop("paused")
                except KeyboardInterrupt:
                    self.agent.goal_controller.stop("paused")
                    self.agent.request_stop()
                    self.bus.warning("Interrupted.")
                except BaseException as error:
                    if not concurrent:
                        self.agent.goal_controller.stop(
                            "usage_limited"
                            if getattr(error, "status_code", None) == 429
                            else "blocked"
                        )
                    log.exception("Runtime operation failed")
                    try:
                        self.commands.record_chat_failure(error)
                    except Exception:
                        log.exception("Failed to record chat failure")
                    self.bus.error(
                        f"Operation failed: {type(error).__name__}: {error}",
                        kind=UIEventKind.SYSTEM,
                    )
                    self._notify(
                        "runtime.failed",
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                with self._lock:
                    value = (
                        None
                        if concurrent or self._closing
                        else self.commands.next_pending()
                    )
                    if not concurrent and value is None and len(self._workers) == 1:
                        value = self._next_goal()
                    if not concurrent and value is None:
                        self._running = False
                    elif not concurrent:
                        self.agent.clear_stop_request()
                    self._publish_state()
        except BaseException:
            log.exception("Runtime could not record or publish its result")
            with self._lock:
                self._closing = True
                if not concurrent:
                    self._running = False
            self.peer.close()
        finally:
            with self._lock:
                self._workers.discard(threading.current_thread())
                self._wake_goal()
                self._lock.notify_all()

    def admit_steering(self, text):
        if not isinstance(text, str):
            raise RpcError(-32602, "Expected steering text")
        with self._lock:
            result = (
                self.agent.admit_user_steering(text, persist=False)
                if self._running and not self._closing
                else None
            )
        if result is not None:
            self.agent.persist_runtime_snapshot()
        self._publish_state()
        return result

    def stop(self):
        with self._lock:
            self.agent.request_stop()
        self.agent.goal_controller.stop("paused")
        self._publish_state()

    def interrupt(self):
        with self._lock:
            result = self.agent.request_interrupt_intent()
        self.agent.goal_controller.stop("paused")
        self._publish_state()
        return {
            "outcome": result.outcome.value,
            "discarded_count": result.discarded_count,
        }

    def resize(self, rows, columns):
        manager = self.agent.process_manager
        if manager is not None:
            manager.resize_tty_sessions(
                rows=max(1, int(rows)),
                columns=max(1, int(columns)),
                agent_id=self.agent.agent_id,
                owner_session_id=self.commands.session_id,
                session_generation=self.agent.session_generation,
            )

    def shutdown(self):
        with self._shutdown_lock:
            if not self._shutdown_complete:
                self._shutdown()
                self._shutdown_complete = True
            return self.commands.exit_saved_session_id

    def _shutdown(self):
        def progress(message):
            self._notify("runtime.shutdown_progress", message=message)

        progress("Stopping active tasks...")
        with self._lock:
            self._closing = True
            self.images.close()
            self.attachments.close()
            self.commands.clear_pending()
            discarded = self.agent.discard_pending_user_steering(
                reason="session_exit", persist=False
            )
            self.agent.request_stop()
        if discarded:
            self.agent.persist_runtime_snapshot()
        self.interactions.shutdown(reason="session closed")
        self.agent.approval_provider.close()
        with self._lock:
            if not self._lock.wait_for(lambda: not self._workers, timeout=10):
                raise TimeoutError("Backend operations did not stop within 10 seconds")
        self.agent.reconcile_pending_tool_calls("session closed")
        self.commands.save_exit(progress=progress)
        self._publish_state()
