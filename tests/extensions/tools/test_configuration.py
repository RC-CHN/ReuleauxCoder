"""Configuration editing uses ordinary file/shell tools, not a second tool family."""

from reuleauxcoder.extensions.tools.registry import iter_tool_classes


def test_configuration_tools_are_not_in_the_agent_catalog():
    names = {tool.name for tool in iter_tool_classes()}
    assert {"read_file", "edit_file", "write_file", "shell"} <= names
    assert not {"config_read", "config_prepare", "config_validate", "config_apply"} & names
