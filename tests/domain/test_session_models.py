import json
from dataclasses import asdict

import pytest

from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.domain.images import ImageReference
from reuleauxcoder.domain.session.models import (
    MAX_RECENT_CONVERSATION_BYTES,
    MAX_RECENT_CONVERSATION_ENTRIES,
    MAX_RECENT_ENTRY_BYTES,
    Session,
    SessionRuntimeState,
)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_recent_preview_preserves_small_messages_and_recent_turn_selection():
    messages = [
        {"role": role, "content": f"{role} {turn} 中文"}
        for turn in range(5)
        for role in ("user", "assistant")
    ]
    session = Session(id="preview", model="test", saved_at="", messages=messages)
    assert session.get_recent_conversation() == messages[-6:]


@pytest.mark.parametrize("with_ledger", [False, True])
def test_recent_preview_keeps_image_references_in_bounded_projection(with_ledger):
    image = ImageReference("a" * 64, "b" * 64, "image/png", 40, 30, 128, 400, 300, "粘贴.png")
    message = {"role": "user", "content": [{"type": "text", "text": "Inspect this " * MAX_RECENT_ENTRY_BYTES}, image.to_part()]}
    ledger = HistoryLedger(session_id="preview")
    ledger.append("message_committed", {"message": message})
    session = Session(id="preview", model="test", saved_at="", messages=[message], history_events=list(ledger.events) if with_ledger else [])
    preview = session.get_recent_conversation()
    assert preview[-1]["images"] == [asdict(image)]
    assert len(_json_bytes(preview[-1])) <= MAX_RECENT_ENTRY_BYTES
    assert "Preview truncated" in preview[-1]["content"]


@pytest.mark.parametrize("text", ['中文🙂"\\\n\x00', "a"])
def test_recent_preview_bounds_encoded_bytes_without_changing_messages(text):
    content = text * (MAX_RECENT_ENTRY_BYTES * 2)
    messages = [{"role": "user", "content": "question"}] + [
        {"role": "assistant", "content": f"Reply {index}: {content}"}
        for index in range(8)
    ]
    session = Session(id="preview", model="test", saved_at="", messages=messages)
    preview = session.get_recent_conversation()
    assert len(_json_bytes(preview)) <= MAX_RECENT_CONVERSATION_BYTES
    assert all(len(_json_bytes(entry)) <= MAX_RECENT_ENTRY_BYTES for entry in preview)
    assert "Earlier preview entries omitted" in preview[0]["content"]
    assert preview[-1]["content"].startswith("Reply 7:")
    assert "Preview truncated" in preview[-1]["content"]
    assert "session history" in preview[-1]["content"]
    assert "\ufffd" not in preview[-1]["content"]
    assert messages[-1]["content"] == f"Reply 7: {content}"
    assert session.messages is messages


def test_recent_preview_bounds_many_small_entries_and_keeps_chronological_order():
    messages = [
        {"role": "assistant", "content": f"step {index}"}
        for index in range(1000)
    ]
    session = Session(id="preview", model="test", saved_at="", messages=messages)
    preview = session.get_recent_conversation()
    assert len(preview) == MAX_RECENT_CONVERSATION_ENTRIES + 1
    assert preview[1:] == messages[-MAX_RECENT_CONVERSATION_ENTRIES:]
    assert "Earlier preview entries omitted" in preview[0]["content"]


def test_recent_preview_also_bounds_unacknowledged_recovered_output():
    ledger = HistoryLedger(session_id="preview")
    text = "recovered output " * MAX_RECENT_ENTRY_BYTES
    ledger.append(
        "output_checkpoint",
        {
            "stream_id": "unfinished", "kind": "tool", "text": text,
            "tool_name": "shell", "replace": False,
        },
    )
    session = Session(
        id="preview", model="test", saved_at="", messages=[],
        history_events=list(ledger.events),
    )
    preview = session.get_recent_conversation()
    assert len(_json_bytes(preview)) <= MAX_RECENT_CONVERSATION_BYTES
    assert preview[0]["content"].startswith("[Recovered tool / shell")
    assert "Preview truncated" in preview[0]["content"]
    assert session.history_events[0].payload["text"] == text
    assert session.messages == []


def test_session_runtime_state_round_trips_plan_and_progress() -> None:
    state = SessionRuntimeState(
        plan_state={
            "revision": 2,
            "items": [
                {"step": "Verify", "active_form": "Verifying", "status": "in_progress"}
            ],
        },
        progress_state={
            "phase": "verifying",
            "summary": "Running tests",
            "revision": 3,
        },
    )

    restored = SessionRuntimeState.from_dict(state.to_dict())

    assert restored.plan_state == state.plan_state
    assert restored.progress_state == state.progress_state
