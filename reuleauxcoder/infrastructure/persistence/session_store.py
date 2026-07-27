"""Session persistence adapter backed by JSON files."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

from reuleauxcoder.domain.context.manager import (
    MESSAGE_TOKEN_KEY,
    ensure_message_token_counts,
)
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.replay import (
    ReplayEnvelope,
    RequestEnvelope,
    align_item_provenance,
)
from reuleauxcoder.domain.history import HistoryEvent, HistoryLedger
from reuleauxcoder.domain.llm.context_messages import synthetic_user_message
from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency
from reuleauxcoder.domain.session.models import (
    Session,
    SessionMetadata,
    SessionRuntimeState,
)
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir

DEFAULT_SESSION_FINGERPRINT = "local"

# Older request_committed events embedded the complete replay on every API
# round. Since every replay contained all preceding messages, that made the
# ledger grow quadratically. The latest complete replay remains authoritative
# in replay.json; request events retain only stable request/replay metadata.
_SESSION_STORAGE_SCHEMA_VERSION = 2
_MAX_REQUEST_RECORDS = 200


@dataclass(slots=True)
class _DirectoryWriteCursor:
    initialized: bool = False
    request_ids: set[str] = field(default_factory=set)
    checkpoint_ids: set[str] = field(default_factory=set)


class SessionStore:
    """File-backed store for conversation sessions."""

    def __init__(
        self,
        sessions_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self._sessions_dir = sessions_dir or get_sessions_dir()
        self._progress = progress
        self._lock = threading.RLock()
        self._write_cursors: dict[str, _DirectoryWriteCursor] = {}

    @property
    def sessions_dir(self) -> Path:
        """Return the underlying session directory."""
        return self._sessions_dir

    def set_progress_callback(self, progress: Callable[[str], None] | None) -> None:
        self._progress = progress

    def _report_progress(self, message: str) -> None:
        if self._progress is None:
            return
        try:
            self._progress(message)
        except Exception:
            pass

    def save(
        self,
        messages: list[dict],
        model: str,
        session_id: Optional[str] = None,
        is_exit: bool = False,
        total_prompt_tokens: int = 0,
        total_completion_tokens: int = 0,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
        history_events: list[HistoryEvent] | tuple[HistoryEvent, ...] | None = None,
        replay_envelope: ReplayEnvelope | None = None,
        request_envelopes: list[RequestEnvelope] | tuple[RequestEnvelope, ...] = (),
        history_completeness: str | None = None,
        checkpoints: list[CompactionCheckpoint] | tuple[CompactionCheckpoint, ...] = (),
        incremental: bool = False,
        events_already_persisted: bool = False,
    ) -> str:
        """Save conversation to disk and return the session ID."""
        with self._lock:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)

            if not session_id:
                session_id = self.generate_session_id()

            saved_messages, _ = reconcile_tool_call_adjacency(
                [dict(message) for message in messages],
                missing_content=lambda _tool_call_id, tool_name: (
                    f"Tool '{tool_name}' interrupted before session persistence."
                ),
            )
            ensure_message_token_counts(saved_messages)
            exit_events: tuple[HistoryEvent, ...] = ()
            exit_message: dict | None = None
            if is_exit:
                exit_time = time.strftime("%Y-%m-%d %H:%M:%S %Z")
                exit_message = {
                    "role": "user",
                    "content": f"[SESSION_EXIT] User left the session at {exit_time}.",
                }
                ensure_message_token_counts([exit_message])
                saved_messages.append(exit_message)

            ledger = HistoryLedger(history_events or ())
            if not history_events:
                for message in saved_messages:
                    event_count = len(ledger.events)
                    ledger.append_message(message, source="legacy_save_snapshot")
                    if exit_message is not None and message is exit_message:
                        exit_events = ledger.events[event_count:]
            elif is_exit:
                assert exit_message is not None
                event_count = len(ledger.events)
                ledger.append_message(exit_message, source="session_exit")
                exit_events = ledger.events[event_count:]

            effective_runtime = runtime_state or SessionRuntimeState(
                model=model, active_mode=active_mode
            )
            if effective_runtime.model is None:
                effective_runtime.model = model
            if effective_runtime.active_mode is None:
                effective_runtime.active_mode = active_mode

            base_replay = replay_envelope
            replay = ReplayEnvelope.create(
                session_id=session_id,
                cache_epoch=base_replay.cache_epoch if base_replay else 0,
                history_version=base_replay.history_version if base_replay else 0,
                model_profile=(
                    base_replay.model_profile
                    if base_replay
                    else effective_runtime.model or model
                ),
                provider_family=(
                    base_replay.provider_family if base_replay else "openai-compatible"
                ),
                request_mode=(
                    base_replay.request_mode if base_replay else "chat-completions"
                ),
                request_settings=(
                    dict(base_replay.request_settings) if base_replay else {}
                ),
                instructions=list(base_replay.instructions) if base_replay else [],
                tools=list(base_replay.tools) if base_replay else [],
                items=saved_messages,
                item_provenance=align_item_provenance(saved_messages, ledger.events),
            )
            session = Session(
                id=session_id,
                model=effective_runtime.model or model,
                saved_at=datetime.now().isoformat(timespec="microseconds"),
                fingerprint=fingerprint or DEFAULT_SESSION_FINGERPRINT,
                messages=saved_messages,
                active_mode=effective_runtime.active_mode or active_mode,
                total_prompt_tokens=total_prompt_tokens,
                total_completion_tokens=total_completion_tokens,
                runtime_state=effective_runtime,
                history_events=list(ledger.events),
                replay_envelope=replay,
                request_envelopes=list(request_envelopes)[-_MAX_REQUEST_RECORDS:],
                checkpoints=list(checkpoints),
                history_completeness=(
                    history_completeness
                    or (
                        "complete"
                        if history_events is not None
                        else "legacy_compacted_or_unknown"
                    )
                ),
            )
            self._write_session_directory(
                session,
                incremental=incremental,
                events_already_persisted=events_already_persisted,
                additional_events=(
                    exit_events if events_already_persisted else ()
                ),
            )
            return session_id

    def append_system_message(
        self,
        session_id: str,
        model: str,
        content: str,
        *,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
    ) -> None:
        """Append a tagged runtime diagnostic, retaining the legacy API name."""
        appended = synthetic_user_message(
            "session_diagnostic",
            content,
            source="session_store",
        )
        with self._lock:
            loaded = self.load(session_id)
            if loaded is None:
                self.save(
                    messages=[appended],
                    model=model,
                    session_id=session_id,
                    active_mode=active_mode,
                    runtime_state=runtime_state,
                    fingerprint=fingerprint,
                )
                return

            updated_messages = list(loaded.messages)
            updated_messages.append(appended)
            ledger = HistoryLedger(loaded.history_events)
            ledger.append_message(appended, source="session_diagnostic")
            self.save(
                messages=updated_messages,
                model=loaded.model or model,
                session_id=session_id,
                total_prompt_tokens=loaded.total_prompt_tokens,
                total_completion_tokens=loaded.total_completion_tokens,
                active_mode=loaded.active_mode or active_mode,
                runtime_state=runtime_state or loaded.runtime_state,
                fingerprint=loaded.fingerprint or fingerprint,
                history_events=list(ledger.events),
                replay_envelope=loaded.replay_envelope,
                request_envelopes=loaded.request_envelopes,
                checkpoints=loaded.checkpoints,
                history_completeness=loaded.history_completeness,
            )

    @staticmethod
    def generate_session_id() -> str:
        """Generate a new session ID."""
        return f"session_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    def load(self, session_id: str) -> Session | None:
        """Load a saved session."""
        with self._lock:
            self._report_progress(f"Loading session files for {session_id}...")
            path = self._get_session_path(session_id)
            directory = self._get_session_directory(session_id)
            if not path.exists() and not directory.exists():
                return None

            session = self._load_session_directory(directory)
            data = None
            if session is None:
                if not path.exists():
                    return None
                data = json.loads(path.read_text())
                session = Session.from_dict(data)
            elif path.exists():
                # A validated canonical directory supersedes the old
                # compatibility snapshot. Keeping both doubles current-state
                # storage and makes it unclear which copy is authoritative.
                path.unlink(missing_ok=True)
            updated_messages, _ = reconcile_tool_call_adjacency(
                [dict(message) for message in session.messages],
                missing_content=lambda _tool_call_id, tool_name: (
                    f"Tool '{tool_name}' interrupted in persisted session."
                ),
            )
            ensure_message_token_counts(updated_messages)
            session.messages = updated_messages
            if session.runtime_state.model is None:
                session.runtime_state.model = session.model
            if session.runtime_state.active_mode is None:
                session.runtime_state.active_mode = session.active_mode
            if data is not None:
                # Reading a legacy single-file session upgrades it to the
                # canonical directory format. Only remove the duplicate after
                # every canonical artifact has been written successfully.
                self.save(
                    messages=session.messages,
                    model=session.model,
                    session_id=session.id or session_id,
                    total_prompt_tokens=session.total_prompt_tokens,
                    total_completion_tokens=session.total_completion_tokens,
                    active_mode=session.active_mode,
                    runtime_state=session.runtime_state,
                    fingerprint=session.fingerprint,
                    history_events=session.history_events or None,
                    replay_envelope=session.replay_envelope,
                    request_envelopes=session.request_envelopes,
                    history_completeness=session.history_completeness,
                    checkpoints=session.checkpoints,
                )
                migrated = self._load_session_directory(directory)
                path.unlink(missing_ok=True)
                if migrated is not None:
                    session = migrated
            return session

    def list(
        self,
        limit: int = 20,
        *,
        fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT,
    ) -> list[SessionMetadata]:
        """List available sessions, newest first."""
        with self._lock:
            if not self._sessions_dir.exists():
                return []

            ranked_sessions: list[
                tuple[tuple[int, datetime, str], SessionMetadata]
            ] = []
            seen_ids: set[str] = set()
            entries = list(self._sessions_dir.iterdir())

            # Directory sessions are the canonical format. Listing only needs
            # their compact manifest; loading replay, ledger, requests and
            # checkpoints for every session made auto-resume scale with the
            # complete archive size.
            for directory in entries:
                if not directory.is_dir():
                    continue
                metadata = self._load_directory_metadata(directory)
                if metadata is None:
                    continue
                seen_ids.add(metadata.id)
                if fingerprint is not None and metadata.fingerprint != fingerprint:
                    continue
                ranked_sessions.append(
                    (
                        self._metadata_rank(
                            metadata, directory / "manifest.json"
                        ),
                        metadata,
                    )
                )

            for file_path in self._sessions_dir.glob("*.json"):
                if file_path.stem in seen_ids:
                    continue
                try:
                    data = json.loads(file_path.read_text())
                    session = Session.from_dict(data)
                    if fingerprint is not None and session.fingerprint != fingerprint:
                        continue

                    metadata = SessionMetadata(
                        id=session.id or file_path.stem,
                        model=session.model,
                        saved_at=session.saved_at,
                        preview=session.get_preview(),
                        fingerprint=session.fingerprint,
                    )
                    seen_ids.add(metadata.id)

                    ranked_sessions.append(
                        (self._metadata_rank(metadata, file_path), metadata)
                    )
                except (json.JSONDecodeError, KeyError):
                    continue

            ranked_sessions.sort(key=lambda item: item[0], reverse=True)
            return [metadata for _, metadata in ranked_sessions[:limit]]

    def get_latest(
        self, *, fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT
    ) -> SessionMetadata | None:
        """Return the most recent session metadata, if any."""
        sessions = self.list(limit=1, fingerprint=fingerprint)
        return sessions[0] if sessions else None

    @staticmethod
    def get_exit_time(messages: list[dict]) -> str | None:
        """Extract exit time from persisted session messages, if present."""
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role not in ("system", "user"):
                continue
            content = msg.get("content", "") or ""
            match = re.search(r"\[SESSION_EXIT\].* at (.+?)\.$", content)
            if match:
                return match.group(1)
        return None

    def _get_session_path(self, session_id: str) -> Path:
        """Map session ID to JSON file path."""
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        return self._sessions_dir / f"{safe_id}.json"

    def _get_session_directory(self, session_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        return self._sessions_dir / safe_id

    @staticmethod
    def _metadata_rank(
        metadata: SessionMetadata, source_path: Path
    ) -> tuple[int, datetime, str]:
        try:
            modified_ns = source_path.stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        try:
            saved_at_rank = datetime.fromisoformat(metadata.saved_at)
        except (TypeError, ValueError):
            try:
                saved_at_rank = datetime.strptime(
                    metadata.saved_at, "%Y-%m-%d %H:%M:%S"
                )
            except (TypeError, ValueError):
                saved_at_rank = datetime.fromtimestamp(0)
        return modified_ns, saved_at_rank, metadata.id

    @staticmethod
    def _load_directory_metadata(directory: Path) -> SessionMetadata | None:
        manifest_path = directory / "manifest.json"
        replay_path = directory / "replay.json"
        if not manifest_path.exists() or not replay_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            preview = str(manifest.get("preview") or "")
            if not preview:
                replay = json.loads(replay_path.read_text(encoding="utf-8"))
                preview = Session(
                    id=str(manifest.get("id") or directory.name),
                    model=str(manifest.get("model") or "?"),
                    saved_at=str(manifest.get("saved_at") or "?"),
                    messages=list(replay.get("items") or ()),
                ).get_preview()
            return SessionMetadata(
                id=str(manifest.get("id") or directory.name),
                model=str(manifest.get("model") or "?"),
                saved_at=str(manifest.get("saved_at") or "?"),
                preview=preview,
                fingerprint=str(manifest.get("fingerprint") or "local"),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def _write_session_directory(
        self,
        session: Session,
        *,
        incremental: bool = False,
        events_already_persisted: bool = False,
        additional_events: tuple[HistoryEvent, ...] = (),
    ) -> None:
        directory = self._get_session_directory(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        requests_dir = directory / "requests"
        requests_dir.mkdir(parents=True, exist_ok=True)
        checkpoints_dir = directory / "checkpoints"
        checkpoints_dir.mkdir(parents=True, exist_ok=True)

        cursor = self._write_cursors.setdefault(session.id, _DirectoryWriteCursor())
        if incremental and not cursor.initialized:
            cursor.request_ids = {path.stem for path in requests_dir.glob("*.json")}
            cursor.checkpoint_ids = {
                path.stem for path in checkpoints_dir.glob("*.json")
            }
            cursor.initialized = True

        events_path = directory / "events.jsonl"
        new_events: list[HistoryEvent] = list(additional_events)
        if not events_already_persisted:
            existing_ids: set[str] = set()
            if events_path.exists():
                with events_path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            existing_ids.add(str(json.loads(line).get("event_id")))
                        except json.JSONDecodeError:
                            continue
            new_events = [
                event
                for event in sorted(session.history_events, key=lambda item: item.seq)
                if event.event_id not in existing_ids
            ]
        if new_events:
            self._report_progress(
                f"Appending {len(new_events)} history event(s)..."
            )
            with events_path.open("a", encoding="utf-8") as stream:
                for event in new_events:
                    event, _ = self._compact_legacy_request_event(event)
                    stream.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

        replay = session.replay_envelope
        if replay is not None:
            self._report_progress(
                f"Writing replay snapshot ({len(replay.items)} item(s))..."
            )
            self._atomic_write_json(directory / "replay.json", replay.to_dict())
        pending_requests = [
            request
            for request in session.request_envelopes
            if not incremental or request.request_id not in cursor.request_ids
        ]
        if pending_requests:
            self._report_progress(
                f"Writing {len(pending_requests)} request record(s)..."
            )
        for request in pending_requests:
            self._atomic_write_json(
                requests_dir / f"{request.request_id}.json", request.to_dict()
            )
            cursor.request_ids.add(request.request_id)
        pending_checkpoints = [
            checkpoint
            for checkpoint in session.checkpoints
            if not incremental or checkpoint.id not in cursor.checkpoint_ids
        ]
        if pending_checkpoints:
            self._report_progress(
                f"Writing {len(pending_checkpoints)} context checkpoint(s)..."
            )
        for checkpoint in pending_checkpoints:
            self._atomic_write_json(
                checkpoints_dir / f"{checkpoint.id}.json", checkpoint.to_dict()
            )
            cursor.checkpoint_ids.add(checkpoint.id)
        manifest = session.to_dict()
        manifest.pop("messages", None)
        manifest.pop("history_events", None)
        manifest.pop("replay_envelope", None)
        manifest.pop("request_envelopes", None)
        manifest.pop("checkpoints", None)
        manifest["checkpoint_ids"] = [item.id for item in session.checkpoints]
        manifest["request_ids"] = [
            item.request_id for item in session.request_envelopes
        ]
        manifest["preview"] = session.get_preview()
        manifest["message_token_counts"] = [
            message.get(MESSAGE_TOKEN_KEY) for message in session.messages
        ]
        manifest["storage_schema_version"] = _SESSION_STORAGE_SCHEMA_VERSION
        manifest["event_payload_policy"] = "bounded"
        manifest["event_count"] = len(session.history_events)
        manifest["last_event_seq"] = max(
            (event.seq for event in session.history_events), default=0
        )
        self._report_progress("Committing session manifest...")
        self._atomic_write_json(directory / "manifest.json", manifest)
        retained_request_ids = set(manifest["request_ids"])
        known_request_ids = set(cursor.request_ids)
        if not incremental:
            known_request_ids.update(path.stem for path in requests_dir.glob("*.json"))
        for request_id in known_request_ids - retained_request_ids:
            try:
                (requests_dir / f"{request_id}.json").unlink(missing_ok=True)
            except OSError:
                continue
            else:
                cursor.request_ids.discard(request_id)

    def _load_session_directory(self, directory: Path) -> Session | None:
        manifest_path = directory / "manifest.json"
        replay_path = directory / "replay.json"
        if not manifest_path.exists() or not replay_path.exists():
            return None
        try:
            self._report_progress("Reading session manifest and replay snapshot...")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            replay = ReplayEnvelope.from_dict(
                json.loads(replay_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not replay.validate() or not replay.validate_protocol():
            return None

        events_path = directory / "events.jsonl"
        events = self._load_history_events(events_path)
        requests: list[RequestEnvelope] = []
        requests_dir = directory / "requests"
        if requests_dir.exists():
            request_ids = manifest.get("request_ids")
            if isinstance(request_ids, list):
                request_paths = [
                    requests_dir / f"{request_id}.json"
                    for request_id in request_ids[-_MAX_REQUEST_RECORDS:]
                    if isinstance(request_id, str)
                ]
                request_paths = [path for path in request_paths if path.exists()]
            else:
                request_paths = sorted(
                    requests_dir.glob("*.json"),
                    key=lambda path: path.stat().st_mtime_ns,
                )[-_MAX_REQUEST_RECORDS:]
            if request_paths:
                self._report_progress(
                    f"Reading {len(request_paths)} request record(s)..."
                )
            for request_path in request_paths:
                try:
                    requests.append(
                        RequestEnvelope(
                            **json.loads(request_path.read_text(encoding="utf-8"))
                        )
                    )
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
        checkpoints: list[CompactionCheckpoint] = []
        checkpoints_dir = directory / "checkpoints"
        if checkpoints_dir.exists():
            checkpoint_paths = sorted(checkpoints_dir.glob("*.json"))
            if checkpoint_paths:
                self._report_progress(
                    f"Reading {len(checkpoint_paths)} context checkpoint(s)..."
                )
            for checkpoint_path in checkpoint_paths:
                try:
                    checkpoints.append(
                        CompactionCheckpoint.from_dict(
                            json.loads(checkpoint_path.read_text(encoding="utf-8"))
                        )
                    )
                except (OSError, KeyError, json.JSONDecodeError, TypeError, ValueError):
                    continue
        # Avoid serializing these potentially huge structures to dictionaries
        # only for Session.from_dict() to reconstruct the same objects again.
        for key in (
            "messages",
            "history_events",
            "replay_envelope",
            "request_envelopes",
            "checkpoints",
        ):
            manifest.pop(key, None)
        session = Session.from_dict(manifest)
        session.messages = list(replay.items)
        token_counts = manifest.get("message_token_counts") or ()
        if len(token_counts) == len(session.messages):
            for message, token_count in zip(session.messages, token_counts):
                if isinstance(token_count, int):
                    message[MESSAGE_TOKEN_KEY] = token_count
        session.messages, recovered_count = self._recover_replay_tail(
            session.messages,
            replay,
            events,
        )
        if recovered_count:
            ensure_message_token_counts(session.messages)
            self._report_progress(
                f"Recovered {recovered_count} message update(s) from "
                "the durable history tail."
            )
        session.replay_envelope = replay
        session.history_events = events
        session.request_envelopes = requests
        session.checkpoints = checkpoints
        return session

    @staticmethod
    def _recover_replay_tail(
        snapshot_messages: list[dict],
        replay: ReplayEnvelope,
        events: list[HistoryEvent],
    ) -> tuple[list[dict], int]:
        """Apply durable message/view events committed after the replay snapshot."""
        if not events:
            return snapshot_messages, 0
        seq_by_id = {event.event_id: event.seq for event in events}
        snapshot_seq = max(
            (
                seq_by_id.get(str(event_id), 0)
                for provenance in replay.item_provenance
                for event_id in provenance.get("source_event_ids", ())
            ),
            default=0,
        )
        if snapshot_seq <= 0 and snapshot_messages:
            # Legacy replay provenance cannot safely establish a boundary.
            # Its synchronous snapshots remain authoritative.
            return snapshot_messages, 0

        recovered = [dict(message) for message in snapshot_messages]
        applied = 0
        for event in events:
            if event.seq <= snapshot_seq:
                continue
            if event.kind == "context_view_committed":
                items = event.payload.get("items")
                if isinstance(items, list) and all(
                    isinstance(item, dict) for item in items
                ):
                    recovered = [dict(item) for item in items]
                    applied += 1
                continue
            if event.kind != "message_committed":
                continue
            message = event.payload.get("message")
            if isinstance(message, dict):
                recovered.append(dict(message))
                applied += 1
        return recovered, applied

    def _load_history_events(self, events_path: Path) -> list[HistoryEvent]:
        """Load events and atomically compact legacy full request replays."""
        events: list[HistoryEvent] = []
        if not events_path.exists():
            return events
        total_bytes = events_path.stat().st_size
        total_mb = total_bytes / (1024 * 1024)
        self._report_progress(f"Reading history ledger ({total_mb:.1f} MB)...")
        started = time.monotonic()
        next_percent = 10
        needs_migration = False
        parse_failed = False
        try:
            with events_path.open("rb") as stream:
                for line in stream:
                    try:
                        event = HistoryEvent.from_dict(json.loads(line))
                    except (json.JSONDecodeError, TypeError, UnicodeError, ValueError):
                        parse_failed = True
                        continue
                    event, compacted = self._compact_legacy_request_event(event)
                    events.append(event)
                    needs_migration = needs_migration or compacted
                    if total_bytes and time.monotonic() - started >= 0.5:
                        percent = min(100, int(stream.tell() * 100 / total_bytes))
                        if percent >= next_percent and percent < 100:
                            self._report_progress(
                                f"Reading history ledger... {percent}% "
                                f"({len(events)} event(s))."
                            )
                            next_percent = (percent // 10 + 1) * 10
        except (OSError, UnicodeError):
            return events
        if needs_migration and not parse_failed:
            self._report_progress(
                "Compacting legacy full-request snapshots in the history ledger..."
            )
            try:
                self._atomic_write_events(events_path, events)
            except OSError:
                # Migration is an optimization. A read-only or temporarily
                # unavailable store must not prevent session restoration.
                pass
            else:
                compacted_mb = events_path.stat().st_size / (1024 * 1024)
                self._report_progress(
                    f"History ledger compacted: {total_mb:.1f} MB -> "
                    f"{compacted_mb:.1f} MB."
                )
        self._report_progress(
            f"History ledger ready ({len(events)} event(s), "
            f"{time.monotonic() - started:.1f}s)."
        )
        return events

    @staticmethod
    def _compact_legacy_request_event(
        event: HistoryEvent,
    ) -> tuple[HistoryEvent, bool]:
        if event.kind != "request_committed":
            return event, False
        replay = event.payload.get("replay")
        if not isinstance(replay, dict) or "items" not in replay:
            return event, False

        payload = dict(event.payload)
        payload["replay"] = {
            "schema_version": replay.get("schema_version"),
            "view_id": replay.get("view_id"),
            "cache_epoch": replay.get("cache_epoch", 0),
            "history_version": replay.get("history_version", 0),
            "model_profile": replay.get("model_profile"),
            "provider_family": replay.get("provider_family"),
            "request_mode": replay.get("request_mode"),
            "instruction_count": len(replay.get("instructions") or ()),
            "tool_count": len(replay.get("tools") or ()),
            "item_count": len(replay.get("items") or ()),
            "stable_prefix_hash": replay.get("stable_prefix_hash"),
            "canonical_payload_hash": replay.get("canonical_payload_hash"),
            "migrated_from_full_replay": True,
        }
        return replace(event, payload=payload), True

    @staticmethod
    def _atomic_write_events(path: Path, events: list[HistoryEvent]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        temporary.replace(path)
