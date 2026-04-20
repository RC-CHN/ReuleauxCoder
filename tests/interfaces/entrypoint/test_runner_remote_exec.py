"""Tests for runner integration with remote execution."""

from __future__ import annotations

from pathlib import Path
from urllib.request import urlopen

from reuleauxcoder.domain.config.models import Config, ContextConfig, RemoteExecConfig
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.interfaces.entrypoint.runner import (
    AppDependencies,
    AppOptions,
    AppRunner,
)


class TestRunnerRemoteExec:
    def test_local_mode_no_relay(self, tmp_path: Path) -> None:
        """When remote_exec is disabled, runner starts normally with local backend."""
        config = Config(remote_exec=RemoteExecConfig(enabled=False))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is None
        assert ctx.agent is not None
        runner.cleanup(ctx.agent)

    def test_local_mode_smoke_startup_uses_local_backends(self, tmp_path: Path) -> None:
        """Smoke test: normal local startup should not initialize remote services."""
        config = Config(
            api_key="key",
            remote_exec=RemoteExecConfig(enabled=False, host_mode=False),
        )
        runner = AppRunner(
            options=AppOptions(server_mode=False),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is None
            assert runner._relay_http_service is None
            assert ctx.agent is not None
            assert len(ctx.agent.tools) > 0
            assert all(getattr(tool.backend, "backend_id", None) == "local" for tool in ctx.agent.tools)
        finally:
            runner.cleanup(ctx.agent)

    def test_remote_enabled_host_mode_starts_relay(self, tmp_path: Path) -> None:
        config = Config(remote_exec=RemoteExecConfig(enabled=True, host_mode=True))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is not None
        assert isinstance(runner._relay_server, RelayServer)
        assert all(isinstance(tool.backend, RemoteRelayToolBackend) for tool in ctx.agent.tools)
        runner.cleanup(ctx.agent)
        assert runner._relay_server is None

    def test_remote_init_failure_does_not_crash(self, tmp_path: Path) -> None:
        def bad_relay_factory(_config: Config) -> RelayServer:
            raise RuntimeError("boom")

        config = Config(remote_exec=RemoteExecConfig(enabled=True, host_mode=True))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
                create_remote_relay_server=bad_relay_factory,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is None
        assert ctx.agent is not None
        runner.cleanup(ctx.agent)

    def test_cleanup_runs_relay_cleanup(self, tmp_path: Path) -> None:
        config = Config(remote_exec=RemoteExecConfig(enabled=True, host_mode=True))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is not None
        # no peers connected, cleanup should still complete without error
        runner.cleanup(ctx.agent)
        assert runner._relay_server is None

    def test_runner_preserves_context_config_on_agent(self, tmp_path: Path) -> None:
        config = Config(
            api_key="key",
            context=ContextConfig(
                snip_keep_recent_tools=9,
                snip_threshold_chars=3210,
                snip_min_lines=8,
                summarize_keep_recent_turns=6,
            ),
            remote_exec=RemoteExecConfig(enabled=False),
        )
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert getattr(ctx.agent, "config", None) is None or getattr(ctx.agent, "config", None) == config
        assert ctx.agent.max_context_tokens == config.max_context_tokens
        runner.cleanup(ctx.agent)

    def test_server_mode_smoke_bootstrap_endpoint(self, tmp_path: Path) -> None:
        relay_bind = "127.0.0.1:18765"
        config = Config(
            api_key="key",
            remote_exec=RemoteExecConfig(enabled=True, host_mode=True, relay_bind=relay_bind),
        )
        runner = AppRunner(
            options=AppOptions(server_mode=True),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            assert isinstance(runner._relay_http_service, RemoteRelayHTTPService)

            with urlopen(f"http://{relay_bind}/remote/bootstrap.sh", timeout=5) as resp:
                body = resp.read().decode("utf-8")
                content_type = resp.headers.get_content_type()

            assert resp.status == 200
            assert content_type in {"text/x-shellscript", "text/plain"}
            assert "#!/bin/sh" in body
            assert "RC_HOST" in body
            assert "rcoder-peer" in body
            assert "/remote/artifacts/{os}/{arch}/rcoder-peer" in body
        finally:
            runner.cleanup(ctx.agent)
