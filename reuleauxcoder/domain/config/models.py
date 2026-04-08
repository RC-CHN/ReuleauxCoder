"""Configuration models - domain layer configuration abstractions."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary format for serialization."""
        return {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "MCPServerConfig":
        """Create from dictionary format."""
        return cls(
            name=name,
            command=d.get("command", ""),
            args=d.get("args", []),
            env=d.get("env", {}),
            cwd=d.get("cwd"),
        )


@dataclass
class Config:
    """Main configuration model for ReuleauxCoder."""

    model: str = "gpt-4o"
    api_key: str = ""
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 128_000
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)

    # Tool output settings
    tool_output_max_chars: int = 12_000
    tool_output_max_lines: int = 120
    tool_output_store_full: bool = True
    tool_output_store_dir: Optional[str] = None

    # Session settings
    session_auto_save: bool = True
    session_dir: Optional[str] = None

    # CLI settings
    history_file: Optional[str] = None

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []
        if not self.api_key:
            errors.append("api_key is required")
        if self.max_tokens < 1:
            errors.append("max_tokens must be positive")
        if self.temperature < 0 or self.temperature > 2:
            errors.append("temperature must be between 0 and 2")
        if self.tool_output_max_chars < 1:
            errors.append("tool_output_max_chars must be positive")
        if self.tool_output_max_lines < 1:
            errors.append("tool_output_max_lines must be positive")
        return errors

    def is_valid(self) -> bool:
        """Check if configuration is valid."""
        return len(self.validate()) == 0
