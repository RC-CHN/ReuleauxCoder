"""Provider-neutral LLM runtime failures exposed to domain consumers."""

from reuleauxcoder.domain.llm.models import LLMResponse


INCOMPLETE_FINISH_REASONS = frozenset({
    "length", "max_tokens", "max_output_tokens", "model_context_window_exceeded",
    "incomplete", "content_filter", "stream_ended",
})


class IncompleteModelResponse(RuntimeError):
    """A provider explicitly ended a response before it could be used."""

    def __init__(self, response: LLMResponse):
        self.response = response
        reason = response.finish_reason
        if reason == "content_filter":
            detail = "The provider filtered the response"
        elif reason == "stream_ended":
            detail = "The provider stream ended before its completion event"
        elif reason == "incomplete":
            detail = "The provider returned an incomplete response"
        else:
            detail = "The model reached its output or context limit"
        super().__init__(f"{detail} ({reason}). No tool calls from this response were executed.")


class LLMRequestCancelled(RuntimeError):
    """A scoped agent no longer owns an in-flight model request."""


class LLMRequestDeferred(RuntimeError):
    """Request preparation yielded to context maintenance before dispatch."""


class ContextWindowExceeded(RuntimeError):
    """The complete request cannot fit the model's context window."""
