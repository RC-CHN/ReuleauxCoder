from __future__ import annotations

from types import SimpleNamespace
import time

import pytest

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.llm.models import LLMResponse
from reuleauxcoder.extensions.tools.builtin.subagent_control import (
    InterruptAgentTool,
    ListAgentsTool,
    SendMessageTool,
    SpawnAgentTool,
    WaitAgentTool,
)
from reuleauxcoder.extensions.tools.registry import iter_tool_classes
from reuleauxcoder.extensions.subagent.manager import SubagentCapacityError


class _Root:
    agent_id = "root"
    subagent_depth = 0

    def __init__(self):
        self.steered = False
        self.stopped = False

    def get_active_mode_config(self):
        return SimpleNamespace(
            allowed_subagent_modes=["explore", "execute", "verify"]
        )

    def _has_user_steering(self):
        return self.steered

    def stop_requested(self):
        return self.stopped


def _bind(tool, root=None):
    tool.bind_agent(root or _Root())
    return tool


def test_registry_exposes_codex_style_tools_and_hides_legacy_agent() -> None:
    names = {tool_type.name for tool_type in iter_tool_classes()}

    assert {
        "spawn_agent",
        "send_message",
        "list_agents",
        "wait_agent",
        "interrupt_agent",
    } <= names
    assert "agent" not in names


def test_spawn_agent_only_registers_async_job(monkeypatch) -> None:
    captured = {}

    class _Manager:
        def submit_background(self, **kwargs):
            captured.update(kwargs)
            return "sj_async"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: _Manager(),
    )
    outcome = _bind(SpawnAgentTool()).execute(
        message="inspect parser",
        mode="explore",
    )

    assert outcome.success
    assert outcome.metadata["job_id"] == "sj_async"
    assert captured["task"] == "inspect parser"
    assert "run_in_background" not in SpawnAgentTool.parameters["properties"]
    assert "detached" not in SpawnAgentTool.parameters["properties"]
    assert "tasks" not in SpawnAgentTool.parameters["properties"]


def test_spawn_agent_returns_structured_capacity_full_outcome(monkeypatch) -> None:
    class _Manager:
        def submit_background(self, **_kwargs):
            raise SubagentCapacityError(active=4)

    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: _Manager(),
    )

    outcome = _bind(SpawnAgentTool()).execute(message="one task too many")

    assert not outcome.success
    assert outcome.summary == "Subagent capacity full (4/4)"
    assert outcome.metadata == {
        "reason": "capacity_full",
        "active": 4,
        "max": 4,
    }
    assert "capacity_full" in outcome.content


def test_send_and_list_agents_are_compact_non_blocking_controls(monkeypatch) -> None:
    directive = SimpleNamespace(directive_id="sd_1")
    manager = SimpleNamespace(
        active_job_count=1,
        max_active_jobs=4,
        queue_message=lambda *args, **kwargs: directive,
        list_jobs=lambda: [
            SimpleNamespace(
                id="sj_1",
                status="running",
                mode="explore",
                task="inspect " + "many " * 80,
                started_at=time.time() - 2,
                finished_at=None,
                prompt_tokens=100,
                completion_tokens=20,
                max_tokens=1000,
                tool_calls=3,
                max_tool_calls=20,
                progress=("reading parser",),
                agent_id="sa_sj_1",
                last_activity_at=time.time() - 1,
                injected_to_parent=False,
            )
        ],
    )
    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: manager,
    )

    sent = _bind(SendMessageTool()).execute("sj_1", "focus on parser")
    listed = _bind(ListAgentsTool()).execute()

    assert sent.success and sent.metadata["directive_id"] == "sd_1"
    assert listed.success and listed.metadata["count"] == 1
    assert listed.summary == "1 running · 0 terminal · 0 delivered"
    assert len(listed.content.splitlines()) == 1
    assert len(listed.content) < 240
    assert "tools 3/20" in listed.content
    assert "sj_1/sa_sj_1" in listed.content
    assert "last_activity_seconds_ago" in listed.metadata["agents"][0]
    assert listed.metadata["agents"][0]["tokens"] == 120
    assert listed.metadata["capacity"] == {"active": 1, "max": 4}


