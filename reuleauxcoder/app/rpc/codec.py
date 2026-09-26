"""Small tagged JSON codec over a fixed catalog of public data contracts.

Tags never name importable modules. Arbitrary dictionaries are escaped so user
data cannot be interpreted as records. Callables and runtime objects fail loudly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass
from enum import Enum

from reuleauxcoder.app import interaction_contracts, ui_events
from reuleauxcoder.app.commands import (
    approval_views,
    capabilities,
    panels,
    process_views,
    shell_views,
    requests,
    specs,
    view_models,
)
from reuleauxcoder.app.rpc import models
from reuleauxcoder.domain import (
    approval,
    attachments,
    goal,
    history_query,
    images,
    plan,
    shell,
    version_control,
)
from reuleauxcoder.domain.agent import tool_outcome
from reuleauxcoder.domain.runtime import events, notification_kind
from reuleauxcoder.extensions.mcp import models as mcp_models
from reuleauxcoder.extensions.skills import models as skill_models


_MODULES = (
    interaction_contracts,
    ui_events,
    approval_views,
    capabilities,
    process_views,
    shell_views,
    requests,
    view_models,
    models,
    history_query,
    images,
    attachments,
    version_control,
    events,
    notification_kind,
    plan,
    shell,
    goal,
    mcp_models,
    skill_models,
    tool_outcome,
)
_TYPES = {
    value.__name__: value
    for module in _MODULES
    for value in vars(module).values()
    if isinstance(value, type)
    and value.__module__ == module.__name__
    and (is_dataclass(value) or issubclass(value, Enum))
}
_TYPES.update(
    {
        cls.__name__: cls
        for cls in (
            specs.ActionCatalog,
            specs.ActionParameter,
            specs.ActionDescription,
            specs.TriggerSpec,
            specs.TriggerKind,
            specs.DuringTurnPolicy,
            panels.PanelDefinition,
            panels.PanelItem,
            panels.PanelItemDetails,
            panels.PanelPresentation,
            panels.PanelRefreshPolicy,
            approval.ApprovalSection,
            approval.ApprovalSectionKind,
            approval.ApprovalQueueStatus,
        )
    }
)


def encode(value):
    if isinstance(value, Enum):
        if _TYPES.get(type(value).__name__) is not type(value):
            raise TypeError(f"Unregistered enum: {type(value).__name__}")
        return {"$type": type(value).__name__, "value": value.value}
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, requests.ActionRequest):
        command = (
            asdict(value.command) if is_dataclass(value.command) else value.command
        )
        return {
            "$type": "ActionRequest",
            "fields": {"action_id": value.action_id, "command": encode(command)},
        }
    if is_dataclass(value) and _TYPES.get(type(value).__name__) is type(value):
        return {
            "$type": type(value).__name__,
            "fields": {
                field.name: encode(getattr(value, field.name))
                for field in fields(value)
                if field.init
            },
        }
    if isinstance(value, (tuple, frozenset)):
        return {
            "$type": type(value).__name__,
            "items": [encode(item) for item in value],
        }
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Wire dictionaries require string keys")
        data = {key: encode(item) for key, item in value.items()}
        return {"$type": "dict", "items": data} if "$type" in data else data
    raise TypeError(f"Not a public wire value: {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get("$type")
    if tag is None:
        return {key: decode(item) for key, item in value.items()}
    if tag == "dict":
        return {key: decode(item) for key, item in value["items"].items()}
    if tag in ("tuple", "frozenset"):
        return (tuple if tag == "tuple" else frozenset)(
            decode(item) for item in value["items"]
        )
    cls = _TYPES[tag]
    if issubclass(cls, Enum):
        return cls(value["value"])
    return cls(**{key: decode(item) for key, item in value["fields"].items()})
