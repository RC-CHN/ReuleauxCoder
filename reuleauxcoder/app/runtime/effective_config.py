"""Build a typed, secret-safe view of effective runtime configuration."""

from __future__ import annotations

from reuleauxcoder.app.commands.view_models import (
    EffectiveConfigRowViewModel,
    EffectiveConfigViewModel,
)
from reuleauxcoder.extensions.lsp.config import LspConfig


def build_effective_config_view(config, agent=None) -> EffectiveConfigViewModel:
    sources = getattr(config, "effective_sources", {}) or {}

    def source(path: str, *, runtime: bool = False) -> str:
        return "session" if runtime else sources.get(path, "default")

    configured_main = getattr(config, "active_main_model_profile", None)
    configured_sub = getattr(config, "active_sub_model_profile", None)
    runtime_main = getattr(agent, "active_main_model_profile", None) or configured_main
    runtime_sub = getattr(agent, "active_sub_model_profile", None) or configured_sub
    runtime_mode = getattr(agent, "active_mode", None) or getattr(
        config, "active_mode", None
    )
    lsp = LspConfig.from_config(config)

    rows = (
        EffectiveConfigRowViewModel(
            "models.active_main",
            str(runtime_main or "-"),
            source("models.active_main", runtime=runtime_main != configured_main),
        ),
        EffectiveConfigRowViewModel(
            "models.active_sub",
            str(runtime_sub or "-"),
            source("models.active_sub", runtime=runtime_sub != configured_sub),
        ),
        EffectiveConfigRowViewModel(
            "models.runtime_model",
            str(getattr(getattr(agent, "llm", None), "model", config.model)),
            "session",
        ),
        EffectiveConfigRowViewModel(
            "modes.active",
            str(runtime_mode or "-"),
            source(
                "modes.active",
                runtime=runtime_mode != getattr(config, "active_mode", None),
            ),
        ),
        EffectiveConfigRowViewModel(
            "lsp.enabled", str(lsp.enabled).lower(), source("lsp.enabled")
        ),
        EffectiveConfigRowViewModel(
            "lsp.include_warnings",
            str(lsp.include_warnings).lower(),
            source("lsp.include_warnings"),
        ),
        EffectiveConfigRowViewModel(
            "lsp.typescript_mode",
            lsp.typescript_mode,
            source("lsp.typescript_mode"),
        ),
        EffectiveConfigRowViewModel(
            "session.auto_save",
            str(config.session_auto_save).lower(),
            source("session.auto_save"),
        ),
        EffectiveConfigRowViewModel(
            "ui.verbosity", config.ui.verbosity, source("ui.verbosity")
        ),
        EffectiveConfigRowViewModel(
            "ui.tool_output", config.ui.tool_output, source("ui.tool_output")
        ),
        EffectiveConfigRowViewModel(
            "ui.reasoning_display",
            config.ui.reasoning_display,
            source("ui.reasoning_display"),
        ),
        EffectiveConfigRowViewModel(
            "tool_output.max_chars",
            str(config.tool_output_max_chars),
            source("tool_output.max_chars"),
        ),
        EffectiveConfigRowViewModel(
            "tool_output.max_lines",
            str(config.tool_output_max_lines),
            source("tool_output.max_lines"),
        ),
        EffectiveConfigRowViewModel(
            "remote_exec.enabled",
            str(config.remote_exec.enabled).lower(),
            source("remote_exec.enabled"),
        ),
    )
    diagnostics = tuple(
        f"{item.severity}: {item.path}: {item.message}"
        for item in getattr(config, "diagnostics", [])
    )
    extension_manager = getattr(agent, "extension_manager", None)
    extension_graph = (
        extension_manager.describe_graph() if extension_manager is not None else ()
    )
    extension_scopes = (
        extension_manager.describe_scopes() if extension_manager is not None else ()
    )
    lsp_manager = getattr(agent, "lsp_manager", None)
    lsp_scopes = lsp_manager.describe_scopes() if lsp_manager is not None else ()

    relay_server = getattr(agent, "relay_server", None)
    peers = (
        relay_server.registry.list_online()
        if relay_server is not None and getattr(relay_server, "registry", None)
        else []
    )
    peer_capabilities = tuple(
        f"{peer.peer_id}: v{peer.meta.get('protocol_version', 1)} "
        + ",".join(sorted(peer.capabilities))
        for peer in sorted(peers, key=lambda item: item.peer_id)
    )

    subagent_manager = getattr(agent, "_subagent_manager", None)
    jobs = subagent_manager.list_jobs() if subagent_manager is not None else []
    active_jobs = tuple(
        f"{job.id}:{job.status}:g{job.generation}:{job.parent_agent_id or '-'}"
        for job in jobs
        if job.status in {"queued", "running", "cancelling"}
    )
    return EffectiveConfigViewModel(
        rows=rows,
        diagnostics=diagnostics,
        extension_graph=extension_graph,
        extension_scopes=extension_scopes,
        lsp_scopes=lsp_scopes,
        peer_capabilities=peer_capabilities,
        active_jobs=active_jobs,
    )
