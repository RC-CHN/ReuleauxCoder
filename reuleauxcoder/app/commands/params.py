"""Typed parameter parsers for template-matched command captures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class ParamParseError(ValueError):
    """Raised when a parameter value cannot be parsed."""


class ParamParser:
    """Base parser interface for captured template parameters."""

    def parse(self, raw: str) -> Any:  # pragma: no cover - interface method
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class StrParam(ParamParser):
    strip: bool = True
    lower: bool = False
    non_empty: bool = False
    reject: frozenset[str] = field(default_factory=frozenset)

    def parse(self, raw: str) -> str:
        value = raw.strip() if self.strip else raw
        if self.lower:
            value = value.lower()
        if self.non_empty and not value:
            raise ParamParseError("value must be non-empty")
        if value in self.reject:
            raise ParamParseError(f"value '{value}' is not allowed")
        return value


@dataclass(frozen=True, slots=True)
class EnumParam(ParamParser):
    values: frozenset[str]
    case_insensitive: bool = False

    def parse(self, raw: str) -> str:
        value = raw.strip()
        if self.case_insensitive:
            lookup = {item.lower(): item for item in self.values}
            key = value.lower()
            if key not in lookup:
                raise ParamParseError(f"value '{value}' is not in enum")
            return lookup[key]
        if value not in self.values:
            raise ParamParseError(f"value '{value}' is not in enum")
        return value


@dataclass(frozen=True, slots=True)
class BoolParam(ParamParser):
    true_values: frozenset[str] = field(
        default_factory=lambda: frozenset({"1", "true", "yes", "on"})
    )
    false_values: frozenset[str] = field(
        default_factory=lambda: frozenset({"0", "false", "no", "off"})
    )
    case_insensitive: bool = True

    def parse(self, raw: str) -> bool:
        value = raw.strip()
        if self.case_insensitive:
            value = value.lower()
        if value in self.true_values:
            return True
        if value in self.false_values:
            return False
        raise ParamParseError(f"value '{raw}' is not a valid boolean")


@dataclass(frozen=True, slots=True)
class IntParam(ParamParser):
    min_value: int | None = None
    max_value: int | None = None

    def parse(self, raw: str) -> int:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ParamParseError(f"value '{raw}' is not a valid integer") from exc
        if self.min_value is not None and value < self.min_value:
            raise ParamParseError(f"value {value} is below min {self.min_value}")
        if self.max_value is not None and value > self.max_value:
            raise ParamParseError(f"value {value} is above max {self.max_value}")
        return value


@dataclass(frozen=True, slots=True)
class FloatParam(ParamParser):
    min_value: float | None = None
    max_value: float | None = None

    def parse(self, raw: str) -> float:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ParamParseError(f"value '{raw}' is not a valid float") from exc
        if self.min_value is not None and value < self.min_value:
            raise ParamParseError(f"value {value} is below min {self.min_value}")
        if self.max_value is not None and value > self.max_value:
            raise ParamParseError(f"value {value} is above max {self.max_value}")
        return value


@dataclass(frozen=True, slots=True)
class ChoiceParam(ParamParser):
    choices: Mapping[str, Any]
    case_insensitive: bool = True

    def parse(self, raw: str) -> Any:
        value = raw.strip()
        if self.case_insensitive:
            lookup = {k.lower(): v for k, v in self.choices.items()}
            key = value.lower()
            if key not in lookup:
                raise ParamParseError(f"value '{raw}' is not a valid choice")
            return lookup[key]
        if value not in self.choices:
            raise ParamParseError(f"value '{raw}' is not a valid choice")
        return self.choices[value]


def parse_captures(
    captures: Mapping[str, str], schema: Mapping[str, ParamParser]
) -> dict[str, Any] | None:
    """Parse captured template fields with a typed schema.

    Returns None on first parsing failure.
    """
    parsed: dict[str, Any] = {}
    for key, parser in schema.items():
        raw = captures.get(key)
        if raw is None:
            return None
        try:
            parsed[key] = parser.parse(raw)
        except ParamParseError:
            return None
    return parsed
