"""LLM domain protocols - abstract interfaces for LLM providers."""

from collections.abc import Callable
from typing import Protocol, Optional, Any
from reuleauxcoder.domain.llm.models import LLMResponse


class LLMProtocol(Protocol):
    """Protocol defining the interface for LLM implementations."""

    model: str

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """Send messages and receive a response.

        Args:
            messages: List of message dictionaries
            tools: Optional list of tool schemas
            on_token: Optional callback invoked for each streamed text chunk

        Returns:
            LLMResponse with content, tool calls, and token counts
        """
        ...


class ToolSchemaProtocol(Protocol):
    """Protocol for tool schema generation."""

    name: str
    description: str
    parameters: dict

    def schema(self) -> dict:
        """Generate OpenAI function-calling schema."""
        ...
