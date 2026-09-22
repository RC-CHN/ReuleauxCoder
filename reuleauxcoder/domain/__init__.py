"""Domain layer - core abstractions and state models."""

__all__ = [
    "Agent",
    "ContextManager",
    "Config",
    "Session",
    "HookRegistry",
    "HookPoint",
    "HookKind",
]


def __getattr__(name):
    from importlib import import_module

    modules = {
        "Agent": "agent",
        "Config": "config",
        "ContextManager": "context",
        "Session": "session",
        "HookKind": "hooks",
        "HookPoint": "hooks",
        "HookRegistry": "hooks",
    }
    if name in modules:
        return getattr(import_module(f"{__name__}.{modules[name]}"), name)
    raise AttributeError(name)
