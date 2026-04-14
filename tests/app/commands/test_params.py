import pytest

from reuleauxcoder.app.commands.params import (
    BoolParam,
    ChoiceParam,
    EnumParam,
    FloatParam,
    IntParam,
    ParamParseError,
    StrParam,
    parse_captures,
)


def test_str_param_strips_and_lowercases() -> None:
    parser = StrParam(strip=True, lower=True)
    assert parser.parse("  HeLLo ") == "hello"


def test_str_param_rejects_empty_when_non_empty() -> None:
    parser = StrParam(non_empty=True)
    with pytest.raises(ParamParseError):
        parser.parse("   ")


def test_enum_param_case_insensitive_returns_canonical_value() -> None:
    parser = EnumParam(values=frozenset({"allow", "deny"}), case_insensitive=True)
    assert parser.parse("ALLOW") == "allow"


def test_bool_param_parses_true_and_false_values() -> None:
    parser = BoolParam()
    assert parser.parse("yes") is True
    assert parser.parse("off") is False


def test_bool_param_rejects_invalid_value() -> None:
    parser = BoolParam()
    with pytest.raises(ParamParseError):
        parser.parse("maybe")


def test_int_param_applies_bounds() -> None:
    parser = IntParam(min_value=1, max_value=3)
    assert parser.parse("2") == 2
    with pytest.raises(ParamParseError):
        parser.parse("0")
    with pytest.raises(ParamParseError):
        parser.parse("4")


def test_float_param_applies_bounds() -> None:
    parser = FloatParam(min_value=0.0, max_value=1.0)
    assert parser.parse("0.5") == 0.5
    with pytest.raises(ParamParseError):
        parser.parse("-0.1")


def test_choice_param_case_insensitive_maps_value() -> None:
    parser = ChoiceParam(choices={"on": 1, "off": 0}, case_insensitive=True)
    assert parser.parse("ON") == 1


def test_parse_captures_returns_typed_values() -> None:
    parsed = parse_captures(
        {"enabled": "true", "count": "3"},
        {"enabled": BoolParam(), "count": IntParam(min_value=1)},
    )
    assert parsed == {"enabled": True, "count": 3}


def test_parse_captures_returns_none_for_missing_or_invalid_values() -> None:
    assert parse_captures({"enabled": "true"}, {"enabled": BoolParam(), "count": IntParam()}) is None
    assert parse_captures({"count": "abc"}, {"count": IntParam()}) is None
