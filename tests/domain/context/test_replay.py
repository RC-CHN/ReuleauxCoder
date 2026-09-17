from dataclasses import replace

import pytest

from reuleauxcoder.domain.context import replay as replay_module
from reuleauxcoder.domain.context.replay import (
    ItemProvenanceIndex,
    ReplayEnvelope,
    align_item_provenance,
    content_hash,
)
from reuleauxcoder.domain.history import HistoryEvent, HistoryLedger


def _replay(items: list[dict]) -> ReplayEnvelope:
    return ReplayEnvelope.create(
        session_id="session",
        cache_epoch=2,
        history_version=3,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[{"content": "stable\r\ntext", "role": "system"}],
        tools=[{"b": 2, "a": 1}],
        items=items,
    )


def test_canonical_hash_normalizes_object_keys_and_newlines() -> None:
    assert content_hash({"b": 2, "a": "x\r\ny"}) == content_hash({"a": "x\ny", "b": 2})


def test_replay_hash_detects_tampering() -> None:
    replay = _replay([{"role": "user", "content": "hello"}])
    tampered = replace(replay, items=({"role": "user", "content": "changed"},))
    assert replay.validate() is True
    assert tampered.validate() is False


def test_replay_hash_includes_wire_affecting_settings() -> None:
    replay = ReplayEnvelope.create(
        session_id="session",
        cache_epoch=0,
        history_version=0,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        request_settings={"temperature": 0.0, "max_tokens": 4096},
        instructions=[],
        tools=[],
        items=[],
    )

    changed = replace(replay, request_settings={"temperature": 0.7, "max_tokens": 4096})

    assert replay.validate() is True
    assert changed.validate() is False


def test_item_provenance_is_audited_but_does_not_change_provider_prefix_hash() -> None:
    first = ReplayEnvelope.create(
        session_id="session",
        cache_epoch=0,
        history_version=0,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[],
        tools=[],
        items=[{"role": "user", "content": "hello"}],
        item_provenance=[
            {
                "source_event_ids": ["event-1"],
                "artifact_refs": [],
                "checkpoint_id": None,
            }
        ],
    )
    second = ReplayEnvelope.create(
        session_id="session",
        cache_epoch=0,
        history_version=0,
        model_profile="model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[],
        tools=[],
        items=[{"role": "user", "content": "hello"}],
        item_provenance=[
            {
                "source_event_ids": ["event-2"],
                "artifact_refs": [],
                "checkpoint_id": None,
            }
        ],
    )

    assert first.validate() and second.validate()
    assert first.stable_prefix_hash == second.stable_prefix_hash
    assert first.canonical_payload_hash != second.canonical_payload_hash


def test_schema_two_replay_keeps_its_original_hash_contract() -> None:
    core = {
        "schema_version": 2,
        "session_id": "legacy",
        "cache_epoch": 1,
        "history_version": 2,
        "model_profile": "model",
        "provider_family": "openai-compatible",
        "request_mode": "chat-completions",
        "request_settings": {"temperature": 0.0},
        "instructions": [],
        "tools": [],
        "items": [{"role": "user", "content": "legacy"}],
    }
    stable = {
        key: core[key]
        for key in (
            "model_profile",
            "provider_family",
            "request_mode",
            "request_settings",
            "instructions",
            "tools",
            "items",
        )
    }
    replay = ReplayEnvelope.from_dict(
        {
            **core,
            "view_id": "legacy-view",
            "stable_prefix_hash": content_hash(stable),
            "canonical_payload_hash": content_hash(core),
        }
    )

    assert replay.item_provenance == ()
    assert replay.validate() is True


def test_schema_three_rejects_missing_aligned_provenance() -> None:
    replay = _replay([{"role": "user", "content": "hello"}])
    damaged = replace(replay, item_provenance=())
    assert damaged.validate() is False


def test_replay_protocol_rejects_missing_tool_result() -> None:
    replay = _replay(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        ]
    )
    assert replay.validate() is True
    assert replay.validate_protocol() is False


def test_provenance_reuses_committed_hashes_and_indexes_only_appends(monkeypatch) -> None:
    ledger = HistoryLedger()
    for index in range(1000):
        ledger.append_message(
            {"role": "user", "content": f"old-{index}"}, source="test"
        )
    items = [{"role": "user", "content": "old-999"}]
    initial = ledger.item_provenance(items)
    hashed = []

    def measured(value):
        hashed.append(value)
        return content_hash(value)

    monkeypatch.setattr(replay_module, "content_hash", measured)
    assert ledger.item_provenance(items) == initial
    assert hashed == items, "unchanged history must not be hashed again"

    hashed.clear()
    new_message = {"role": "assistant", "content": "new"}
    committed = ledger.append_message(new_message, source="test")
    ledger.append("request_payload_observed", {"item_count": 2})
    result = ledger.item_provenance([*items, new_message])
    assert len(hashed) == 3, "only the new commit and requested items need hashes"
    assert result[0] == initial[0]
    assert result[1]["source_event_ids"] == [committed.event_id]


