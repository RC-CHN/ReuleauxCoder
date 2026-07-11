"""Deterministic extension graph, scope creation and disposal."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from reuleauxcoder.domain.extensions.manifest import (
    EXTENSION_API_VERSION,
    ExtensionManifest,
    ExtensionPhase,
    ExtensionScope,
    SubagentPolicy,
)


class ExtensionInstance(Protocol):
    """Optional lifecycle supported by instantiated extensions."""

    def dispose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ExtensionContext:
    scope: ExtensionScope
    scope_id: str
    config: Mapping[str, Any]
    services: Mapping[str, Any]


ExtensionFactory = Callable[[ExtensionContext], Any]


@dataclass(frozen=True, slots=True)
class ExtensionDefinition:
    manifest: ExtensionManifest
    factory: ExtensionFactory


@dataclass(frozen=True, slots=True)
class ExtensionDiagnostic:
    extension_id: str
    phase: str
    message: str


@dataclass
class ExtensionScopeContainer:
    """Instances owned by one runner/session/agent/subagent scope."""

    scope: ExtensionScope
    scope_id: str
    _instances: dict[str, Any] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    diagnostics: list[ExtensionDiagnostic] = field(default_factory=list)
    _disposed: bool = False

    def get(self, extension_id: str) -> Any | None:
        return self._instances.get(extension_id)

    @property
    def extension_ids(self) -> tuple[str, ...]:
        return tuple(self._order)

    def dispose(self) -> None:
        """Dispose instances in reverse construction order, exactly once."""
        if self._disposed:
            return
        self._disposed = True
        for extension_id in reversed(self._order):
            instance = self._instances[extension_id]
            dispose = getattr(instance, "dispose", None) or getattr(
                instance, "close", None
            )
            if not callable(dispose):
                continue
            try:
                dispose()
            except Exception as error:  # disposal is best-effort but observable
                self.diagnostics.append(
                    ExtensionDiagnostic(
                        extension_id=extension_id,
                        phase="dispose",
                        message=str(error),
                    )
                )


class ExtensionManager:
    """Own explicit definitions and build deterministic runtime scopes."""

    def __init__(self, *, api_version: int = EXTENSION_API_VERSION):
        self.api_version = api_version
        self._definitions: dict[str, ExtensionDefinition] = {}
        self._containers: list[ExtensionScopeContainer] = []

    def register(self, definition: ExtensionDefinition) -> None:
        manifest = definition.manifest
        if manifest.extension_id in self._definitions:
            raise ValueError(f"Duplicate extension id: {manifest.extension_id}")
        if manifest.api_version != self.api_version:
            raise ValueError(
                f"Extension '{manifest.extension_id}' requires API "
                f"{manifest.api_version}, runtime provides {self.api_version}"
            )
        self._definitions[manifest.extension_id] = definition

    def resolve_order(self) -> tuple[str, ...]:
        """Topologically resolve dependencies plus before/after constraints."""
        ids = set(self._definitions)
        edges: dict[str, set[str]] = {extension_id: set() for extension_id in ids}
        indegree = {extension_id: 0 for extension_id in ids}

        for extension_id, definition in self._definitions.items():
            manifest = definition.manifest
            missing = manifest.requires - ids
            if missing:
                names = ", ".join(sorted(missing))
                raise ValueError(
                    f"Extension '{extension_id}' has missing dependencies: {names}"
                )
            for predecessor in manifest.requires | manifest.after:
                if predecessor in ids:
                    edges[predecessor].add(extension_id)
            for successor in manifest.before:
                if successor in ids:
                    edges[extension_id].add(successor)

        for successors in edges.values():
            for successor in successors:
                indegree[successor] += 1

        def order_key(name: str) -> tuple[ExtensionPhase, str]:
            return (self._definitions[name].manifest.phase, name)

        ready = sorted(
            (name for name, degree in indegree.items() if degree == 0),
            key=order_key,
        )
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for successor in sorted(edges[current]):
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
                    ready.sort(key=order_key)

        if len(ordered) != len(ids):
            cycle = sorted(name for name, degree in indegree.items() if degree > 0)
            raise ValueError(f"Extension ordering cycle: {', '.join(cycle)}")
        return tuple(ordered)

    def open_scope(
        self,
        scope: ExtensionScope,
        scope_id: str,
        *,
        config: Mapping[str, Any] | None = None,
        services: Mapping[str, Any] | None = None,
        remote_target: bool = False,
    ) -> ExtensionScopeContainer:
        """Instantiate extensions enabled for one explicit scope."""
        context = ExtensionContext(
            scope=scope,
            scope_id=scope_id,
            config=config or {},
            services=services or {},
        )
        container = ExtensionScopeContainer(scope=scope, scope_id=scope_id)
        try:
            for extension_id in self.resolve_order():
                definition = self._definitions[extension_id]
                if scope not in definition.manifest.scopes:
                    continue
                if (
                    scope is ExtensionScope.SUBAGENT
                    and definition.manifest.subagent_policy is SubagentPolicy.OMIT
                ):
                    continue
                if remote_target and not definition.manifest.remote_compatible:
                    raise ValueError(
                        f"Extension '{extension_id}' is not remote compatible"
                    )
                namespace = definition.manifest.config_namespace
                extension_config = (
                    context.config.get(namespace, {}) if namespace else context.config
                )
                extension_context = ExtensionContext(
                    scope=scope,
                    scope_id=scope_id,
                    config=extension_config,
                    services=context.services,
                )
                instance = definition.factory(extension_context)
                container._instances[extension_id] = instance
                container._order.append(extension_id)
        except Exception:
            container.dispose()
            raise
        self._containers.append(container)
        return container

    def dispose_all(self) -> tuple[ExtensionDiagnostic, ...]:
        """Dispose scopes in reverse creation order."""
        diagnostics: list[ExtensionDiagnostic] = []
        for container in reversed(self._containers):
            container.dispose()
            diagnostics.extend(container.diagnostics)
        self._containers.clear()
        return tuple(diagnostics)

    def describe_graph(self) -> tuple[str, ...]:
        """Return a stable, secret-free extension ordering snapshot."""
        return tuple(
            f"{extension_id} [{self._definitions[extension_id].manifest.phase.value}]"
            for extension_id in self.resolve_order()
        )

    def describe_scopes(self) -> tuple[str, ...]:
        """Return active scope ownership without exposing extension instances."""
        return tuple(
            f"{container.scope.value}:{container.scope_id} -> "
            + ", ".join(container.extension_ids)
            for container in self._containers
        )
