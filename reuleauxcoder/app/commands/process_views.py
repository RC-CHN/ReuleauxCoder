"""Shared presentation contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessRowViewModel:
    session_id: str
    command: str
    cwd: str
    state: str
    stream_mode: str
    backend: str
    elapsed_seconds: float
    exit_code: int | None
    termination_reason: str | None
    output_truncated: bool
    output_decode_replaced: bool


@dataclass(frozen=True, slots=True)
class ProcessSessionsViewModel:
    sessions: tuple[ProcessRowViewModel, ...]
    view_type: str = "process_sessions"
    output_session_id: str | None = None
    output: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "sessions": [
                {
                    "session_id": session.session_id,
                    "command": session.command,
                    "cwd": session.cwd,
                    "state": session.state,
                    "stream_mode": session.stream_mode,
                    "backend": session.backend,
                    "elapsed_seconds": session.elapsed_seconds,
                    "exit_code": session.exit_code,
                    "termination_reason": session.termination_reason,
                    "output_truncated": session.output_truncated,
                    "output_decode_replaced": session.output_decode_replaced,
                }
                for session in self.sessions
            ]
        }
