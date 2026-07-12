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
    assert content_hash({"b": 2, "a": "x\r\ny"}) == content_hash(
        {"a": "x\ny", "b": 2}
    )


def test_replay_hash_detects_tampering() -> None:
    replay = _replay([{"role": "user", "content": "hello"}])
    tampered = replace(
        replay, items=({"role": "user", "content": "changed"},)
    )
    assert replay.validate() is True
    assert tampered.validate() is False


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
