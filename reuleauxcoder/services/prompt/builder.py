"""System prompt builder."""

import os
import platform

from reuleauxcoder.infrastructure.platform import get_platform_info


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
    cwd = os.getcwd()
    tool_list = "\n".join(f"- **{t.name}**: {t.description}" for t in tools)
    uname = platform.uname()
    shell = get_platform_info().get_preferred_shell()

    base = f"""\
You are ReuleauxCoder, an AI coding assistant running in the user's terminal.
You help with software engineering: writing code, fixing bugs, refactoring, explaining code, running commands, and more.

# Environment
- Working directory: {cwd}
- OS: {uname.system} {uname.release} ({uname.machine})
- Python: {platform.python_version()}
- Shell: {shell.value}

# Tools
{tool_list}

# Rules
1. **Read before edit.** Always read a file before modifying it.
2. **edit_file for small changes.** Use edit_file for targeted edits; write_file only for new files or complete rewrites.
3. **Verify your work.** After making changes, run relevant tests or commands to confirm correctness.
4. **Be concise.** Show code over prose. Explain only what's necessary.
5. **One step at a time.** For multi-step tasks, execute them sequentially.
6. **edit_file uniqueness.** When using edit_file, include enough surrounding context in old_string to guarantee a unique match.
7. **Respect existing style.** Match the project's coding conventions.
8. **Ask when unsure.** If the request is ambiguous, ask for clarification rather than guessing.
"""

    if skills_catalog:
        base += "\n" + skills_catalog.rstrip() + "\n"

    if user_system_append:
        base += "\n# User Instructions\n" + user_system_append.rstrip() + "\n"

    # Keep mode-specific directives appended at the end to minimize cache churn.
    mode_lines: list[str] = []
    if mode_name or mode_prompt_append:
        mode_lines.extend(["", "# Active Mode"])
        if mode_name:
            mode_lines.append(f"- {mode_name}")
        if mode_prompt_append:
            mode_lines.extend(["", "# Mode Instructions", mode_prompt_append])

    if blocked_tools:
        mode_lines.extend(["", "# Mode Tool Boundaries"])
        mode_lines.append(
            "The following tools are unavailable in this mode and must not be called: "
            + ", ".join(f"`{name}`" for name in sorted(blocked_tools))
            + "."
        )

    if mode_switch_hints:
        mode_lines.extend(["", "# Mode Switch Hints"])
        mode_lines.append(
            "If a task requires unavailable capabilities, ask the user to switch mode with "
            "`/mode switch <name>` before proceeding. Suggested modes: "
            + ", ".join(f"`{name}`" for name in mode_switch_hints)
            + "."
        )

    if available_modes:
        mode_lines.extend(["", "# Available Modes"])
        mode_lines.append("When mode mismatch blocks progress, request user mode switch explicitly.")
        for mode, desc in available_modes:
            if mode == mode_name:
                prefix = "- *"
                suffix = "* (active)"
            else:
                prefix = "- "
                suffix = ""
            if desc:
                mode_lines.append(f"{prefix}`{mode}`: {desc}{suffix}")
            else:
                mode_lines.append(f"{prefix}`{mode}`{suffix}")

    if not mode_lines:
        return base

    return base + "\n".join(mode_lines) + "\n"
