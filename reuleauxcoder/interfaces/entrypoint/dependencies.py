"""Dependency providers and context models for the shared app runner."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Callable

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.mcp.manager import MCPManager
from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.registry import build_tools
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.interactions import UIInteractor
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.llm.client import LLM


def _default_load_config(path: Path | None) -> Config:
    return ConfigLoader.from_path(path)


def _default_create_llm(config: Config) -> LLM:
    return LLM(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        preserve_reasoning_content=getattr(config, "preserve_reasoning_content", True),
        backfill_reasoning_content_for_tool_calls=getattr(
            config, "backfill_reasoning_content_for_tool_calls", False
        ),
        debug_trace=getattr(config, "llm_debug_trace", False),
    )


def _default_create_tool_backend(config: Config, ui_bus: UIEventBus) -> ToolBackend:
    return LocalToolBackend()


def _default_load_tools(tool_backend: ToolBackend) -> list[Any]:
    return build_tools(tool_backend)


def _default_create_agent(llm: LLM, tools: list[Any], config: Config) -> Agent:
    return Agent(
        llm=llm,
        tools=tools,
        max_context_tokens=config.max_context_tokens,
        available_modes=getattr(config, "modes", {}) or {},
        active_mode=getattr(config, "active_mode", None),
    )


def _default_create_session_store(sessions_dir: Path | None) -> SessionStore:
    return SessionStore(sessions_dir)


def _default_create_mcp_manager(ui_bus: UIEventBus) -> MCPManager:
    return MCPManager(ui_bus=ui_bus)


def _default_create_remote_relay_server(config: Config) -> RelayServer | None:
    if not getattr(config, "remote_exec", None) or not config.remote_exec.enabled:
        return None
    if not config.remote_exec.host_mode:
        return None
    return RelayServer(
        heartbeat_interval_sec=config.remote_exec.heartbeat_interval_sec,
        heartbeat_timeout_sec=config.remote_exec.heartbeat_timeout_sec,
        default_tool_timeout_sec=config.remote_exec.default_tool_timeout_sec,
        shell_timeout_sec=config.remote_exec.shell_timeout_sec,
    )


def _default_create_remote_artifact_provider(
    ui_bus: UIEventBus,
) -> Callable[[str, str, str], tuple[bytes, str] | None]:
    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "reuleauxcoder-agent"
    artifact_root = repo_root / "artifacts" / "remote"
    build_dir = Path(tempfile.mkdtemp(prefix="rcoder-peer-build-"))
    build_lock = threading.Lock()
    cache: dict[tuple[str, str], Path] = {}

    def _find_prebuilt_binary(
        os_name: str, arch: str, artifact_name: str
    ) -> Path | None:
        output_name = artifact_name + (".exe" if os_name == "windows" else "")
        candidates = [
            artifact_root / os_name / arch / output_name,
            artifact_root / f"{os_name}-{arch}" / output_name,
            repo_root / output_name,
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def provide(
        os_name: str, arch: str, artifact_name: str
    ) -> tuple[bytes, str] | None:
        if artifact_name != "rcoder-peer":
            return None
        if os_name not in {"linux", "darwin", "windows"}:
            return None
        if arch not in {"amd64", "arm64"}:
            return None

        prebuilt_binary = _find_prebuilt_binary(os_name, arch, artifact_name)
        if prebuilt_binary is not None:
            ui_bus.info(
                f"Using prebuilt peer artifact for {os_name}/{arch}: {prebuilt_binary.name}",
                kind=UIEventKind.REMOTE,
                os=os_name,
                arch=arch,
                source="prebuilt",
            )
            return prebuilt_binary.read_bytes(), "application/octet-stream"

        if shutil.which("go") is None:
            raise RuntimeError(
                "peer artifact unavailable: no prebuilt binary found and local 'go' toolchain is not installed"
            )

        target_key = (os_name, arch)
        with build_lock:
            binary_path = cache.get(target_key)
            if binary_path is None or not binary_path.exists():
                output_name = artifact_name + (".exe" if os_name == "windows" else "")
                binary_path = build_dir / f"{os_name}-{arch}-{output_name}"
                env = dict(os.environ)
                env["GOOS"] = os_name
                env["GOARCH"] = arch
                subprocess.run(
                    [
                        "go",
                        "build",
                        "-o",
                        str(binary_path),
                        "./cmd/reuleauxcoder-agent",
                    ],
                    cwd=agent_dir,
                    env=env,
                    check=True,
                    timeout=180,
                )
                cache[target_key] = binary_path
                ui_bus.info(
                    f"Built peer artifact for {os_name}/{arch}: {binary_path.name}",
                    kind=UIEventKind.REMOTE,
                    os=os_name,
                    arch=arch,
                    source="built",
                )
        return binary_path.read_bytes(), "application/octet-stream"

    setattr(provide, "_build_dir", build_dir)
    setattr(provide, "_artifact_root", artifact_root)
    return provide


def _default_create_remote_http_service(
    config: Config,
    relay_server: RelayServer,
    ui_bus: UIEventBus,
) -> RemoteRelayHTTPService | None:
    if not getattr(config, "remote_exec", None) or not config.remote_exec.enabled:
        return None
    if not config.remote_exec.host_mode:
        return None
    return RemoteRelayHTTPService(
        relay_server=relay_server,
        bind=config.remote_exec.relay_bind,
        ui_bus=ui_bus,
        artifact_provider=_default_create_remote_artifact_provider(ui_bus),
        bootstrap_access_secret=config.remote_exec.bootstrap_access_secret,
        bootstrap_token_ttl_sec=config.remote_exec.bootstrap_token_ttl_sec,
    )


@dataclass
class AppDependencies:
    """Lightweight dependency providers for AppRunner."""

    load_config: Callable[[Path | None], Config] = _default_load_config
    create_ui_bus: Callable[[], UIEventBus] = UIEventBus
    create_llm: Callable[[Config], LLM] = _default_create_llm
    create_tool_backend: Callable[[Config, UIEventBus], ToolBackend] = (
        _default_create_tool_backend
    )
    load_tools: Callable[[ToolBackend], list[Any]] = _default_load_tools
    create_agent: Callable[[LLM, list[Any], Config], Agent] = _default_create_agent
    create_session_store: Callable[[Path | None], SessionStore] = (
        _default_create_session_store
    )
    create_mcp_manager: Callable[[UIEventBus], MCPManager] = _default_create_mcp_manager
    create_remote_relay_server: Callable[[Config], RelayServer | None] = (
        _default_create_remote_relay_server
    )
    create_remote_http_service: Callable[
        [Config, RelayServer, UIEventBus], RemoteRelayHTTPService | None
    ] = _default_create_remote_http_service


@dataclass
class AppContext:
    """Context object containing all initialized application components."""

    config: Config
    llm: LLM
    agent: Agent
    ui_bus: UIEventBus
    ui_interactor: UIInteractor | None = None
    mcp_manager: MCPManager | None = None
    skills_service: SkillsService | None = None
    current_session_id: str | None = None
    session_exit_time: str | None = None
    sessions_dir: Path | None = None


@dataclass
class AppOptions:
    """Options for application initialization."""

    config_path: Path | None = None
    model: str | None = None
    resume_session_id: str | None = None
    auto_resume_latest: bool = True
    server_mode: bool = False
