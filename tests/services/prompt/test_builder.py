from reuleauxcoder.services.prompt.builder import system_prompt


class _Tool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


def test_system_prompt_includes_user_system_append() -> None:
    prompt = system_prompt(
        [_Tool("read_file", "Read file")],
        user_system_append="Always answer in Chinese.",
    )

    assert "# User Instructions" in prompt
    assert "Always answer in Chinese." in prompt


def test_system_prompt_includes_user_append_before_mode_instructions() -> None:
    prompt = system_prompt(
        [_Tool("read_file", "Read file")],
        mode_name="coder",
        mode_prompt_append="Focus on concrete code changes.",
        user_system_append="Always answer in Chinese.",
    )

    assert prompt.index("# User Instructions") < prompt.index("# Active Mode")
    assert "Focus on concrete code changes." in prompt


def test_system_prompt_contains_only_static_and_semi_static_blocks() -> None:
    prompt = system_prompt(
        [_Tool("read_file", "Read file")],
        mode_name="coder",
        mode_prompt_append="Focus on concrete code changes.",
        user_system_append="Always answer in Chinese.",
        skills_catalog="# Skills\n- skill-a",
    )

    assert prompt.index("# Tools") < prompt.index("# User Instructions")
    assert prompt.index("# User Instructions") < prompt.index("# Active Mode")
    assert "# Environment" not in prompt
    assert "- Working directory: " not in prompt

