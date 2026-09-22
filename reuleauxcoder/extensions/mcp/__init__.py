"""MCP extension - Model Context Protocol integration."""

__all__ = [
    "MCPClient",
    "MCPManager",
    "build_mcp_servers_view",
    "find_mcp_server",
    "refresh_mcp_runtime_tools",
    "toggle_mcp_server",
]


def __getattr__(name):
    from importlib import import_module

    modules = {"MCPClient": "client", "MCPManager": "manager"}
    if name in __all__:
        return getattr(import_module(f"{__name__}.{modules.get(name, 'runtime')}"), name)
    raise AttributeError(name)
