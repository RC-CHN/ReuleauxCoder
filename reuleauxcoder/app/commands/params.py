"""Typed parameter parsers for template-matched command captures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
