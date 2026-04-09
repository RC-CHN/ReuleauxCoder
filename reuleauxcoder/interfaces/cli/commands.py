"""CLI command handlers."""

from pathlib import Path

from reuleauxcoder.domain.config.models import ApprovalRuleConfig, MCPServerConfig
from reuleauxcoder.domain.context.manager import estimate_tokens
from reuleauxcoder.domain.hooks import HookPoint
from reuleauxcoder.domain.hooks.builtin import ToolPolicyGuardHook
from reuleauxcoder.infrastructure.persistence.workspace_config_store import WorkspaceConfigStore
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.render import show_help
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


VALID_APPROVAL_ACTIONS = {"allow", "warn", "require_approval", "deny"}


def handle_command(
    user_input: str,
    agent,
    config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    sessions_dir: Path | None = None,
):
    if user_input.lower() in ("/quit", "/exit"):
        if agent.messages:
            sid = SessionStore(sessions_dir).save(
                agent.messages,
                config.model,
                current_session_id,
                is_exit=True,
            )
            ui_bus.info(f"Session auto-saved: {sid}", kind=UIEventKind.SESSION)
        return {"action": "exit", "session_id": current_session_id}

    if user_input == "/help":
        show_help()
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/reset":
        agent.reset()
        ui_bus.warning("Conversation reset (in-memory only, does not delete saved sessions).")
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/new":
        previous_session_id = current_session_id
        if agent.messages:
            previous_session_id = SessionStore(sessions_dir).save(
                agent.messages,
                config.model,
                current_session_id,
            )
            ui_bus.info(
                f"Previous session auto-saved: {previous_session_id}",
                kind=UIEventKind.SESSION,
            )

        agent.reset()
        current_session_id = None
        ui_bus.success("Started a new conversation.", kind=UIEventKind.SESSION)
        if previous_session_id:
            ui_bus.info(
                f"Resume previous with: /session {previous_session_id}",
                kind=UIEventKind.SESSION,
            )
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/tokens":
        p = agent.llm.total_prompt_tokens
        c = agent.llm.total_completion_tokens
        ui_bus.info(
            f"Tokens used this session: {p} prompt + {c} completion = {p + c} total"
        )
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/model":
        _show_model_profiles(config, ui_bus)
        return {"action": "continue", "session_id": current_session_id}

    if user_input.startswith("/model "):
        target = user_input[7:].strip()
        if target in {"", "ls", "list", "show"}:
            _show_model_profiles(config, ui_bus)
            return {"action": "continue", "session_id": current_session_id}

        _switch_model_profile(target, agent, config, ui_bus)
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/compact":
        before = estimate_tokens(agent.messages)
        compressed = agent.context.maybe_compress(agent.messages, agent.llm)
        after = estimate_tokens(agent.messages)
        if compressed:
            ui_bus.success(
                f"Compressed: {before} → {after} tokens ({len(agent.messages)} messages)"
            )
        else:
            ui_bus.info(
                f"Nothing to compress ({before} tokens, {len(agent.messages)} messages)"
            )
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/save":
        sid = SessionStore(sessions_dir).save(
            agent.messages,
            config.model,
            current_session_id,
        )
        current_session_id = sid
        ui_bus.success(f"Session saved: {sid}", kind=UIEventKind.SESSION)
        ui_bus.info(f"Resume with: rcoder -r {sid}", kind=UIEventKind.SESSION)
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/sessions":
        sessions = SessionStore(sessions_dir).list()
        if not sessions:
            ui_bus.info("No saved sessions.", kind=UIEventKind.SESSION)
        else:
            for s in sessions:
                ui_bus.info(
                    f"  {s.id} ({s.model}, {s.saved_at}) {s.preview}",
                    kind=UIEventKind.SESSION,
                )
        return {"action": "continue", "session_id": current_session_id}

    if user_input.startswith("/session "):
        resumed_session_id, resumed_exit_time = _handle_session_resume(
            user_input[len("/session ") :].strip(),
            agent,
            config,
            ui_bus,
            sessions_dir,
        )
        if resumed_session_id is not None:
            return {
                "action": "continue",
                "session_id": resumed_session_id,
                "session_exit_time": resumed_exit_time,
            }
        return {"action": "continue", "session_id": current_session_id}

    if user_input == "/approval" or user_input == "/approval show":
        _show_approval_rules(config, ui_bus, agent)
        return {"action": "continue", "session_id": current_session_id}

    if user_input.startswith("/approval set "):
        result = _handle_approval_set(user_input[14:].strip(), agent, config, ui_bus)
        if result:
            return {"action": "continue", "session_id": current_session_id}

    if user_input == "/mcp" or user_input == "/mcp show":
        _show_mcp_servers(config, ui_bus, agent)
        return {"action": "continue", "session_id": current_session_id}

    if user_input.startswith("/mcp enable "):
        _handle_mcp_toggle(
            user_input[len("/mcp enable ") :].strip(),
            enabled=True,
            agent=agent,
            config=config,
            ui_bus=ui_bus,
        )
        return {"action": "continue", "session_id": current_session_id}

    if user_input.startswith("/mcp disable "):
        _handle_mcp_toggle(
            user_input[len("/mcp disable ") :].strip(),
            enabled=False,
            agent=agent,
            config=config,
            ui_bus=ui_bus,
        )
        return {"action": "continue", "session_id": current_session_id}

    return {"action": "chat", "session_id": current_session_id}


