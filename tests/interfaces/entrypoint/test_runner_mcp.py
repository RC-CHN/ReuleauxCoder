from types import SimpleNamespace

from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner


def test_init_mcp_binds_agent_catalog_and_runtime_issue_observers() -> None:
    observed: dict[str, object] = {}

    class _Manager:
        performance_monitor = None

        def bind_runtime_observers(self, **kwargs) -> None:
            observed.update(kwargs)

        def connect_servers_async(self, servers) -> None:
            observed["servers"] = list(servers)

    manager = _Manager()
    dependencies = AppDependencies(create_mcp_manager=lambda _bus: manager)
    runner = AppRunner(dependencies=dependencies)
    agent = SimpleNamespace(
        replace_mcp_tools=lambda tools: tools,
        record_runtime_issue=lambda phase, error_type, ref: (
            phase,
            error_type,
            ref,
        ),
    )
    ui_bus = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    enabled = SimpleNamespace(enabled=True)
    disabled = SimpleNamespace(enabled=False)

    assert runner._init_mcp([enabled, disabled], agent, ui_bus) is manager
    assert observed["catalog_listener"] is agent.replace_mcp_tools
    assert observed["runtime_issue_sink"] is agent.record_runtime_issue
    assert observed["servers"] == [enabled]
