import threading
import json
from pathlib import Path

from reuleauxcoder.domain.context.manager import MESSAGE_TOKEN_KEY
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.replay import ReplayEnvelope
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.domain.session.models import Session, SessionRuntimeState
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


def test_session_preview_uses_latest_real_user_request(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {"role": "user", "content": "initial topic"},
            {"role": "assistant", "content": "old answer"},
            {
                "role": "user",
                "content": "[SESSION_RESUME] User returned.\n\nlatest request",
            },
            {"role": "assistant", "content": "latest answer"},
            {"role": "user", "content": "[SESSION_EXIT] User left."},
        ],
        model="m1",
    )

    metadata = next(item for item in store.list() if item.id == session_id)

    assert metadata.preview == "latest request"


def test_session_listing_uses_directory_metadata_without_full_restore(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "metadata only"}], model="m1"
    )

    monkeypatch.setattr(
        store,
        "_load_session_directory",
        lambda _directory: (_ for _ in ()).throw(
            AssertionError("listing must not restore full sessions")
        ),
    )

    listed = store.list()

    assert [item.id for item in listed] == [session_id]
    assert listed[0].preview == "metadata only"


def test_session_store_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    messages = [{"role": "user", "content": "hello world"}]

    session_id = store.save(
        messages=messages,
        model="gpt-4o",
        total_prompt_tokens=12,
        total_completion_tokens=34,
        active_mode="coder",
        runtime_state=SessionRuntimeState(
            model="gpt-4o",
            active_mode="coder",
            llm_debug_trace=True,
        ),
        fingerprint="local",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["role"] == messages[0]["role"]
    assert loaded.messages[0]["content"] == messages[0]["content"]
    assert isinstance(loaded.messages[0].get(MESSAGE_TOKEN_KEY), int)
    assert loaded.model == "gpt-4o"
    assert loaded.total_prompt_tokens == 12
    assert loaded.total_completion_tokens == 34
    assert loaded.active_mode == "coder"
    assert loaded.runtime_state.model == "gpt-4o"
    assert loaded.runtime_state.active_mode == "coder"
    assert loaded.runtime_state.llm_debug_trace is True
    assert loaded.fingerprint == "local"


def test_new_session_layout_separates_full_ledger_from_runtime_view(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    ledger = HistoryLedger()
    raw = {"role": "tool", "tool_call_id": "call", "content": "raw-line\n" * 200}
    ledger.append_message(raw, source="tool_result")
    view = [
        {
            "role": "system",
            "content": "[Context checkpoint summary]\nTool output archived.",
        }
    ]
    replay = ReplayEnvelope.create(
        session_id=None,
        cache_epoch=1,
        history_version=1,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[{"role": "system", "content": "stable"}],
        tools=[],
        items=view,
    )

    session_id = store.save(
        messages=view,
        model="model",
        history_events=list(ledger.events),
        replay_envelope=replay,
    )

    directory = tmp_path / session_id
    assert (directory / "manifest.json").exists()
    assert (directory / "events.jsonl").exists()
    assert (directory / "replay.json").exists()
    assert "raw-line" in (directory / "events.jsonl").read_text(encoding="utf-8")
    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"].startswith("[Context checkpoint summary]")
    assert loaded.history_completeness == "complete"
    assert loaded.replay_envelope is not None
    assert loaded.replay_envelope.validate()


def test_compaction_checkpoints_are_immutable_session_artifacts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    checkpoint = CompactionCheckpoint.create(
        trigger="quality_wall",
        strategy=["summarize"],
        source_history_version=3,
        replacement_history=[
            {"role": "system", "content": "[Context checkpoint summary]\nstate"}
        ],
        tokens_before=60_000,
        tokens_after=40_000,
        preserved_rounds=3,
        cache_epoch=2,
        actual_prompt_tokens=61_000,
        cached_input_tokens=50_000,
    )
    session_id = store.save(
        messages=list(checkpoint.replacement_history),
        model="model",
        checkpoints=[checkpoint],
    )

    checkpoint_path = tmp_path / session_id / "checkpoints" / f"{checkpoint.id}.json"
    assert checkpoint_path.exists()
    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.checkpoints == [checkpoint]


def test_events_jsonl_only_appends_new_event_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    ledger = HistoryLedger()
    ledger.append_message({"role": "user", "content": "one"}, source="user")
    session_id = store.save(
        messages=[{"role": "user", "content": "one"}],
        model="model",
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    first_lines = events_path.read_text(encoding="utf-8").splitlines()

    store.save(
        messages=[{"role": "user", "content": "one"}],
        model="model",
        session_id=session_id,
        history_events=list(ledger.events),
    )
    assert events_path.read_text(encoding="utf-8").splitlines() == first_lines

    ledger.append_message({"role": "assistant", "content": "two"}, source="assistant")
    store.save(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ],
        model="model",
        session_id=session_id,
        history_events=list(ledger.events),
    )
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 4


def test_load_compacts_legacy_request_replay_without_changing_current_context(
    tmp_path: Path,
) -> None:
    progress = []
    store = SessionStore(tmp_path, progress=progress.append)
    ledger = HistoryLedger()
    request = ledger.append(
        "request_committed",
        {"replay": {"items": ["x" * 300_000]}},
    )
    usage = ledger.append(
        "usage_observed",
        {
            "actual_prompt_tokens": 10,
            "cached_input_tokens": 5,
            "local_request_estimate": 8,
            "local_history_estimate": 4,
            "request_boundary": "turn:1",
            "model_profile": "model",
        },
    )
    session_id = store.save(
        messages=[{"role": "user", "content": "hello"}],
        model="model",
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(event.to_dict(), ensure_ascii=False)
            for event in (request, usage)
        )
        + "\n",
        encoding="utf-8",
    )
    replay_path = tmp_path / session_id / "replay.json"
    replay_before = ReplayEnvelope.from_dict(
        json.loads(replay_path.read_text(encoding="utf-8"))
    )
    size_before = events_path.stat().st_size

    loaded = store.load(session_id)

    assert loaded is not None
    restored_request = next(
        event for event in loaded.history_events if event.event_id == request.event_id
    )
    assert restored_request.seq == request.seq
    assert restored_request.payload["replay"]["item_count"] == 1
    assert "items" not in restored_request.payload["replay"]
    assert any(event.event_id == usage.event_id for event in loaded.history_events)
    assert events_path.stat().st_size < size_before / 100
    assert loaded.messages[0]["content"] == "hello"
    assert loaded.replay_envelope is not None
    assert loaded.replay_envelope.validate()
    assert loaded.replay_envelope.stable_prefix_hash == replay_before.stable_prefix_hash
    assert loaded.replay_envelope.cache_epoch == replay_before.cache_epoch
    assert any(message.startswith("Reading history ledger (") for message in progress)
    assert any(message.startswith("History ledger compacted:") for message in progress)


def test_incremental_exit_appends_only_new_exit_events(tmp_path: Path) -> None:
    ledger = HistoryLedger()
    message = {"role": "user", "content": "one"}
    ledger.append_message(message, source="user")
    session_id = SessionStore(tmp_path).save(
        messages=[message],
        model="model",
        history_events=list(ledger.events),
    )
    events_path = tmp_path / session_id / "events.jsonl"
    before = events_path.read_text(encoding="utf-8").splitlines()

    SessionStore(tmp_path).save(
        messages=[message],
        model="model",
        session_id=session_id,
        is_exit=True,
        history_events=list(ledger.events),
        incremental=True,
        events_already_persisted=True,
    )

    after = events_path.read_text(encoding="utf-8").splitlines()
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2
    loaded = SessionStore(tmp_path).load(session_id)
    assert loaded is not None
    assert loaded.messages[-1]["content"].startswith("[SESSION_EXIT]")


def test_new_save_does_not_duplicate_canonical_session_as_legacy_file(
    tmp_path: Path,
) -> None:
    ledger = HistoryLedger()
    ledger.append_message({"role": "user", "content": "one"}, source="user")
    session_id = SessionStore(tmp_path).save(
        messages=[{"role": "user", "content": "one"}],
        model="model",
        history_events=list(ledger.events),
    )

    assert (tmp_path / session_id / "manifest.json").exists()
    assert (tmp_path / session_id / "replay.json").exists()
    assert not (tmp_path / f"{session_id}.json").exists()


def test_valid_canonical_load_removes_stale_legacy_duplicate(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "canonical"}], model="model"
    )
    duplicate = tmp_path / f"{session_id}.json"
    duplicate.write_text("{}", encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert loaded.messages[0]["content"] == "canonical"
    assert not duplicate.exists()


def test_tampered_replay_does_not_claim_canonical_directory_restore(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "safe fallback"}], model="model"
    )
    canonical = store.load(session_id)
    assert canonical is not None
    legacy_path = tmp_path / f"{session_id}.json"
    legacy_path.write_text(
        json.dumps(canonical.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    replay_path = tmp_path / session_id / "replay.json"
    replay_data = json.loads(replay_path.read_text(encoding="utf-8"))
    replay_data["items"][0]["content"] = "tampered"
    replay_path.write_text(json.dumps(replay_data), encoding="utf-8")

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "safe fallback"
    assert not legacy_path.exists()
    reloaded = store.load(session_id)
    assert reloaded is not None
    assert reloaded.messages[0]["content"] == "safe fallback"


def test_session_store_save_with_exit_appends_exit_marker(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "bye"}],
        model="gpt-4o",
        is_exit=True,
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[-1]["role"] == "user"
    assert loaded.messages[-1]["content"].startswith("[SESSION_EXIT]")
    assert isinstance(loaded.messages[-1].get(MESSAGE_TOKEN_KEY), int)
    assert store.get_exit_time(loaded.messages) is not None


def test_session_store_places_synthetic_tool_result_before_exit_marker(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": "{}"},
                    }
                ],
            }
        ],
        model="gpt-4o",
        is_exit=True,
    )

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["role"] for message in loaded.messages] == [
        "assistant",
        "tool",
        "user",
    ]
    assert loaded.messages[1]["tool_call_id"] == "call_pending"
    assert loaded.messages[2]["content"].startswith("[SESSION_EXIT]")


