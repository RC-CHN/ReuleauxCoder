"""Session persistence adapter backed by JSON files."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from reuleauxcoder.domain.context.manager import ensure_message_token_counts
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.replay import (
    ReplayEnvelope,
    RequestEnvelope,
    align_item_provenance,
)
from reuleauxcoder.domain.history import HistoryEvent, HistoryLedger
from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency
from reuleauxcoder.domain.session.models import (
    Session,
    SessionMetadata,
    SessionRuntimeState,
)
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir

DEFAULT_SESSION_FINGERPRINT = "local"


class SessionStore:
    """File-backed store for conversation sessions."""

    def __init__(self, sessions_dir: Path | None = None):
        self._sessions_dir = sessions_dir or get_sessions_dir()
        self._lock = threading.RLock()

    @property
    def sessions_dir(self) -> Path:
        """Return the underlying session directory."""
        return self._sessions_dir

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
                    ledger.append_message(message, source="legacy_save_snapshot")
            elif is_exit:
                ledger.append_message(exit_message, source="session_exit")

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
                    base_replay.provider_family
                    if base_replay
                    else "openai-compatible"
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
                item_provenance=align_item_provenance(
                    saved_messages, ledger.events
                ),
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
                request_envelopes=list(request_envelopes),
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
            path = self._get_session_path(session_id)
            self._write_session_directory(session)
            self._atomic_write_json(path, session.to_dict())
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
        """Append a system message to an existing session, creating it if needed."""
        with self._lock:
            loaded = self.load(session_id)
            if loaded is None:
                self.save(
                    messages=[{"role": "system", "content": content}],
                    model=model,
                    session_id=session_id,
                    active_mode=active_mode,
                    runtime_state=runtime_state,
                    fingerprint=fingerprint,
                )
                return

            updated_messages = list(loaded.messages)
            appended = {"role": "system", "content": content}
            updated_messages.append(appended)
            ledger = HistoryLedger(loaded.history_events)
            ledger.append_message(appended, source="system_diagnostic")
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
            if data is not None and updated_messages != data.get("messages"):
                self._atomic_write_json(path, session.to_dict())
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
            for file_path in self._sessions_dir.glob("*.json"):
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

                    stat = file_path.stat()
                    try:
                        saved_at_rank = datetime.fromisoformat(session.saved_at)
                    except (TypeError, ValueError):
                        try:
                            saved_at_rank = datetime.strptime(
                                session.saved_at, "%Y-%m-%d %H:%M:%S"
                            )
                        except (TypeError, ValueError):
                            saved_at_rank = datetime.fromtimestamp(0)

                    ranked_sessions.append(
                        ((stat.st_mtime_ns, saved_at_rank, metadata.id), metadata)
                    )
                except (json.JSONDecodeError, KeyError):
                    continue

            for directory in self._sessions_dir.iterdir():
                if not directory.is_dir() or directory.name in seen_ids:
                    continue
                session = self._load_session_directory(directory)
                if session is None:
                    continue
                if fingerprint is not None and session.fingerprint != fingerprint:
                    continue
                metadata = SessionMetadata(
                    id=session.id or directory.name,
                    model=session.model,
                    saved_at=session.saved_at,
                    preview=session.get_preview(),
                    fingerprint=session.fingerprint,
                )
                try:
                    saved_at_rank = datetime.fromisoformat(session.saved_at)
                except (TypeError, ValueError):
                    saved_at_rank = datetime.fromtimestamp(0)
                ranked_sessions.append(
                    (
                        (directory.stat().st_mtime_ns, saved_at_rank, metadata.id),
                        metadata,
                    )
                )

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
    def _atomic_write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def _write_session_directory(self, session: Session) -> None:
        directory = self._get_session_directory(session.id)
        directory.mkdir(parents=True, exist_ok=True)
        requests_dir = directory / "requests"
        requests_dir.mkdir(parents=True, exist_ok=True)
        checkpoints_dir = directory / "checkpoints"
        checkpoints_dir.mkdir(parents=True, exist_ok=True)

        events_path = directory / "events.jsonl"
        existing_ids: set[str] = set()
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
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
            with events_path.open("a", encoding="utf-8") as stream:
                for event in new_events:
                    stream.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

        replay = session.replay_envelope
        if replay is not None:
            self._atomic_write_json(directory / "replay.json", replay.to_dict())
        for request in session.request_envelopes:
            self._atomic_write_json(
                requests_dir / f"{request.request_id}.json", request.to_dict()
            )
        for checkpoint in session.checkpoints:
            self._atomic_write_json(
                checkpoints_dir / f"{checkpoint.id}.json", checkpoint.to_dict()
            )
        manifest = session.to_dict()
        manifest.pop("messages", None)
        manifest.pop("replay_envelope", None)
        manifest.pop("request_envelopes", None)
        manifest.pop("checkpoints", None)
        manifest["checkpoint_ids"] = [item.id for item in session.checkpoints]
        self._atomic_write_json(directory / "manifest.json", manifest)

    def _load_session_directory(self, directory: Path) -> Session | None:
        manifest_path = directory / "manifest.json"
        replay_path = directory / "replay.json"
        if not manifest_path.exists() or not replay_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            replay = ReplayEnvelope.from_dict(
                json.loads(replay_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        if not replay.validate() or not replay.validate_protocol():
            return None

        events: list[HistoryEvent] = []
        events_path = directory / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(HistoryEvent.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
        requests: list[RequestEnvelope] = []
        requests_dir = directory / "requests"
        if requests_dir.exists():
            for request_path in sorted(requests_dir.glob("*.json")):
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
            for checkpoint_path in sorted(checkpoints_dir.glob("*.json")):
                try:
                    checkpoints.append(
                        CompactionCheckpoint.from_dict(
                            json.loads(checkpoint_path.read_text(encoding="utf-8"))
                        )
                    )
                except (OSError, KeyError, json.JSONDecodeError, TypeError, ValueError):
                    continue
        manifest["messages"] = list(replay.items)
        manifest["replay_envelope"] = replay.to_dict()
        manifest["history_events"] = [event.to_dict() for event in events]
        manifest["request_envelopes"] = [item.to_dict() for item in requests]
        manifest["checkpoints"] = [item.to_dict() for item in checkpoints]
        return Session.from_dict(manifest)
