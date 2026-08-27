"""Provider-neutral contracts consumed by the agent runtime."""

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from reuleauxcoder.domain.cancellation import CancellationSignal
from reuleauxcoder.domain.llm.models import LLMResponse


@runtime_checkable
class LLMProtocol(Protocol):
    """Exact model-client surface used by ``Agent`` and ``AgentLoop``."""

    model: str
    provider_family: str
    request_mode: str
    max_tokens: int
    debug_trace: bool
    last_dispatched_request: dict[str, Any] | None
    last_debug_trace_path: str | None

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_reasoning_token: Callable[[str], None] | None = None,
        hook_registry: Any | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        cancellation_event: CancellationSignal | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        """Dispatch one fully prepared provider request."""
        ...


class ToolSchemaProtocol(Protocol):
    """Protocol for tool schema generation."""

    name: str
    description: str
    parameters: dict

    def schema(self) -> dict:
        """Generate OpenAI function-calling schema."""
        ...
