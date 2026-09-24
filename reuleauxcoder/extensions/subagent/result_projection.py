"""Delegated prompt and result projection from immutable child evidence."""

from dataclasses import dataclass
import re
from typing import TypedDict
from reuleauxcoder.domain.history import HistoryEvent
from reuleauxcoder.extensions.subagent.models import SubagentResult


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    tool_facts: tuple[HistoryEvent, ...]
    mode: str | None
    elapsed_seconds: float
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    model_calls: int


def project_result(
    snapshot: ResultSnapshot,
    *,
    status: str,
    summary: str,
    partial: bool = False,
    usage_uncertain: bool = False,
    resume_ready: bool = False,
    transcript_ref: str | None = None,
) -> SubagentResult:
    reported = _parse_delegated_final_response(summary)
    files = sorted(
        {match.group(0) for match in re.finditer(r"(?:[\w.-]+/)+[\w.-]+", summary)}
    )
    tool_facts = snapshot.tool_facts
    failures = [event for event in tool_facts if not event.payload.get("success")]
    runtime_evidence = [
        (
            f"tool {event.payload.get('tool_name') or 'unknown'}: "
            f"{event.payload.get('status') or 'unknown'}"
            + (
                f" (exit {event.payload['exit_code']})"
                if event.payload.get("exit_code") is not None
                else ""
            )
        )
        for event in tool_facts[-20:]
    ]
    evidence = list(dict.fromkeys([*reported["evidence"], *runtime_evidence]))
    unresolved = list(reported["unresolved"])
    missing_sections = reported["missing"]
    confidence = reported["confidence"]
    if missing_sections and not partial:
        unresolved.append(
            "Delegated final response omitted required sections: "
            + ", ".join(missing_sections)
        )
        confidence = "low"
    if snapshot.mode == "verify" and failures:
        status = "failed"
        summary = (
            "Verification observed one or more failed tool outcomes.\n" + summary
        ).strip()
    result = SubagentResult(
        status=status,
        summary=reported["conclusion"] or summary,
        evidence=evidence,
        files=files[:100],
        changes=reported["artifacts"],
        unresolved=unresolved,
        confidence=confidence,
        duration_seconds=snapshot.elapsed_seconds,
        partial=partial,
        tool_uses=snapshot.tool_calls,
        prompt_tokens=snapshot.prompt_tokens,
        completion_tokens=snapshot.completion_tokens,
        model_calls=snapshot.model_calls,
        usage_uncertain=usage_uncertain,
        resume_ready=resume_ready,
        transcript_ref=transcript_ref,
    )
    return result


def _normalize_subagent_terminal_status(status: str, summary: str) -> str:
    """Fail closed when a nominal worker terminal is really budget exhaustion."""
    if status != "ok":
        return status
    normalized = summary.lower()
    budget_markers = (
        "sub-agent token budget exhausted",
        "sub-agent round budget exhausted",
        "reached maximum tool-call rounds",
        "maximum tool-call rounds reached",
        "max rounds reached",
        "sub-agent tool-call budget exhausted",
    )
    return (
        "failed" if any(marker in normalized for marker in budget_markers) else status
    )


def build_delegated_prompt(
    *,
    task: str,
    parent_context: str,
    context_mode: str,
    worktree_path: str | None = None,
    working_directory: str | None = None,
) -> str:
    """Build the stable child contract used by fresh and resumed workers."""
    sections = [
        (
            "You are a delegated worker with a narrow assigned scope. Do not "
            "create or delegate to other agents and do not modify the root plan. "
            "Use report_progress only for low-frequency human-visible status, "
            "report_to_parent for non-blocking findings/replies, and "
            "request_guidance only when you cannot safely continue without a decision."
        ),
        f"[Parent context mode={context_mode}]\n{parent_context}\n[/Parent context]",
    ]
    if worktree_path:
        sections.append(
            "[Isolated worktree]\n"
            f"{worktree_path}\n"
            "Re-read relevant files because inherited paths or contents may be stale.\n"
            "[/Isolated worktree]"
        )
    elif working_directory:
        sections.append(f"[Execution root]\n{working_directory}\n[/Execution root]")
    sections.extend(
        [
            f"[Assigned task]\n{task}\n[/Assigned task]",
            (
                "When the task is complete, return one final assistant response with "
                "no tool calls. Use exactly these sections in this order:\n"
                "1. Conclusion — answer the assigned task directly.\n"
                "2. Evidence — cite actual reads, commands, tests, or diagnostics.\n"
                "3. Changes and artifacts — list files/artifacts/worktree state, or None.\n"
                "4. Unresolved issues — list blockers/risks/parent decisions, or None.\n"
                "5. Confidence — high, medium, or low, including why confidence is reduced."
            ),
        ]
    )
    return "\n\n".join(sections)


def _resume_workspace_notice() -> str:
    return (
        "[Resume workspace notice]\n"
        "The workspace and parent-owned LSP document generations may have changed "
        "while this worker was parked. Re-read every relevant file or symbol before "
        "relying on observations from the checkpoint. Guidance is not tool approval.\n"
        "[/Resume workspace notice]"
    )


_FINAL_SECTION_PATTERN = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\d+[.)]\s*)?"
    r"(Conclusion|Evidence|Changes and artifacts|Unresolved issues|Confidence)"
    r"\s*(?:—|–|-|:)\s*"
)


class _DelegatedFinalSections(TypedDict):
    conclusion: str
    evidence: list[str]
    artifacts: list[str]
    unresolved: list[str]
    confidence: str | None
    missing: list[str]


def _parse_delegated_final_response(text: str) -> _DelegatedFinalSections:
    """Parse the child contract while keeping runtime facts authoritative."""
    matches = list(_FINAL_SECTION_PATTERN.finditer(text))
    sections: dict[str, str] = {}
    aliases = {
        "conclusion": "conclusion",
        "evidence": "evidence",
        "changes and artifacts": "artifacts",
        "unresolved issues": "unresolved",
        "confidence": "confidence",
    }
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[aliases[match.group(1).lower()]] = text[match.end() : end].strip()

    def _items(name: str) -> list[str]:
        value = sections.get(name, "").strip()
        if not value or value.lower() in {"none", "n/a", "unknown"}:
            return []
        lines = [
            re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
            for line in value.splitlines()
        ]
        return [line for line in lines if line]

    confidence_text = sections.get("confidence", "").strip()
    confidence_match = re.match(r"(?i)^(high|medium|low)\b", confidence_text)
    required = ("conclusion", "evidence", "artifacts", "unresolved", "confidence")
    return {
        "conclusion": sections.get("conclusion", "").strip(),
        "evidence": _items("evidence"),
        "artifacts": _items("artifacts"),
        "unresolved": _items("unresolved"),
        "confidence": confidence_match.group(1).lower() if confidence_match else None,
        "missing": [name for name in required if name not in sections],
    }
