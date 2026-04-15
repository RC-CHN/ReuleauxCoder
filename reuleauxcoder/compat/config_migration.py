"""Configuration compatibility helpers."""

from __future__ import annotations

from copy import deepcopy


def migrate_legacy_config(data: dict) -> tuple[dict, bool]:
    """Migrate legacy ``app`` model config into new ``models`` layout.

    Returns ``(migrated_data, changed)``.
    """
    migrated = deepcopy(data)
    changed = False

    app = migrated.get("app", {}) if isinstance(migrated.get("app"), dict) else {}
    models = migrated.get("models")
    has_profiles = (
        isinstance(models, dict)
        and isinstance(models.get("profiles"), dict)
        and bool(models.get("profiles"))
    )

    if not has_profiles:
        migrated["models"] = {
            "active": "default",
            "profiles": {
                "default": {
                    "model": app.get("model", "gpt-4o"),
                    "api_key": app.get("api_key", ""),
                    "base_url": app.get("base_url"),
                    "max_tokens": app.get("max_tokens", 4096),
                    "temperature": app.get("temperature", 0.0),
                    "max_context_tokens": app.get("max_context_tokens", 128_000),
                }
            },
        }
        changed = True

    # Normalize active profile: if missing/invalid, pick first profile key.
    if isinstance(migrated.get("models"), dict):
        profiles = migrated["models"].get("profiles", {})
        active = migrated["models"].get("active")
        if isinstance(profiles, dict) and profiles:
            if not isinstance(active, str) or active not in profiles:
                migrated["models"]["active"] = next(iter(profiles.keys()))
                changed = True

    # Cleanup legacy fields in app section after migration.
    if isinstance(migrated.get("app"), dict):
        app_data = migrated["app"]
        legacy_keys = {
            "model",
            "api_key",
            "base_url",
            "max_tokens",
            "temperature",
            "max_context_tokens",
        }
        before = set(app_data.keys())
        for k in legacy_keys:
            app_data.pop(k, None)
        if set(app_data.keys()) != before:
            changed = True
        if not app_data:
            migrated.pop("app", None)
            changed = True

    return migrated, changed


def migrate_bash_to_shell(data: dict) -> tuple[dict, bool]:
    """Migrate legacy ``bash`` builtin tool references to ``shell``.

    Affects:
    - ``modes.profiles.*.tools`` arrays
    - ``approval.rules.*.tool_name`` values

    Returns ``(migrated_data, changed)``.
    """
    migrated = deepcopy(data)
    changed = False

    # Migrate mode tool lists
    modes = migrated.get("modes")
    if isinstance(modes, dict):
        profiles = modes.get("profiles")
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if isinstance(profile, dict):
                    tools = profile.get("tools")
                    if isinstance(tools, list):
                        new_tools = ["shell" if t == "bash" else t for t in tools]
                        if new_tools != tools:
                            profile["tools"] = new_tools
                            changed = True

    # Migrate approval rules
    approval = migrated.get("approval")
    if isinstance(approval, dict):
        rules = approval.get("rules")
        if isinstance(rules, list):
            for rule in rules:
                if isinstance(rule, dict) and rule.get("tool_name") == "bash":
                    rule["tool_name"] = "shell"
                    changed = True

    return migrated, changed
