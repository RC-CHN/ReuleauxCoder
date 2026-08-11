"""Provider-neutral LLM runtime failures exposed to domain consumers."""


class LLMRequestCancelled(RuntimeError):
    """A scoped agent no longer owns an in-flight model request."""
