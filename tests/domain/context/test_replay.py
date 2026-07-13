from dataclasses import replace

from reuleauxcoder.domain.context.replay import ReplayEnvelope, content_hash


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
