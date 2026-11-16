"""Provider-neutral LLM runtime failures exposed to domain consumers."""


class LLMRequestCancelled(RuntimeError):
    """A scoped agent no longer owns an in-flight model request."""


class LLMRequestDeferred(RuntimeError):
    """Request preparation yielded to context maintenance before dispatch."""


class ContextWindowExceeded(RuntimeError):
    """The complete request cannot fit the model's context window."""
