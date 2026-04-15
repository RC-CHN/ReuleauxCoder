"""Policies for the shell tool."""

from __future__ import annotations

import re
from dataclasses import dataclass

from reuleauxcoder.domain.hooks.types import GuardDecision
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.tools.policies.base import ToolPolicy


@dataclass(frozen=True, slots=True)
class ShellDangerousCommandPolicy(ToolPolicy):
    """Block obviously dangerous shell commands before execution."""

    patterns: tuple[tuple[str, str], ...] = (
        (r"\brm\s+(-\w*)?-rf\s", "force recursive delete"),
        (r"\brm\s+(-\w*)?-r\w*\s+(/\s|~(?:/|\s|$)|\$HOME(?:/|\s|$))", "recursive delete on home/root"),
        (r"\bmkfs\b", "format filesystem"),
        (r"\bdd\s+.*of=/dev/", "raw disk write"),
        (r">\s*/dev/sd[a-z]", "overwrite block device"),
        (r"\bchmod\s+(-R\s+)?777\s+/", "chmod 777 on root"),
        (r":\(\)\s*\{.*:\|:\s*\}", "fork bomb"),
        (r"\bcurl\b.*\|\s*(sudo\s+)?bash", "pipe curl to bash"),
        (r"\bwget\b.*\|\s*(sudo\s+)?bash", "pipe wget to bash"),
    )

    def evaluate(self, tool_call: ToolCall) -> GuardDecision | None:
        if tool_call.name != "shell":
            return None

        command = tool_call.arguments.get("command")
        if not isinstance(command, str):
            return GuardDecision.deny("shell tool requires a string 'command' argument")

        for pattern, reason in self.patterns:
            if re.search(pattern, command):
                return GuardDecision.deny(
                    f"Blocked by shell policy: {reason}. Command: {command}"
                )
        return GuardDecision.allow()
