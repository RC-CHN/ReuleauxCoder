import pytest

from reuleauxcoder.app.commands.params import (
    EnumParam,
    ParamParseError,
    StrParam,
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