def _handle_session_resume(
    target: str,
    agent,
    config,
    ui_bus: UIEventBus,
    sessions_dir: Path | None,
) -> tuple[str | None, str | None]:
    if not target:
        ui_bus.error("Usage: /session <session_id|latest>", kind=UIEventKind.SESSION)
        return None, None

    store = SessionStore(sessions_dir)
    session_id = target
    if target == "latest":
        latest = store.get_latest()
        if latest is None:
            ui_bus.error("No saved sessions.", kind=UIEventKind.SESSION)
            return None, None
        session_id = latest.id

    loaded = store.load(session_id)
    if loaded is None:
        ui_bus.error(f"Session '{session_id}' not found.", kind=UIEventKind.SESSION)
        return None, None

    messages, loaded_model = loaded
    agent.state.messages = list(messages)

    if loaded_model and loaded_model != config.model:
        agent.llm.model = loaded_model
        config.model = loaded_model
        ui_bus.info(
            f"Model switched to session model: {loaded_model}",
            kind=UIEventKind.SESSION,
        )

    exit_time = store.get_exit_time(messages)
    ui_bus.success(f"Resumed session: {session_id}", kind=UIEventKind.SESSION)
    return session_id, exit_time


def _show_approval_rules(config, ui_bus: UIEventBus, agent=None) -> None:
    ui_bus.info(
        f"Approval default_mode: {config.approval.default_mode}",
        kind=UIEventKind.COMMAND,
    )
    if not config.approval.rules:
        ui_bus.info("No approval rules configured.", kind=UIEventKind.COMMAND)
    else:
        ui_bus.info("Configured rules:", kind=UIEventKind.COMMAND)
        visible_rules: list[ApprovalRuleConfig] = []
        for rule in config.approval.rules:
            if _is_disabled_mcp_rule(config, rule):
                continue
            visible_rules.append(rule)

        for idx, rule in enumerate(visible_rules, 1):
            parts = []
            if rule.tool_source:
                parts.append(f"source={rule.tool_source}")
            if rule.mcp_server:
                parts.append(f"mcp_server={rule.mcp_server}")
            if rule.tool_name:
                parts.append(f"tool={rule.tool_name}")
            if rule.effect_class:
                parts.append(f"effect={rule.effect_class}")
            if rule.profile:
                parts.append(f"profile={rule.profile}")
            scope = ", ".join(parts) if parts else "<default match>"
            ui_bus.info(
                f"  {idx}. {scope} -> {rule.action}",
                kind=UIEventKind.COMMAND,
            )

    if agent is None:
        return

    mcp_tools_by_server: dict[str, list[str]] = {}
    for tool in getattr(agent, "tools", []):
        if getattr(tool, "tool_source", None) != "mcp":
            continue
        server_name = getattr(tool, "server_name", None) or "unknown"
        mcp_tools_by_server.setdefault(server_name, []).append(tool.name)

    if not mcp_tools_by_server:
        return

    ui_bus.info("MCP effective policy view:", kind=UIEventKind.COMMAND)
    for server_name in sorted(mcp_tools_by_server):
        server_rule = _find_matching_rule(
            config.approval.rules,
            ApprovalRuleConfig(tool_source="mcp", mcp_server=server_name, action=config.approval.default_mode),
        )
        server_action = server_rule.action if server_rule else _resolve_mcp_server_action(config, server_name)
        server_source = (
            "configured at server level"
            if server_rule is not None
            else "inherited from generic mcp/default"
        )
        ui_bus.info(
            f"  MCP server {server_name} -> {server_action}",
            kind=UIEventKind.COMMAND,
        )
        ui_bus.info(
            f"    [dim]{server_source}[/dim]",
            kind=UIEventKind.COMMAND,
        )

        for tool_name in sorted(mcp_tools_by_server[server_name]):
            tool_rule = _find_matching_rule(
                config.approval.rules,
                ApprovalRuleConfig(
                    tool_source="mcp",
                    mcp_server=server_name,
                    tool_name=tool_name,
                    action=config.approval.default_mode,
                ),
            )
            tool_action = tool_rule.action if tool_rule else server_action
            tool_source = (
                "configured at tool level"
                if tool_rule is not None
                else f"inherited from server {server_name}"
            )
            ui_bus.info(
                f"    - {tool_name} -> {tool_action}",
                kind=UIEventKind.COMMAND,
            )
            ui_bus.info(
                f"      [dim]{tool_source}[/dim]",
                kind=UIEventKind.COMMAND,
            )


