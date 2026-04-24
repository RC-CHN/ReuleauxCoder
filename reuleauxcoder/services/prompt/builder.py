"""System prompt builder."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PromptZone(str, Enum):
    """Prompt injection zone ordered by cache stability."""

    STATIC = "static"
    SEMI_STATIC = "semi_static"


@dataclass(slots=True)
class PromptBlock:
    """One prompt block with an explicit cache-stability zone."""

    key: str
    body: str
    zone: PromptZone
    order: int = 100
    title: str = ""

    def render(self) -> str:
        content = self.body.strip()
        if not content:
            return ""
        if self.title:
            return f"# {self.title}\n{content}"
        return content


class PromptAssembler:
    """Collect and render prompt blocks in stable cache-friendly order."""

    _ZONE_ORDER = {
        PromptZone.STATIC: 0,
        PromptZone.SEMI_STATIC: 1,
    }

    def __init__(self) -> None:
        self._blocks: list[PromptBlock] = []

    def add(self, block: PromptBlock) -> None:
        if block.body.strip():
            self._blocks.append(block)

    def render(self) -> str:
        rendered = [
            block.render()
            for block in sorted(
                self._blocks,
                key=lambda item: (self._ZONE_ORDER[item.zone], item.order, item.key),
            )
            if block.render()
        ]
        return "\n\n".join(rendered).rstrip() + "\n"


def _identity_block() -> PromptBlock:
    return PromptBlock(
        key="identity",
        zone=PromptZone.STATIC,
        order=10,
        body=(
            "You are ReuleauxCoder, an AI coding assistant running in the user's terminal.\n"
            "You help with software engineering: writing code, fixing bugs, refactoring, "
            "explaining code, running commands, and more."
        ),
    )


def _tools_block(tools) -> PromptBlock:
    tool_list = "\n".join(f"- **{t.name}**: {t.description}" for t in tools)
    return PromptBlock(
        key="tools",
        title="Tools",
        zone=PromptZone.STATIC,
        order=20,
        body=tool_list,
    )


def _rules_block() -> PromptBlock:
    return PromptBlock(
        key="rules",
        title="Rules",
        zone=PromptZone.STATIC,
        order=30,
        body="""1. **Read before edit.** Always read a file before modifying it.
2. **edit_file for small changes.** Use edit_file for targeted edits; write_file only for new files or complete rewrites.
3. **Verify your work.** After making changes, run relevant tests or commands to confirm correctness.
4. **Be concise.** Show code over prose. Explain only what's necessary.
5. **One step at a time.** For multi-step tasks, execute them sequentially.
6. **edit_file uniqueness.** When using edit_file, include enough surrounding context in old_string to guarantee a unique match.
7. **Respect existing style.** Match the project's coding conventions.
8. **Ask when unsure.** If the request is ambiguous, ask for clarification rather than guessing.""",
    )


def _skills_block(skills_catalog: str) -> PromptBlock:
    return PromptBlock(
        key="skills_catalog",
        zone=PromptZone.SEMI_STATIC,
        order=100,
        body=skills_catalog,
    )


def _user_instructions_block(user_system_append: str) -> PromptBlock:
    return PromptBlock(
        key="user_instructions",
        title="User Instructions",
        zone=PromptZone.SEMI_STATIC,
        order=110,
        body=user_system_append,
    )


def _mode_block(mode_name: str | None, mode_prompt_append: str) -> PromptBlock | None:
    if not mode_name and not mode_prompt_append:
        return None

    lines = []
    if mode_name:
        lines.append(f"- {mode_name}")
    if mode_prompt_append:
        lines.extend(["", "# Mode Instructions", mode_prompt_append])

    return PromptBlock(
        key="active_mode",
        title="Active Mode",
        zone=PromptZone.SEMI_STATIC,
        order=120,
        body="\n".join(lines),
    )


def _blocked_tools_block(blocked_tools: list[str] | None) -> PromptBlock | None:
    if not blocked_tools:
        return None
    return PromptBlock(
        key="blocked_tools",
        title="Mode Tool Boundaries",
        zone=PromptZone.SEMI_STATIC,
        order=130,
        body=(
            "The following tools are unavailable in this mode and must not be called: "
            + ", ".join(f"`{name}`" for name in sorted(blocked_tools))
            + "."
        ),
    )


def _mode_switch_hints_block(mode_switch_hints: list[str] | None) -> PromptBlock | None:
    if not mode_switch_hints:
        return None
    return PromptBlock(
        key="mode_switch_hints",
        title="Mode Switch Hints",
        zone=PromptZone.SEMI_STATIC,
        order=140,
        body=(
            "If a task requires unavailable capabilities, ask the user to switch mode with "
            "`/mode switch <name>` before proceeding. Suggested modes: "
            + ", ".join(f"`{name}`" for name in mode_switch_hints)
            + "."
        ),
    )


def _available_modes_block(
    available_modes: list[tuple[str, str]] | None,
    mode_name: str | None,
) -> PromptBlock | None:
    if not available_modes:
        return None

    lines = ["When mode mismatch blocks progress, request user mode switch explicitly."]
    for mode, desc in available_modes:
        if mode == mode_name:
            prefix = "- *"
            suffix = "* (active)"
        else:
            prefix = "- "
            suffix = ""
        if desc:
            lines.append(f"{prefix}`{mode}`: {desc}{suffix}")
        else:
            lines.append(f"{prefix}`{mode}`{suffix}")

    return PromptBlock(
        key="available_modes",
        title="Available Modes",
        zone=PromptZone.SEMI_STATIC,
        order=150,
        body="\n".join(lines),
    )


def system_prompt(
    tools,
    mode_name: str | None = None,
    mode_prompt_append: str = "",
    user_system_append: str = "",
    blocked_tools: list[str] | None = None,
    mode_switch_hints: list[str] | None = None,
    available_modes: list[tuple[str, str]] | None = None,
    skills_catalog: str = "",
) -> str:
    """Generate the system prompt for the agent."""
    assembler = PromptAssembler()
    assembler.add(_identity_block())
    assembler.add(_tools_block(tools))
    assembler.add(_rules_block())
    assembler.add(_skills_block(skills_catalog))
    assembler.add(_user_instructions_block(user_system_append))

    mode_block = _mode_block(mode_name, mode_prompt_append)
    if mode_block is not None:
        assembler.add(mode_block)

    blocked_tools_block = _blocked_tools_block(blocked_tools)
    if blocked_tools_block is not None:
        assembler.add(blocked_tools_block)

    mode_switch_hints_block = _mode_switch_hints_block(mode_switch_hints)
    if mode_switch_hints_block is not None:
        assembler.add(mode_switch_hints_block)

    available_modes_block = _available_modes_block(available_modes, mode_name)
    if available_modes_block is not None:
        assembler.add(available_modes_block)

    return assembler.render()
