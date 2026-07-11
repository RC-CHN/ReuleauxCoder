"""LLM domain messages - message structures for conversation."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Message:
    """A single message in the conversation."""

    role: str  # "system", "user", "assistant", "tool"
    content: Optional[str] = None
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary format for API calls."""
        msg: dict = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.tool_calls is not None:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        return msg

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        """Create from dictionary format."""
        return cls(
            role=d.get("role", "user"),
            content=d.get("content"),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
        )


def estimate_tokens(messages: list[dict]) -> int:
    """Estimate token count for messages. ~3.5 chars/token for mixed en/zh content."""
    total = 0
    for m in messages:
        if m.get("content"):
            total += len(m["content"]) // 3
        if m.get("tool_calls"):
            total += len(str(m["tool_calls"])) // 3
    return total