def _handle_approval_set(spec: str, agent, config, ui_bus: UIEventBus) -> bool:
    parts = spec.split()
    if len(parts) < 2:
        ui_bus.error(
            "Usage: /approval set <target> <allow|warn|require_approval|deny>",
            kind=UIEventKind.COMMAND,
        )
        return True

    target = parts[0]
    action = parts[1]
    if action not in VALID_APPROVAL_ACTIONS:
        ui_bus.error(
            "approval action must be one of allow, warn, require_approval, deny",
            kind=UIEventKind.COMMAND,
        )
        return True

    rule = _parse_approval_target(target, action)
    if rule is None:
        ui_bus.error(
            "target must be one of tool:<name>, mcp, mcp:<server>, or mcp:<server>:<tool>",
            kind=UIEventKind.COMMAND,
        )
        return True

    config.approval.rules = [r for r in config.approval.rules if not _same_rule_target(r, rule)]
    config.approval.rules.append(rule)
    path = WorkspaceConfigStore().save_approval_config(config.approval)
    _refresh_approval_runtime(agent, config)
    ui_bus.success(
        f"Updated approval rule and saved to {path}",
        kind=UIEventKind.COMMAND,
    )
    return True


def _parse_approval_target(target: str, action: str) -> ApprovalRuleConfig | None:
    if target == "mcp":
        return ApprovalRuleConfig(tool_source="mcp", action=action)
    if target.startswith("tool:"):
        return ApprovalRuleConfig(tool_name=target[5:], action=action)
    if target.startswith("mcp:"):
        rest = target[4:]
        if not rest:
            return None
        if ":" in rest:
            server, tool_name = rest.split(":", 1)
            if not server or not tool_name:
                return None
            return ApprovalRuleConfig(
                tool_source="mcp",
                mcp_server=server,
                tool_name=tool_name,
                action=action,
            )
        return ApprovalRuleConfig(tool_source="mcp", mcp_server=rest, action=action)
    return None


def _same_rule_target(left: ApprovalRuleConfig, right: ApprovalRuleConfig) -> bool:
    return (
        left.tool_name == right.tool_name
        and left.tool_source == right.tool_source
        and left.mcp_server == right.mcp_server
        and left.effect_class == right.effect_class
        and left.profile == right.profile
    )


def _resolve_mcp_server_action(config, server_name: str) -> str:
    tool_rule = _find_matching_rule(
        config.approval.rules,
        ApprovalRuleConfig(tool_source="mcp", mcp_server=server_name, action=config.approval.default_mode),
    )
    if tool_rule is not None:
        return tool_rule.action
    generic_rule = _find_matching_rule(
        config.approval.rules,
        ApprovalRuleConfig(tool_source="mcp", action=config.approval.default_mode),
    )
    if generic_rule is not None:
        return generic_rule.action
    return config.approval.default_mode


def _find_matching_rule(rules: list[ApprovalRuleConfig], target: ApprovalRuleConfig) -> ApprovalRuleConfig | None:
    for rule in rules:
        if _same_rule_target(rule, target):
            return rule
    return None


def _refresh_approval_runtime(agent, config) -> None:
    hooks = agent.hook_registry._hooks.get(HookPoint.BEFORE_TOOL_EXECUTE, [])
    for hook in hooks:
        if isinstance(hook, ToolPolicyGuardHook):
            hook.update_approval_config(config.approval)


def _show_mcp_servers(config, ui_bus: UIEventBus, agent=None) -> None:
    servers = list(getattr(config, "mcp_servers", []) or [])
    if not servers:
        ui_bus.info("No MCP servers configured.", kind=UIEventKind.MCP)
        return

    manager = getattr(agent, "mcp_manager", None) if agent is not None else None
    runtime_connected = set(getattr(manager, "connected_servers", set()) or set())

    ui_bus.info("MCP servers:", kind=UIEventKind.MCP)
    for server in servers:
        enabled_mark = "enabled" if getattr(server, "enabled", True) else "disabled"
        runtime_mark = "connected" if server.name in runtime_connected else "disconnected"
        ui_bus.info(
            f"  - {server.name}: {enabled_mark}, runtime={runtime_mark}",
            kind=UIEventKind.MCP,
        )


