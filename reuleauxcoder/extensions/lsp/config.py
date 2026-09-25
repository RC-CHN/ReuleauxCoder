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
    edit_wait_timeout_ms: int = 1000
    max_diagnostics: int = 20
    max_injection_chars: int = 12_000
    max_message_chars: int = 1_000
    include_warnings: bool = True
    typescript_mode: str = "auto"
    server_overrides: dict[str, LspServerOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        if set(self.server_overrides) - {language.name.lower() for language in LanguageId}:
            raise ValueError("lsp.servers contains an unsupported language")
        if any(server.cmd is not None and not server.cmd.strip() for server in self.server_overrides.values()):
            raise ValueError("lsp server cmd must not be empty")
        if self.edit_wait_timeout_ms < 0:
            raise ValueError("lsp.edit_wait_timeout_ms cannot be negative")
        for name in ("poll_timeout_ms", "max_diagnostics", "max_message_chars"):
            if getattr(self, name) < 1:
                raise ValueError(f"lsp.{name} must be positive")
        if self.max_injection_chars < 512:
            raise ValueError("lsp.max_injection_chars must be at least 512")

    def get_override(self, language_key: str) -> LspServerOverride | None:
        """Get the per-language override for a config key (e.g. 'python')."""
        return self.server_overrides.get(language_key)

    @classmethod
    def from_config(cls, config: Config) -> LspConfig:
        """Parse LspConfig from the project Config object.

        Falls back to defaults if the [lsp] section is missing.
        """
        defaults = cls()
        lsp_raw = getattr(config, "lsp", None)
        if lsp_raw is None:
            return defaults

        enabled = bool(lsp_raw.get("enabled", defaults.enabled))
        poll_timeout_ms = int(lsp_raw.get("poll_timeout_ms", defaults.poll_timeout_ms))
        edit_wait_timeout_ms = int(
            lsp_raw.get("edit_wait_timeout_ms", defaults.edit_wait_timeout_ms)
        )
        max_diagnostics = int(lsp_raw.get("max_diagnostics", defaults.max_diagnostics))
        max_injection_chars = int(
            lsp_raw.get("max_injection_chars", defaults.max_injection_chars)
        )
        max_message_chars = int(
            lsp_raw.get("max_message_chars", defaults.max_message_chars)
        )
        include_warnings = bool(
            lsp_raw.get("include_warnings", defaults.include_warnings)
        )
        typescript_mode = str(
            lsp_raw.get("typescript_mode", defaults.typescript_mode)
        ).lower()
        if typescript_mode not in {"auto", "native", "legacy"}:
            raise ValueError("lsp.typescript_mode must be one of: auto, native, legacy")

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
            edit_wait_timeout_ms=edit_wait_timeout_ms,
            max_diagnostics=max_diagnostics,
            max_injection_chars=max_injection_chars,
            max_message_chars=max_message_chars,
            include_warnings=include_warnings,
            typescript_mode=typescript_mode,
            server_overrides=overrides,
        )
