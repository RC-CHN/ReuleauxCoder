from reuleauxcoder.extensions.subagent.manager import (
    _normalize_subagent_terminal_status,
    _parse_delegated_final_response,
    build_delegated_prompt,
)


def test_delegated_prompt_has_non_recursive_control_and_final_contract() -> None:
    prompt = build_delegated_prompt(
        task="Inspect the parser",
        parent_context="User requested a parser audit.",
        context_mode="recent",
        worktree_path="/tmp/worktree",
    )

    assert "Do not create or delegate to other agents" in prompt
    assert "do not modify the root plan" in prompt
    assert "report_progress" in prompt
    assert "report_to_parent" in prompt
    assert "request_guidance" in prompt
    assert prompt.index("1. Conclusion") < prompt.index("2. Evidence")
    assert prompt.index("2. Evidence") < prompt.index("3. Changes and artifacts")
    assert prompt.index("3. Changes and artifacts") < prompt.index(
        "4. Unresolved issues"
    )
    assert prompt.index("4. Unresolved issues") < prompt.index("5. Confidence")
    assert "[Isolated worktree]\n/tmp/worktree" in prompt


def test_delegated_final_response_parser_preserves_all_contract_sections() -> None:
    parsed = _parse_delegated_final_response(
        """1. Conclusion — parser is correct
2. Evidence —
- pytest passed
- read parser.py
3. Changes and artifacts —
- parser.py
4. Unresolved issues — None
5. Confidence — medium because one platform was not tested"""
    )

    assert parsed == {
        "conclusion": "parser is correct",
        "evidence": ["pytest passed", "read parser.py"],
        "artifacts": ["parser.py"],
        "unresolved": [],
        "confidence": "medium",
        "missing": [],
    }


def test_budget_markers_cannot_be_reported_as_successful_terminal() -> None:
    for summary in (
        "(sub-agent token budget exhausted before request)",
        "(sub-agent round budget exhausted)",
        "(reached maximum tool-call rounds)",
    ):
        assert _normalize_subagent_terminal_status("ok", summary) == "failed"

    assert _normalize_subagent_terminal_status("ok", "finished normally") == "ok"