def _handle_mcp_toggle(
    server_name: str,
    *,
    enabled: bool,
    agent,
    config,
    ui_bus: UIEventBus,
) -> None:
    if not server_name:
        action = "enable" if enabled else "disable"
        ui_bus.error(f"Usage: /mcp {action} <server>", kind=UIEventKind.MCP)
        return

    server = _find_mcp_server(config.mcp_servers, server_name)
    if server is None:
        ui_bus.error(f"MCP server '{server_name}' not found in config.", kind=UIEventKind.MCP)
        return

    if bool(server.enabled) == enabled:
        state = "enabled" if enabled else "disabled"
        ui_bus.info(f"MCP server '{server_name}' is already {state}.", kind=UIEventKind.MCP)
        return

    server.enabled = enabled
    path = WorkspaceConfigStore().save_mcp_server_config(server)

    manager = getattr(agent, "mcp_manager", None)
    if manager is None:
        if enabled:
            ui_bus.warning(
                "MCP manager is not initialized; change is saved and will apply on next startup.",
                kind=UIEventKind.MCP,
            )
        else:
            ui_bus.info(
                "MCP manager is not initialized; disable state is saved.",
                kind=UIEventKind.MCP,
            )
        ui_bus.success(f"Saved MCP server '{server_name}' to {path}", kind=UIEventKind.MCP)
        return

    ok = manager.connect_server(server) if enabled else manager.disconnect_server(server_name)
    _refresh_mcp_runtime_tools(agent)

    state = "enabled" if enabled else "disabled"
    if ok:
        ui_bus.success(
            f"MCP server '{server_name}' {state} and saved to {path}",
            kind=UIEventKind.MCP,
        )
    else:
        ui_bus.warning(
            f"MCP server '{server_name}' state saved to {path}, but runtime {state} failed.",
            kind=UIEventKind.MCP,
        )


def _find_mcp_server(servers: list[MCPServerConfig], server_name: str) -> MCPServerConfig | None:
    for server in servers:
        if server.name == server_name:
            return server
    return None


def _refresh_mcp_runtime_tools(agent) -> None:
    manager = getattr(agent, "mcp_manager", None)
    manager_tools = list(getattr(manager, "tools", []) or [])
    non_mcp_tools = [t for t in getattr(agent, "tools", []) if getattr(t, "tool_source", None) != "mcp"]
    agent.tools = non_mcp_tools + manager_tools


def _is_disabled_mcp_rule(config, rule: ApprovalRuleConfig) -> bool:
    if rule.tool_source != "mcp" or not rule.mcp_server:
        return False

    server = _find_mcp_server(getattr(config, "mcp_servers", []), rule.mcp_server)
    if server is None:
        return False
    return not bool(getattr(server, "enabled", True))


def _show_model_profiles(config, ui_bus: UIEventBus) -> None:
    profiles = getattr(config, "model_profiles", {}) or {}
    active = getattr(config, "active_model_profile", None)

    if not profiles:
        ui_bus.warning(
            "No model profiles configured. Add models.profiles in config.yaml.",
            kind=UIEventKind.COMMAND,
        )
        ui_bus.info(
            f"Current runtime model={config.model}, base_url={config.base_url}, max_tokens={config.max_tokens}, temperature={config.temperature}, max_context_tokens={config.max_context_tokens}",
            kind=UIEventKind.COMMAND,
        )
        return

    ui_bus.info("Model profiles:", kind=UIEventKind.COMMAND)
    for name in sorted(profiles):
        p = profiles[name]
        marker = "*" if active == name else " "
        api_hint = "***" if getattr(p, "api_key", "") else "(empty)"
        ui_bus.info(
            f"  {marker} {name}: model={p.model}, base_url={p.base_url}, max_tokens={p.max_tokens}, temperature={p.temperature}, max_context_tokens={p.max_context_tokens}, api_key={api_hint}",
            kind=UIEventKind.COMMAND,
        )


def _switch_model_profile(profile_name: str, agent, config, ui_bus: UIEventBus) -> None:
    profiles = getattr(config, "model_profiles", {}) or {}
    profile = profiles.get(profile_name)
    if profile is None:
        ui_bus.error(
            f"Unknown model profile '{profile_name}'. Use /model to list available profiles.",
            kind=UIEventKind.COMMAND,
        )
        return

    agent.llm.reconfigure(
        model=profile.model,
        api_key=profile.api_key,
        base_url=profile.base_url,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
    )
    config.model = profile.model
    config.api_key = profile.api_key
    config.base_url = profile.base_url
    config.temperature = profile.temperature
    config.max_tokens = profile.max_tokens
    config.max_context_tokens = profile.max_context_tokens
    config.active_model_profile = profile_name

    agent.context.reconfigure(profile.max_context_tokens)

    path = WorkspaceConfigStore().save_active_model_profile(profile_name)

    ui_bus.success(
        f"Switched model profile to '{profile_name}' ({profile.model}) and saved to {path}",
        kind=UIEventKind.COMMAND,
    )
