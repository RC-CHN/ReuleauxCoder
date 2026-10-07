"""Versioned launch contract for an externally supervised, unattended task."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from reuleauxcoder.domain.goal import validate_budget


@dataclass(frozen=True)
class JobCheck:
    argv: list[str]
    timeout_seconds: int = 300

    def __post_init__(self):
        if (
            not isinstance(self.argv, list)
            or not self.argv
            or not all(isinstance(x, str) and x for x in self.argv)
        ):
            raise ValueError("A check requires a nonempty argv array")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ValueError("Check timeout must be a positive integer")


@dataclass(frozen=True)
class JobSpec:
    workspace: str
    prompt: str
    objective: str = ""
    config: str | None = None
    model: str | None = None
    token_budget: int | None = None
    max_seconds: float | None = None
    artifacts: list[str] = field(default_factory=list)
    checks: list[JobCheck] = field(default_factory=list)
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported job specification version")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("A job requires task instructions")
        objective = self.objective or self.prompt
        if not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 4000:
            raise ValueError(
                "Provide an objective of 1–4000 characters for long prompts"
            )
        object.__setattr__(self, "objective", objective.strip())
        workspace = Path(self.workspace).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")
        object.__setattr__(self, "workspace", str(workspace))
        if self.config is not None:
            config = Path(self.config).expanduser().resolve(strict=True)
            if not config.is_file():
                raise ValueError("Config must be a file")
            object.__setattr__(self, "config", str(config))
        validate_budget(self.token_budget)
        if self.max_seconds is not None and (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("max_seconds must be positive and finite")
        if not isinstance(self.artifacts, list) or not all(
            isinstance(name, str) for name in self.artifacts
        ):
            raise ValueError("Artifacts must be an array of relative paths")
        for name in self.artifacts:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not name:
                raise ValueError("Artifact paths must be relative to the workspace")
        if not isinstance(self.checks, list) or not all(
            isinstance(check, JobCheck) for check in self.checks
        ):
            raise ValueError("Checks must be JobCheck records")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["checks"] = [JobCheck(**check) for check in value.get("checks", [])]
        return cls(**value)


EXIT_CODES = {
    "completed": 0,
    "failed": 1,
    "blocked": 3,
    "paused": 4,
    "budget_limited": 5,
    "usage_limited": 6,
    "time_limited": 7,
    "verification_failed": 8,
    "cancelled": 130,
}
