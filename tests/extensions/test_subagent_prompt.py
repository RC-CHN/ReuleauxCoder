from reuleauxcoder.extensions.subagent.manager import build_delegated_prompt


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
