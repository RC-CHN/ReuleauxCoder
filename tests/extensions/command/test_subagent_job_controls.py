from reuleauxcoder.extensions.command.builtin.subagent_jobs import (
    ControlSubagentJobCommand,
    _parse_control_job,
)


def test_parse_subagent_message_command() -> None:
    parsed = _parse_control_job("/jobs message sj_123 focus on tests", None)
    assert parsed == ControlSubagentJobCommand(
        action="message", job_id="sj_123", message="focus on tests"
    )


def test_parse_subagent_cleanup_command() -> None:
    parsed = _parse_control_job("/jobs cleanup sj_123", None)
    assert parsed == ControlSubagentJobCommand(action="cleanup", job_id="sj_123")
