from reuleauxcoder.app.commands.matchers import match_template, matches_any, normalize_input


def test_normalize_input_collapses_whitespace() -> None:
    assert normalize_input("  hello\n\tworld   ") == "hello world"


def test_normalize_input_empty_result_for_whitespace_only() -> None:
    assert normalize_input("  \t\n ") == ""


def test_match_template_exact_match() -> None:
    assert match_template("/sessions", "/sessions") == {}


def test_match_template_single_placeholder() -> None:
    assert match_template("/mode coder", "/mode {name}") == {"name": "coder"}


def test_match_template_greedy_placeholder_captures_remaining_tokens() -> None:
    assert match_template("/session latest saved", "/session {target+}") == {"target": "latest saved"}


def test_match_template_greedy_placeholder_must_be_last() -> None:
    assert match_template("/x hello world", "/x {first+} {second}") is None


def test_match_template_case_insensitive_literals() -> None:
    assert match_template("/MODE Coder", "/mode {name}", case_insensitive=True) == {"name": "Coder"}


def test_match_template_rejects_extra_tokens() -> None:
    assert match_template("/sessions extra", "/sessions") is None


def test_matches_any_true_when_one_template_matches() -> None:
    assert matches_any("/mode", ("/sessions", "/mode")) is True


def test_matches_any_false_when_none_match() -> None:
    assert matches_any("/other", ("/sessions", "/mode")) is False
