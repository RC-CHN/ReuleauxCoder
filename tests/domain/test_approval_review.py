import json
import threading
import time
from types import SimpleNamespace

from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.approval_review import AutoReviewJudge
from reuleauxcoder.domain.history import HistoryLedger


class _LLM:
    def __init__(self, content: str, delay: float = 0) -> None:
        self.content = content
        self.delay = delay
        self.messages = None

    def chat(self, *, messages, tools=None):
        self.messages = messages
        time.sleep(self.delay)
        return SimpleNamespace(content=self.content)


def _agent():
    ledger = HistoryLedger(session_id="session", agent_id="agent")
    ledger.append_message(
        {"role": "user", "content": "delete the temporary file"},
        source="user_input",
    )
    return SimpleNamespace(
        messages=[
            {"role": "user", "content": "delete the temporary file"},
            {"role": "assistant", "content": "I think this is safe"},
            {"role": "tool", "content": "untrusted output"},
        ],
        _current_turn_id="turn",
        request_stop=lambda: None,
        history_ledger=ledger,
    )


def test_auto_review_uses_user_authorization_not_agent_prose_or_tool_output() -> None:
    agent = _agent()
    event_id = agent.history_ledger.events[0].event_id
    llm = _LLM(
        '{"decision":"allow","reason":"explicitly requested",'
        f'"authorization_event_ids":["{event_id}"]}}'
    )
    judge = AutoReviewJudge(agent=agent, llm=llm)
    decision = judge(ApprovalRequest(tool_name="shell", tool_args={"command": "rm temp"}))
    payload = json.loads(llm.messages[1]["content"])
    encoded = json.dumps(payload)
    assert decision.approved is True
    assert "delete the temporary file" in encoded
    assert "I think this is safe" not in encoded
    assert "untrusted output" not in encoded


def test_auto_review_timeout_fails_closed() -> None:
    judge = AutoReviewJudge(
        agent=_agent(),
        llm=_LLM('{"decision":"allow","reason":"late"}', delay=0.05),
        timeout_seconds=0.01,
    )
    # Constructor clamps production timeout to >=1s; lower it for a fast unit test.
    judge.timeout_seconds = 0.01
    decision = judge(ApprovalRequest(tool_name="shell"))
    assert decision.approved is False
    assert "timed out" in (decision.reason or "")


def test_auto_review_allow_requires_known_authorization_event() -> None:
    judge = AutoReviewJudge(
        agent=_agent(),
        llm=_LLM(
            '{"decision":"allow","reason":"claimed",'
            '"authorization_event_ids":["message:999"]}'
        ),
    )
    decision = judge(ApprovalRequest(tool_name="shell"))
    assert decision.approved is False
    assert "authorization evidence" in (decision.reason or "")


def test_auto_review_rejects_json_wrapped_in_prose() -> None:
    judge = AutoReviewJudge(
        agent=_agent(),
        llm=_LLM(
            'sure {"decision":"allow","reason":"claimed",'
            '"authorization_event_ids":["message:0"]}'
        ),
    )
    decision = judge(ApprovalRequest(tool_name="shell"))
    assert decision.approved is False
    assert "invalid JSON" in (decision.reason or "")


def test_three_denials_trip_turn_circuit_breaker() -> None:
    stopped = threading.Event()
    agent = _agent()
    agent.request_stop = stopped.set
    judge = AutoReviewJudge(
        agent=agent, llm=_LLM('{"decision":"deny","reason":"risk"}')
    )
    for _ in range(3):
        judge(ApprovalRequest(tool_name="shell"))
    assert stopped.is_set()