def test_provenance_duplicate_messages_use_distinct_latest_commits() -> None:
    ledger = HistoryLedger()
    message = {"role": "user", "content": "repeat"}
    first = ledger.append_message(message, source="test")
    second = ledger.append_message(message, source="test")
    result = ledger.item_provenance([message] * 3, fallback_event_id="fallback")
    assert [item["source_event_ids"] for item in result] == [
        [second.event_id], [first.event_id], ["fallback"]
    ]
    result[0]["source_event_ids"].clear()
    assert ledger.item_provenance([message])[0]["source_event_ids"] == [second.event_id]


def test_provenance_caches_compacted_views_and_replaces_the_latest_view(monkeypatch) -> None:
    ledger = HistoryLedger()
    messages = [{"role": "user", "content": text} for text in ("summary", "recent")]
    view = ledger.append_context_view(
        messages, reason="compact", history_version=1, checkpoint_id="checkpoint-1"
    )
    expected = ledger.item_provenance(messages)
    assert all(item["source_event_ids"] == [view.event_id] for item in expected)
    assert all(item["checkpoint_id"] == "checkpoint-1" for item in expected)
    hashed = []

    def measured(value):
        hashed.append(value)
        return content_hash(value)

    monkeypatch.setattr(replay_module, "content_hash", measured)
    assert ledger.item_provenance(messages) == expected
    assert hashed == messages
    newer = ledger.append_context_view(
        messages, reason="replace", history_version=2, checkpoint_id="checkpoint-2"
    )
    result = ledger.item_provenance(messages)
    assert all(item["source_event_ids"] == [newer.event_id] for item in result)
    assert all(item["checkpoint_id"] == "checkpoint-2" for item in result)


def test_provenance_rebuilds_for_restored_replaced_and_truncated_histories() -> None:
    ledger = HistoryLedger()
    items = [{"role": "user", "content": text} for text in ("first", "middle", "last")]
    for item in items:
        ledger.append_message(item, source="test")
    events = ledger.events
    index = ItemProvenanceIndex()
    original = index.align(items, events)
    restored = tuple(HistoryEvent.from_dict(event.to_dict()) for event in events)
    assert index.align(items, restored) == original
    # Same length and unchanged first/last event must not hide a middle replacement.
    replaced = list(restored)
    replaced[2] = replace(replaced[2], payload={"message": {"role": "user", "content": "changed"}})
    changed_items = [items[0], {"role": "user", "content": "changed"}, items[2], items[1]]
    result = index.align(changed_items, replaced)
    assert result == align_item_provenance(changed_items, replaced)
    assert result[1]["source_event_ids"] == [replaced[2].event_id]
    assert result[-1]["source_event_ids"] == []
    assert index.align(items, restored[:2]) == align_item_provenance(items, restored[:2])
    assert all(not item["source_event_ids"] for item in index.align(items, []))
    assert index.align(items, events) == original


def test_provenance_restore_does_not_hash_superseded_views(monkeypatch) -> None:
    ledger = HistoryLedger()
    ledger.append_context_view(
        [{"role": "user", "content": f"obsolete-{index}"} for index in range(1000)],
        reason="old compact", history_version=1,
    )
    message = {"role": "user", "content": "current"}
    current = ledger.append_context_view(
        [message], reason="latest compact", history_version=2
    )
    hashed = []

    def measured(value):
        hashed.append(value)
        return content_hash(value)

    monkeypatch.setattr(replay_module, "content_hash", measured)
    index = ItemProvenanceIndex()
    assert index.align([message], ledger.events)[0]["source_event_ids"] == [current.event_id]
    assert hashed == [message, message]
    # A reset must also discard a cached view, not only message hashes.
    assert index.align([message], [])[0]["source_event_ids"] == []


def test_failed_provenance_update_does_not_poison_later_alignment() -> None:
    ledger = HistoryLedger()
    message = {"role": "user", "content": "valid"}
    ledger.append_message(message, source="test")
    invalid = replace(ledger.events[0], payload={"message": {"content": object()}})
    index = ItemProvenanceIndex()
    with pytest.raises(TypeError):
        index.align([message], [*ledger.events, invalid])
    assert index.align([message] * 2, ledger.events) == align_item_provenance([message] * 2, ledger.events)
