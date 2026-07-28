import pytest

from reuleauxcoder.app.commands.panels import (
    CommandPanelRegistry,
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
    PanelRefreshPolicy,
)


class ExampleView:
    pass


def _build(model: object, title: str) -> PanelDefinition:
    assert isinstance(model, ExampleView)
    return PanelDefinition(
        view_type="example",
        title=title,
        items=(PanelItem("one", "first", "/example one"),),
    )


def test_panel_spec_rejects_the_wrong_view_model_type() -> None:
    spec = CommandPanelSpec("example", ExampleView, _build)

    assert spec.build_for(object(), "Example") is None
    assert spec.build_for(ExampleView(), "Example") == _build(
        ExampleView(), "Example"
    )


def test_panel_definition_resolves_typed_child_by_row_label() -> None:
    child = PanelDefinition(
        view_type="child",
        title="Child",
        items=(PanelItem("run", "execute", "/example run"),),
        return_to_parent_on_submit=True,
    )
    root = PanelDefinition(
        view_type="example",
        title="Example",
        items=(PanelItem("details", "open details", ""),),
        children=(("details", child),),
        filterable=True,
    )

    assert root.child_for("details") is child
    assert root.child_for("missing") is None


def test_panel_registry_preserves_order_and_rejects_duplicates() -> None:
    first = CommandPanelSpec(
        "first", ExampleView, _build, refresh=PanelRefreshPolicy.ABSORB
    )
    second = CommandPanelSpec("second", ExampleView, _build)

    registry = CommandPanelRegistry((first, second))

    assert registry.view_types() == ("first", "second")
    assert registry.get("first") is first
    assert registry.get("missing") is None

    with pytest.raises(ValueError, match="Duplicate command panel view: first"):
        CommandPanelRegistry((first, first))
