"""Shell picker data shared by CLI, TUI and RPC."""

from dataclasses import asdict, dataclass

from reuleauxcoder.domain.shell import ShellCatalog, ShellOption


@dataclass(frozen=True, slots=True)
class ShellsView:
    catalog: ShellCatalog
    current: ShellOption | None
    automatic: bool
    view_type: str = "shells"

    def to_payload(self) -> dict:
        return {
            "catalog": asdict(self.catalog),
            "current": asdict(self.current) if self.current else None,
            "automatic": self.automatic,
        }
