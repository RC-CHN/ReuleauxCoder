import pytest

from reuleauxcoder.extensions.subagent.worker_protocol import (
    WorkerEnvelope,
    WorkerSpec,
    WorkerToolSpec,
)


def test_worker_spec_is_json_safe_and_round_trips_exact_replay_messages() -> None:
    spec = WorkerSpec(
        job_id="sj_1",
        agent_id="child_1",
        session_id="session",
        session_generation=2,
        worker_generation=3,
        cancellation_epoch=4,
        delegated_prompt="inspect parser",
        llm_kwargs={"model": "test", "api_key": "secret"},
        tools=(
            WorkerToolSpec(
                name="read_file",
                description="read",
                parameters={"type": "object", "properties": {}},
            ),
        ),
        max_context_tokens=128000,
        max_rounds=20,
        max_tool_calls=80,
        max_tokens=40000,
        replay_messages=(
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "stable prefix"},
        ),
    )

    restored = WorkerSpec.from_dict(spec.to_dict())

    assert restored == spec
    assert restored.replay_messages == spec.replay_messages


def test_worker_envelope_rejects_payload_tampering_and_old_versions() -> None:
    envelope = WorkerEnvelope(
        type="tool_request",
        job_id="sj_1",
        agent_id="child_1",
        session_generation=1,
        worker_generation=1,
        cancellation_epoch=0,
        sequence=1,
        payload={"call_id": "call_1", "name": "read_file"},
    )
    encoded = envelope.to_dict()

    assert WorkerEnvelope.from_dict(encoded) == envelope

    encoded["payload"]["name"] = "write_file"
    with pytest.raises(ValueError, match="hash mismatch"):
        WorkerEnvelope.from_dict(encoded)

    encoded = envelope.to_dict()
    encoded["version"] = 999
    with pytest.raises(ValueError, match="unsupported worker protocol"):
        WorkerEnvelope.from_dict(encoded)
