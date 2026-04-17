"""Base class and backend dispatch helpers for tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


BackendHandler = Callable[..., str]


def backend_handler(backend_id: str) -> Callable[[BackendHandler], BackendHandler]:
    """Mark a tool method as the implementation for a specific backend."""

    def decorator(func: BackendHandler) -> BackendHandler:
        setattr(func, "_tool_backend_id", backend_id)
        return func

    return decorator


class Tool(ABC):
    """Minimal tool interface with backend-aware dispatch helpers."""

    name: str
    description: str
    parameters: dict
    _backend_handlers: dict[str, str] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        handlers: dict[str, str] = {}
        for base in reversed(cls.__mro__[1:]):
            handlers.update(getattr(base, "_backend_handlers", {}))
        for attr_name, value in cls.__dict__.items():
            backend_id = getattr(value, "_tool_backend_id", None)
            if backend_id:
                handlers[backend_id] = attr_name
        cls._backend_handlers = handlers

    def __init__(self, backend: Any = None):
        self.backend = backend

    def preflight_validate(self, **kwargs) -> str | None:
        """Optional lightweight validation before approval/execution.

        Return an error string to short-circuit execution, or None if valid.
        """
        return None

    @property
    def backend_id(self) -> str:
        return getattr(self.backend, "backend_id", "local")

    def run_backend(self, *args, **kwargs) -> str:
        """Dispatch to a tool-local implementation for the active backend."""
        handler_name = self._backend_handlers.get(self.backend_id)
        if handler_name is None:
            handler_name = self._backend_handlers.get("local")
        if handler_name is None:
            raise RuntimeError(
                f"Tool '{self.name}' has no handler for backend '{self.backend_id}' and no local fallback"
            )
        handler = getattr(self, handler_name)
        return handler(*args, **kwargs)

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """Run the tool and return a text result."""
        ...

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
