from __future__ import annotations

import pytest

from reuleauxcoder.domain.extensions import (
    ExtensionDefinition,
    ExtensionManager,
    ExtensionManifest,
    ExtensionScope,
)


def _definition(
    extension_id: str,
    *,
    requires=frozenset(),
    before=frozenset(),
    after=frozenset(),
    scopes=frozenset({ExtensionScope.SESSION}),
    factory=lambda context: object(),
    api_version=1,
    config_namespace=None,
):
    return ExtensionDefinition(
        manifest=ExtensionManifest(
            extension_id=extension_id,
            version="1.0.0",
            api_version=api_version,
            requires=requires,
            before=before,
            after=after,
            scopes=scopes,
            config_namespace=config_namespace,
        ),
        factory=factory,
    )


def test_dependency_and_ordering_constraints_are_deterministic() -> None:
    manager = ExtensionManager()
    manager.register(_definition("observer", after=frozenset({"processor"})))
    manager.register(_definition("auth", before=frozenset({"processor"})))
    manager.register(_definition("processor", requires=frozenset({"auth"})))

    assert manager.resolve_order() == ("auth", "processor", "observer")


def test_missing_dependency_and_cycle_fail_before_instantiation() -> None:
    missing = ExtensionManager()
    missing.register(_definition("a", requires=frozenset({"missing"})))
    with pytest.raises(ValueError, match="missing dependencies"):
        missing.resolve_order()

    cycle = ExtensionManager()
    cycle.register(_definition("a", after=frozenset({"b"})))
    cycle.register(_definition("b", after=frozenset({"a"})))
    with pytest.raises(ValueError, match="ordering cycle"):
        cycle.resolve_order()


def test_duplicate_and_incompatible_api_are_rejected() -> None:
    manager = ExtensionManager()
    manager.register(_definition("a"))
    with pytest.raises(ValueError, match="Duplicate"):
        manager.register(_definition("a"))
    with pytest.raises(ValueError, match="requires API"):
        manager.register(_definition("future", api_version=2))


def test_scope_filter_and_config_namespace() -> None:
    seen = []
    manager = ExtensionManager()
    manager.register(
        _definition(
            "session-only",
            config_namespace="demo",
            factory=lambda context: seen.append(dict(context.config)) or object(),
        )
    )

    subagent = manager.open_scope(
        ExtensionScope.SUBAGENT, "sub-1", config={"demo": {"enabled": True}}
    )
    session = manager.open_scope(
        ExtensionScope.SESSION, "session-1", config={"demo": {"enabled": True}}
    )

    assert subagent.extension_ids == ()
    assert session.extension_ids == ("session-only",)
    assert seen == [{"enabled": True}]


def test_partial_construction_disposes_in_reverse_order() -> None:
    disposed = []

    class Instance:
        def __init__(self, name):
            self.name = name

        def dispose(self):
            disposed.append(self.name)

    manager = ExtensionManager()
    manager.register(_definition("a", factory=lambda context: Instance("a")))
    manager.register(
        _definition(
            "b",
            after=frozenset({"a"}),
            factory=lambda context: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    )

    with pytest.raises(RuntimeError, match="boom"):
        manager.open_scope(ExtensionScope.SESSION, "s")

    assert disposed == ["a"]


def test_dispose_all_is_reverse_order_exactly_once_and_observable() -> None:
    disposed = []

    class Instance:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def dispose(self):
            disposed.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} failed")

    manager = ExtensionManager()
    manager.register(_definition("a", factory=lambda context: Instance("a")))
    manager.register(
        _definition(
            "b",
            after=frozenset({"a"}),
            factory=lambda context: Instance("b", fail=True),
        )
    )
    manager.open_scope(ExtensionScope.SESSION, "s")

    diagnostics = manager.dispose_all()
    manager.dispose_all()

    assert disposed == ["b", "a"]
    assert len(diagnostics) == 1
    assert diagnostics[0].extension_id == "b"
    assert diagnostics[0].phase == "dispose"
