from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType


def test_agent_event_chat_start_contains_user_input() -> None:
    event = AgentEvent.chat_start("hello")
    assert event.event_type is AgentEventType.CHAT_START
    assert event.data == {"user_input": "hello"}


def test_agent_event_tool_call_start_contains_name_and_args() -> None:
    event = AgentEvent.tool_call_start("shell", {"command": "ls"})
    assert event.event_type is AgentEventType.TOOL_CALL_START
    assert event.tool_name == "shell"
    assert event.tool_args == {"command": "ls"}


def test_agent_event_tool_call_end_truncates_long_result() -> None:
    result = "x" * 600
    event = AgentEvent.tool_call_end("read_file", result, success=False)
    assert event.event_type is AgentEventType.TOOL_CALL_END
    assert event.tool_name == "read_file"
    assert event.tool_success is False
    assert event.tool_result == "x" * 500


def test_agent_event_subagent_completed_contains_payload() -> None:
    event = AgentEvent.subagent_completed(
        job_id="job-1",
        mode="explore",
        task="scan repo",
        status="ok",
        result="done",
        error=None,
    )
    assert event.event_type is AgentEventType.SUBAGENT_COMPLETED
    assert event.data["job_id"] == "job-1"
    assert event.data["mode"] == "explore"
    assert event.data["status"] == "ok"
    assert event.data["result"] == "done"


def test_agent_event_error_contains_message() -> None:
    event = AgentEvent.error("boom")
    assert event.event_type is AgentEventType.ERROR
    assert event.error_message == "boom"