def test_session_store_append_system_message_updates_existing_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "hello"}], model="gpt-4o"
    )

    store.append_system_message(
        session_id,
        "gpt-4o",
        "[LLM_ERROR_DIAGNOSTIC] path=/tmp/demo.json error=BadRequestError: boom",
        active_mode="coder",
    )

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[-1]["role"] == "user"
    assert loaded.messages[-1]["content"].startswith("<session_diagnostic")
    assert "[LLM_ERROR_DIAGNOSTIC]" in loaded.messages[-1]["content"]
    assert isinstance(loaded.messages[-1].get(MESSAGE_TOKEN_KEY), int)


def test_session_store_load_backfills_missing_message_token_counts(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "legacy_missing_tokens"
    path = tmp_path / f"{session_id}.json"
    legacy = Session(
        id=session_id,
        model="gpt-4o",
        saved_at="2026-01-01T00:00:00",
        messages=[{"role": "user", "content": "hello"}],
    )
    path.write_text(json.dumps(legacy.to_dict(), ensure_ascii=False), encoding="utf-8")

    loaded = store.load(session_id)
    assert loaded is not None
    assert isinstance(loaded.messages[0].get(MESSAGE_TOKEN_KEY), int)
    assert not path.exists()
    assert (tmp_path / session_id / "replay.json").exists()


def test_session_store_load_repairs_legacy_out_of_order_tool_results(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    session_id = "legacy_tool_order"
    path = tmp_path / f"{session_id}.json"
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_legacy",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    }
    legacy = Session(
        id=session_id,
        model="gpt-4o",
        saved_at="2026-01-01T00:00:00",
        messages=[
            assistant,
            {"role": "user", "content": "[SESSION_EXIT] old"},
            {
                "role": "tool",
                "tool_call_id": "call_legacy",
                "content": "recovered later",
            },
        ],
    )
    path.write_text(json.dumps(legacy.to_dict(), ensure_ascii=False), encoding="utf-8")

    loaded = store.load(session_id)

    assert loaded is not None
    assert [message["role"] for message in loaded.messages] == [
        "assistant",
        "tool",
        "user",
    ]
    persisted = store.load(session_id)
    assert persisted is not None
    assert [message["role"] for message in persisted.messages] == [
        "assistant",
        "tool",
        "user",
    ]


def test_session_store_list_filters_by_fingerprint(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "first"}], model="m1", fingerprint="local"
    )
    remote_id = store.save(
        messages=[{"role": "user", "content": "second"}],
        model="m2",
        fingerprint="remote:abc",
    )
    (tmp_path / "broken.json").write_text("{not-json}", encoding="utf-8")

    local_sessions = store.list(limit=10, fingerprint="local")
    remote_sessions = store.list(limit=10, fingerprint="remote:abc")
    all_sessions = store.list(limit=10, fingerprint=None)

    assert [item.id for item in local_sessions] == [local_id]
    assert [item.id for item in remote_sessions] == [remote_id]
    assert {item.id for item in all_sessions} == {local_id, remote_id}
    assert store.get_latest(fingerprint="local") is not None
    assert store.get_latest(fingerprint="remote:abc") is not None