def test_list_agents_marks_terminal_result_as_already_delivered(monkeypatch) -> None:
    manager = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="sj_done",
                status="completed",
                mode="explore",
                task="inspect parser",
                started_at=time.time() - 2,
                finished_at=time.time() - 1,
                prompt_tokens=100,
                completion_tokens=20,
                max_tokens=1000,
                tool_calls=3,
                max_tool_calls=20,
                progress=(),
                agent_id="sa_sj_done",
                last_activity_at=time.time() - 1,
                injected_to_parent=True,
            )
        ]
    )
    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: manager,
    )

    listed = _bind(ListAgentsTool()).execute()

    assert listed.summary == "0 running · 1 terminal · 1 delivered"
    assert listed.metadata["agents"][0]["delivered_to_parent"] is True
    assert "completed" in listed.content
    assert "delivered" in listed.content


def test_wait_agent_is_interruptible_by_human_without_cancelling_child(
    monkeypatch,
) -> None:
    root = _Root()
    root.steered = True
    manager = SimpleNamespace(
        wait_for_parent_activity=lambda *_args, **_kwargs: pytest.fail(
            "human steering should win before waiting"
        )
    )
    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: manager,
    )

    outcome = _bind(WaitAgentTool(), root).execute(timeout_ms=1000)

    assert outcome.success
    assert outcome.metadata["outcome"] == "steered"


def test_wait_agent_returns_existing_activity_without_cancelling(monkeypatch) -> None:
    root = _Root()
    manager = SimpleNamespace(
        wait_for_parent_activity=lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: manager,
    )

    outcome = _bind(WaitAgentTool(), root).execute(timeout_ms=1_000)

    assert outcome.success
    assert outcome.metadata["outcome"] == "activity"
    assert root.stopped is False


def test_wait_agent_timeout_does_not_touch_child_lifecycle(monkeypatch) -> None:
    root = _Root()
    waits = []
    manager = SimpleNamespace(
        wait_for_parent_activity=lambda *_args, **kwargs: (
            waits.append(kwargs["timeout"]) or False
        ),
    )
    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: manager,
    )

    outcome = _bind(WaitAgentTool(), root).execute(timeout_ms=100)

    assert outcome.success
    assert outcome.metadata["outcome"] == "timed_out"
    assert waits
    assert root.stopped is False


def test_interrupt_agent_reports_only_observed_terminal_state(monkeypatch) -> None:
    job = SimpleNamespace(
        status="cancelled",
        cancellation_id="cancel_sj_1_1",
        usage_uncertain=True,
    )
    manager = SimpleNamespace(
        cancel_job=lambda _job_id: True,
        wait_job=lambda _job_id, timeout: job,
    )
    monkeypatch.setattr(
        "reuleauxcoder.extensions.tools.builtin.subagent_control.get_subagent_manager",
        lambda _root: manager,
    )

    outcome = _bind(InterruptAgentTool()).execute("sj_1")

    assert outcome.success
    assert outcome.metadata == {
        "job_id": "sj_1",
        "status": "cancelled",
        "cancellation_id": "cancel_sj_1_1",
        "usage_uncertain": True,
    }
    assert "cancellation_id=cancel_sj_1_1" in outcome.content


class _OneResponseLLM:
    model = "test"
    last_dispatched_request = None

    def chat(self, **_kwargs):
        return LLMResponse(content="done")


def test_parent_loop_does_not_implicitly_wait_for_children() -> None:
    agent = Agent(llm=_OneResponseLLM(), tools=[])
    agent._has_subagent_activity = lambda: False

    assert agent._loop.run() == "done"
