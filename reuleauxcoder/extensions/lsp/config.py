"""LSP configuration — parse the [lsp] section from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reuleauxcoder.domain.config.models import Config


@dataclass
class LspServerOverride:
    """Per-language server configuration override."""

    language: str  # config key name (e.g. "python", "cpp")
    cmd: str | None = None
    args: list[str] | None = None
    workspace_root: str | None = None
    init_opts: dict[str, Any] | None = None


@dataclass
class LspConfig:
    """Parsed [lsp] section from config.yaml."""

    enabled: bool = True
    poll_timeout_ms: int = 5000
    max_diagnostics: int = 20
    include_warnings: bool = False
    server_overrides: dict[str, LspServerOverride] = field(default_factory=dict)

    def get_override(self, language_key: str) -> LspServerOverride | None:
        """Get the per-language override for a config key (e.g. 'python')."""
        return self.server_overrides.get(language_key)

    @classmethod
    def from_config(cls, config: Config) -> LspConfig:
        """Parse LspConfig from the project Config object.

        Falls back to defaults if the [lsp] section is missing.
        """
        lsp_raw = getattr(config, "lsp", None)
        if lsp_raw is None:
            return cls()

        enabled = bool(lsp_raw.get("enabled", True))
        poll_timeout_ms = int(lsp_raw.get("poll_timeout_ms", 5000))
        max_diagnostics = int(lsp_raw.get("max_diagnostics", 20))
        include_warnings = bool(lsp_raw.get("include_warnings", False))

        overrides: dict[str, LspServerOverride] = {}
        servers_raw = lsp_raw.get("servers", {}) or {}
        for lang_key, srv in servers_raw.items():
            overrides[lang_key] = LspServerOverride(
                language=lang_key,
                cmd=srv.get("cmd"),
                args=srv.get("args"),
                workspace_root=srv.get("workspace_root"),
                init_opts=srv.get("init_opts"),
            )

        return cls(
            enabled=enabled,
            poll_timeout_ms=poll_timeout_ms,
            max_diagnostics=max_diagnostics,
            include_warnings=include_warnings,
            server_overrides=overrides,
        )
