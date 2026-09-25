"""Configuration-management contracts shared by tools and human interfaces."""

from dataclasses import asdict, dataclass
from typing import Literal, Protocol

ConfigActor = Literal["user", "model"]
ConfigScope = Literal["user", "workspace", "explicit"]


class ConfigurationPort(Protocol):
    def execute(
        self, operation: str, parameters: dict, *, actor: ConfigActor
    ) -> dict: ...
    def review(self, change_id: str, *, actor: ConfigActor) -> dict: ...


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    code: str
    path: str
    message: str
    severity: str = "error"
    source: str | None = None
    line: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigOperationError(ValueError):
    """A bounded, secret-free error suitable for every management adapter."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
