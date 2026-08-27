"""LLM domain models - response and tool call structures."""

from dataclasses import dataclass, field
import json


EMPTY_ASSISTANT_CONTENT_PLACEHOLDER = "[No assistant content returned.]"
PROVIDER_DATA_KEY = "provider_data"


@dataclass
class ToolCall:
    """Represents a tool call from the LLM."""

    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """Response from the LLM including content and tool calls."""

    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_input_tokens: int | None = None
    provider_data: dict | None = None
    tokens: list[str] = field(
        default_factory=list
    )  # Streamed tokens for event emission

    @property
    def message(self) -> dict:
        """Convert to OpenAI message format for appending to history."""
        msg: dict = {
            "role": "assistant",
            "content": self.content or EMPTY_ASSISTANT_CONTENT_PLACEHOLDER,
        }
        if self.reasoning_content is not None:
            msg["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            msg["content"] = self.content or None
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.provider_data is not None:
            msg[PROVIDER_DATA_KEY] = self.provider_data
        return msg
