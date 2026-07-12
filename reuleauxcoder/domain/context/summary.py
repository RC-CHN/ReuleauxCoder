"""Summary generation utilities."""

import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.services.llm.client import LLM


SUMMARY_SYSTEM_PROMPT = """Create a compact handoff checkpoint for a coding agent.
Return plain text with exactly these sections when applicable:
Goal, Constraints, Decisions, Completed, Files, Errors, Pending, Next.
Preserve concrete paths, commands, error messages, user boundaries, and current
repository state. Remove verbose tool output, repeated discussion, drafts, and
reasoning. Never invent completion or authorization."""


def generate_summary(messages: list[dict], llm: Optional["LLM"] = None) -> str:
    """Generate a summary of messages."""
    if llm:
        try:
            flat = flatten_messages(messages)
            resp = llm.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            SUMMARY_SYSTEM_PROMPT
                        ),
                    },
                    {"role": "user", "content": flat[:15000]},
                ],
            )
            return resp.content
        except Exception:
            pass

    return extract_key_info(messages)


def flatten_messages(messages: list[dict], truncate: int = 1200) -> str:
    """Flatten messages to a string."""
    parts = []
    for m in messages:
        role = m.get("role", "?")
        text = m.get("content", "") or ""
        if text:
            if len(text) > truncate:
                half = truncate // 2
                text = text[:half] + "\n…\n" + text[-half:]
            parts.append(f"[{role}] {text}")
    flattened = "\n".join(parts)
    if len(flattened) <= 40_000:
        return flattened
    # Preserve both the earliest constraints and the latest working state.
    return flattened[:12_000] + "\n… [middle omitted] …\n" + flattened[-28_000:]


def extract_key_info(messages: list[dict]) -> str:
    """Extract key information from messages without LLM."""
    files_seen = set()
    errors = []
    decisions = []

    for m in messages:
        text = m.get("content", "") or ""

        # Extract file paths
        for match in re.finditer(r"[\w./\-]+\.\w{1,5}", text):
            files_seen.add(match.group())

        # Extract error lines
        for line in text.splitlines():
            line_lower = line.lower()
            if "error" in line_lower:
                errors.append(line.strip()[:150])
            if "decision" in line_lower or "decided" in line_lower:
                decisions.append(line.strip()[:150])

    parts = []
    if files_seen:
        parts.append(f"Files touched: {', '.join(sorted(files_seen)[:20])}")
    if errors:
        parts.append(f"Errors seen: {'; '.join(errors[:5])}")
    if decisions:
        parts.append(f"Decisions: {'; '.join(decisions[:3])}")

    return "\n".join(parts) or "(no extractable context)"
