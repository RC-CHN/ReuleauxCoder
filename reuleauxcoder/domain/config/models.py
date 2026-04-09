"""Configuration models - domain layer configuration abstractions."""

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    enabled: bool = True

    def to_dict(self) -> dict:
        """Convert to dictionary format for serialization."""
        return {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
            "enabled": self.enabled,
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
            enabled=d.get("enabled", True),
        )


@dataclass
class ModelProfileConfig:
    """Named model/runtime profile used by ``/model`` switching."""

    name: str
    model: str
    api_key: str
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 128_000

    def to_dict(self) -> dict:
        """Convert to dictionary format for serialization."""
        return {
            "model": self.model,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "max_context_tokens": self.max_context_tokens,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "ModelProfileConfig":
        """Create from dictionary format."""
        return cls(
            name=name,
            model=d.get("model", "gpt-4o"),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url"),
            max_tokens=d.get("max_tokens", 4096),
            temperature=d.get("temperature", 0.0),
            max_context_tokens=d.get("max_context_tokens", 128_000),
        )


ApprovalAction = Literal["allow", "warn", "require_approval", "deny"]


@dataclass
class ApprovalRuleConfig:
    """User-configurable approval rule."""

    tool_name: Optional[str] = None
    tool_source: Optional[str] = None
    mcp_server: Optional[str] = None
    effect_class: Optional[str] = None
    profile: Optional[str] = None
    action: ApprovalAction = "require_approval"


@dataclass
class ApprovalConfig:
    """Approval policy configuration."""

    default_mode: ApprovalAction = "require_approval"
    rules: list[ApprovalRuleConfig] = field(default_factory=list)


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
    model_profiles: dict[str, ModelProfileConfig] = field(default_factory=dict)
    active_model_profile: Optional[str] = None

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

    # Approval settings
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)

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
        valid_actions = {"allow", "warn", "require_approval", "deny"}
        if self.active_model_profile and self.active_model_profile not in self.model_profiles:
            errors.append("active_model_profile must exist in model_profiles")
        for name, profile in self.model_profiles.items():
            if not profile.api_key:
                errors.append(f"model_profiles[{name}].api_key is required")
            if profile.max_tokens < 1:
                errors.append(f"model_profiles[{name}].max_tokens must be positive")
            if profile.max_context_tokens < 1:
                errors.append(f"model_profiles[{name}].max_context_tokens must be positive")
            if profile.temperature < 0 or profile.temperature > 2:
                errors.append(f"model_profiles[{name}].temperature must be between 0 and 2")
        if self.approval.default_mode not in valid_actions:
            errors.append("approval.default_mode must be one of allow, warn, require_approval, deny")
        for i, rule in enumerate(self.approval.rules):
            if rule.action not in valid_actions:
                errors.append(
                    f"approval.rules[{i}].action must be one of allow, warn, require_approval, deny"
                )
        return errors

    def is_valid(self) -> bool:
        """Check if configuration is valid."""
        return len(self.validate()) == 0
