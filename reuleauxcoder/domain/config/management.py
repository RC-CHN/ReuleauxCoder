"""Configuration diagnostics shared by loading, CLI and editor interfaces."""

from dataclasses import asdict, dataclass


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
