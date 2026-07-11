"""Explicit extension manifests, ordering and runtime scopes."""

from reuleauxcoder.domain.extensions.manager import (
    ExtensionContext,
    ExtensionDefinition,
    ExtensionDiagnostic,
    ExtensionManager,
    ExtensionScopeContainer,
)
from reuleauxcoder.domain.extensions.hook_adapter import HookExtensionAdapter
from reuleauxcoder.domain.extensions.ports import (
    AuthorizationPolicy,
    ContextContributor,
    LifecycleParticipant,
    OutcomeProcessor,
    RuntimeObserver,
    ToolExtensionRuntime,
)
from reuleauxcoder.domain.extensions.manifest import (
    EXTENSION_API_VERSION,
    ExtensionPhase,
    ExtensionManifest,
    ExtensionScope,
    SubagentPolicy,
)

__all__ = [
    "EXTENSION_API_VERSION",
    "ExtensionContext",
    "ExtensionDefinition",
    "ExtensionDiagnostic",
    "ExtensionManager",
    "ExtensionManifest",
    "ExtensionPhase",
    "ExtensionScope",
    "SubagentPolicy",
    "ExtensionScopeContainer",
    "HookExtensionAdapter",
    "AuthorizationPolicy",
    "ContextContributor",
    "LifecycleParticipant",
    "OutcomeProcessor",
    "RuntimeObserver",
    "ToolExtensionRuntime",
]
