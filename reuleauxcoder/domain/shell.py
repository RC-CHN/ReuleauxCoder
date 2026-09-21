"""Execution-shell choices independent of frontend and process transport."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShellOption:
    id: str
    name: str
    path: str
    kind: str
    environment: str
    distribution: str | None = None
    launcher: str | None = None

    @property
    def summary(self) -> str:
        return f"{self.name} · {self.environment} · {self.path}"


@dataclass(frozen=True, slots=True)
class WslDistribution:
    name: str
    launcher: str


@dataclass(frozen=True, slots=True)
class ShellCatalog:
    options: tuple[ShellOption, ...]
    distributions: tuple[WslDistribution, ...] = ()
    diagnostics: tuple[str, ...] = ()
    distribution: str | None = None