def test_session_store_get_latest_prefers_recently_updated_session(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    first_id = store.save(
        messages=[{"role": "user", "content": "first"}], model="m1", fingerprint="local"
    )
    second_id = store.save(
        messages=[{"role": "user", "content": "second"}],
        model="m2",
        fingerprint="local",
    )

    # Update the older session after the newer one was created.
    store.save(
        messages=[{"role": "user", "content": "first-updated"}],
        model="m1",
        session_id=first_id,
        fingerprint="local",
    )

    latest = store.get_latest(fingerprint="local")
    assert latest is not None
    assert latest.id == first_id
    assert latest.id != second_id


def test_session_store_get_exit_time_returns_none_without_marker() -> None:
    messages = [{"role": "user", "content": "hello"}]
    assert SessionStore.get_exit_time(messages) is None


def test_session_store_load_missing_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.load("missing") is None


def test_session_store_concurrent_save_keeps_sessions_readable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session_id = store.save(
        messages=[{"role": "user", "content": "seed"}], model="m1", fingerprint="local"
    )

    def update_existing(index: int) -> None:
        store.save(
            messages=[{"role": "user", "content": f"existing-{index}"}],
            model="m1",
            session_id=session_id,
            fingerprint="local",
        )

    def create_new(index: int) -> None:
        store.save(
            messages=[{"role": "user", "content": f"new-{index}"}],
            model="m2",
            fingerprint="remote:abc",
        )

    threads = [
        threading.Thread(target=update_existing, args=(i,)) for i in range(4)
    ] + [threading.Thread(target=create_new, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    loaded = store.load(session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"].startswith("existing-")

    local_latest = store.get_latest(fingerprint="local")
    remote_latest = store.get_latest(fingerprint="remote:abc")
    assert local_latest is not None
    assert local_latest.id == session_id
    assert remote_latest is not None
    assert remote_latest.fingerprint == "remote:abc"

    listed = store.list(limit=20, fingerprint=None)
    assert len(listed) >= 5
