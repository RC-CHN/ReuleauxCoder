from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from reuleauxcoder.app.rpc.codec import decode
from reuleauxcoder.infrastructure.rpc.peer import RpcError


def test_concurrent_retries_admit_one_ordinary_turn(runtime):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def run():
        calls.append(True)
        entered.set()
        release.wait(2)
        return "done"

    runtime.loop.run = run
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: runtime.server.submit(
                "same input", submission_id="send-1", session_generation=0,
            ), range(2)))
        assert entered.wait(1)
        assert [decode(reply).submission_id for reply in replies] == ["send-1"] * 2
        assert len(calls) == 1
        assert not runtime.agent.pending_user_steering()
        assert sum(message.get("content") == "same input" for message in runtime.agent.messages) == 1
    finally:
        release.set()
        runtime.client.wait_idle()


def test_steering_retry_after_snapshot_failure_does_not_readmit(runtime, monkeypatch):
    runtime.server._running = True
    runtime.agent._accepting_user_steering = True
    runtime.agent._current_turn_id = "active"
    calls = []
    original = runtime.agent.persist_runtime_snapshot

    def snapshot():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("slow disk failed")
        original()

    monkeypatch.setattr(runtime.agent, "persist_runtime_snapshot", snapshot)
    try:
        with pytest.raises(OSError):
            runtime.server.submit("direction", submission_id="steer-1", session_generation=0)
        result = decode(runtime.server.submit(
            "direction", submission_id="steer-1", session_generation=0,
        ))
        assert result.status == "steering"
        assert runtime.agent.pending_user_steering() == ("direction",)
        admissions = [e for e in runtime.agent.history_ledger.events if e.kind == "steering_admitted"]
        assert len(admissions) == 1
        assert admissions[0].payload["submission_id"] == "steer-1"
    finally:
        runtime.server._running = False


def test_submission_id_rejects_payload_reuse_and_stale_generation(runtime):
    runtime.server.submit("first", submission_id="unique", session_generation=0)
    runtime.client.wait_idle()
    with pytest.raises(RpcError, match="reused"):
        runtime.server.submit("different", submission_id="unique", session_generation=0)
    with pytest.raises(RpcError, match="Session changed"):
        runtime.server.submit("first", submission_id="unique", session_generation=-1)
