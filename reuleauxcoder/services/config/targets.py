"""Resolved model labels for inspection and optional connection tests."""


def model_targets(config) -> list[dict]:
    if config is None:
        return []
    targets = []
    for name, settings in config.model_profiles.items():
        roles = []
        if name == config.active_main_model_profile:
            roles.append("main")
        if name == config.active_sub_model_profile:
            roles.append("subagent")
        if config.approval.reviewer == "auto_review" and name == config.approval.auto_review_model_profile:
            roles.append("reviewer")
        targets.append({
            "profile": name, "roles": roles, "model": settings.model,
            "provider": settings.provider, "base_url": settings.base_url,
            "request_mode": settings.request_mode or ("messages" if settings.provider == "anthropic" else "chat-completions"),
            "max_tokens": settings.max_tokens,
        })
    return targets
