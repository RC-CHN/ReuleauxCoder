"""Validated semantic checkpoint summaries for weak-model-safe compaction."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from reuleauxcoder.services.llm.client import LLM

CheckpointKind = Literal["partial_prefix", "phase_checkpoint", "full_recovery"]

SUMMARY_SYSTEM_PROMPT = """Create a coding-agent checkpoint as strict JSON.
Return exactly one JSON object with the schema shown in the user message. Do not
wrap it in markdown or prose. Distinguish completed, inferred, unverified, and
pending work. Never invent authorization, verification, files, errors, or task
completion. Long outputs and diffs must remain artifact references."""

_TOP_LEVEL = {
    "scope",
    "user_intent",
    "decisions",
    "progress",
    "code_state",
    "errors_and_learning",
    "agent_state",
    "pending",
    "provenance",
}


def generate_summary(
    messages: list[dict],
    llm: Optional["LLM"] = None,
    *,
    checkpoint_kind: CheckpointKind = "partial_prefix",
    summarized_history_version: int = 0,
    recent_rounds_preserved: int = 0,
) -> str:
    """Return canonical JSON from deterministic facts plus validated enrichment."""
    deterministic = build_summary_document(
        messages,
        checkpoint_kind=checkpoint_kind,
        summarized_history_version=summarized_history_version,
        recent_rounds_preserved=recent_rounds_preserved,
    )
    enriched = None
    if llm is not None:
        projected = project_summary_input(messages)
        prompt = (
            "Required schema (all keys required):\n"
            f"{json.dumps(_empty_summary_document(), ensure_ascii=False)}\n\n"
            "Deterministic facts must not be removed or contradicted:\n"
            f"{json.dumps(deterministic, ensure_ascii=False)}\n\n"
            "Bounded conversation projection:\n"
            f"{json.dumps(projected, ensure_ascii=False)}"
        )
        try:
            response = llm.chat(
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                max_output_tokens=4_096,
            )
            enriched = _parse_summary(response.content or "")
            if enriched is None:
                repair = llm.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Repair the candidate into exactly one strict JSON "
                                "object matching the supplied schema. Do not add facts."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Schema:\n{json.dumps(_empty_summary_document(), ensure_ascii=False)}\n"
                                f"Candidate:\n{str(response.content or '')[:15_000]}"
                            ),
                        },
                    ],
                    max_output_tokens=2_048,
                )
                enriched = _parse_summary(repair.content or "")
        except Exception:
            enriched = None

    document = (
        merge_summary_documents(deterministic, enriched)
        if enriched is not None
        else deterministic
    )
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)


def build_summary_document(
    messages: list[dict],
    *,
    checkpoint_kind: CheckpointKind = "partial_prefix",
    summarized_history_version: int = 0,
    recent_rounds_preserved: int = 0,
) -> dict[str, Any]:
    """Build non-negotiable facts without trusting a summarizer model."""
    users: list[dict[str, str]] = []
    tool_facts: list[str] = []
    files: set[str] = set()
    errors: list[dict[str, str]] = []
    decisions: list[dict[str, Any]] = []
    artifacts: set[str] = set()
    active_subagents: list[str] = []
    pending_approvals: list[str] = []

    for index, message in enumerate(messages):
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        role = message.get("role")
        if role == "user" and not content.startswith("[SESSION_"):
            users.append(
                {
                    "event_ref": f"message:{index}",
                    "text": " ".join(content.split())[:500],
                }
            )
        for match in re.finditer(r"(?:[\w.-]+/)+[\w.-]+|[\w.-]+\.\w{1,8}", content):
            files.add(match.group())
        for match in re.finditer(
            r"(?:artifact|transcript)[_ ]ref[=:]\s*([^\s,}\]]+)", content, re.I
        ):
            artifacts.add(match.group(1))
        if role == "tool":
            first = next(
                (line.strip() for line in content.splitlines() if line.strip()), ""
            )
            if first:
                tool_facts.append(
                    f"{message.get('tool_call_id') or 'tool'}: {first[:240]}"
                )
        for line in content.splitlines():
            lowered = line.lower()
            if "error" in lowered or "failed" in lowered:
                errors.append(
                    {
                        "error": line.strip()[:240],
                        "cause": "unknown",
                        "fix_or_status": "unverified",
                        "user_feedback": "",
                    }
                )
            if lowered.startswith("decision:") or " decided " in lowered:
                decisions.append(
                    {
                        "decision": line.strip()[:240],
                        "rationale": "unverified",
                        "alternatives_rejected": [],
                    }
                )
        if "[Sub-agent" in content:
            active_subagents.append(content.splitlines()[0][:160])
        if "approval" in content.lower() and "pending" in content.lower():
            pending_approvals.append(content.splitlines()[0][:160])

    current_goal = users[-1]["text"] if users else "unknown"
    constraints = [
        item["text"]
        for item in users
        if any(
            token in item["text"].lower()
            for token in ("must", "do not", "don't", "不要", "必须", "注意", "记得")
        )
    ]
    authorization = [
        item["text"]
        for item in users
        if any(
            token in item["text"].lower()
            for token in ("allow", "approve", "permission", "允许", "审批", "授权")
        )
    ]
    return {
        "scope": {
            "summarized_history_version": max(0, summarized_history_version),
            "summarized_rounds": _count_user_rounds(messages),
            "recent_rounds_preserved": max(0, recent_rounds_preserved),
            "checkpoint_kind": checkpoint_kind,
        },
        "user_intent": {
            "current_goal": current_goal,
            "explicit_requests": users[-12:],
            "constraints_and_preferences": constraints[-12:],
            "authorization_boundaries": authorization[-8:],
        },
        "decisions": decisions[-12:],
        "progress": {
            "completed": [],
            "current_work": current_goal,
            "verified_by": tool_facts[-12:],
        },
        "code_state": {
            "files_read": sorted(files)[:100],
            "files_changed": [],
            "important_symbols": [],
            "worktrees_and_commits": [],
        },
        "errors_and_learning": errors[-12:],
        "agent_state": {
            "active_subagents": active_subagents[-12:],
            "pending_approvals": pending_approvals[-12:],
            "execution_target": "unknown",
            "relevant_artifacts": sorted(artifacts)[:50],
        },
        "pending": {
            "tasks": [current_goal] if current_goal != "unknown" else [],
            "blockers": [],
            "next_action": "continue current work",
        },
        "provenance": {
            "transcript_ref": "HistoryLedger",
            "source_checkpoint_ids": [],
        },
    }


def validate_summary_document(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL:
        return False
    if len(json.dumps(value, ensure_ascii=False, default=str)) > 20_000:
        return False
    expected_objects = {
        "scope",
        "user_intent",
        "progress",
        "code_state",
        "agent_state",
        "pending",
        "provenance",
    }
    if any(not isinstance(value.get(key), dict) for key in expected_objects):
        return False
    if not isinstance(value.get("decisions"), list) or not isinstance(
        value.get("errors_and_learning"), list
    ):
        return False
    required_nested = {
        "scope": {
            "summarized_history_version",
            "summarized_rounds",
            "recent_rounds_preserved",
            "checkpoint_kind",
        },
        "user_intent": {
            "current_goal",
            "explicit_requests",
            "constraints_and_preferences",
            "authorization_boundaries",
        },
        "progress": {"completed", "current_work", "verified_by"},
        "code_state": {
            "files_read",
            "files_changed",
            "important_symbols",
            "worktrees_and_commits",
        },
        "agent_state": {
            "active_subagents",
            "pending_approvals",
            "execution_target",
            "relevant_artifacts",
        },
        "pending": {"tasks", "blockers", "next_action"},
        "provenance": {"transcript_ref", "source_checkpoint_ids"},
    }
    if not all(set(value[key]) == fields for key, fields in required_nested.items()):
        return False
    scope = value["scope"]
    if not all(
        isinstance(scope[key], int) and not isinstance(scope[key], bool)
        for key in (
            "summarized_history_version",
            "summarized_rounds",
            "recent_rounds_preserved",
        )
    ) or scope["checkpoint_kind"] not in {
        "partial_prefix",
        "phase_checkpoint",
        "full_recovery",
    }:
        return False
    list_fields = (
        ("user_intent", "explicit_requests"),
        ("user_intent", "constraints_and_preferences"),
        ("user_intent", "authorization_boundaries"),
        ("progress", "completed"),
        ("progress", "verified_by"),
        ("code_state", "files_read"),
        ("code_state", "files_changed"),
        ("code_state", "important_symbols"),
        ("code_state", "worktrees_and_commits"),
        ("agent_state", "active_subagents"),
        ("agent_state", "pending_approvals"),
        ("agent_state", "relevant_artifacts"),
        ("pending", "tasks"),
        ("pending", "blockers"),
        ("provenance", "source_checkpoint_ids"),
    )
    if any(not isinstance(value[parent][key], list) for parent, key in list_fields):
        return False
    string_fields = (
        ("user_intent", "current_goal"),
        ("progress", "current_work"),
        ("agent_state", "execution_target"),
        ("pending", "next_action"),
        ("provenance", "transcript_ref"),
    )
    return all(isinstance(value[parent][key], str) for parent, key in string_fields)


def merge_summary_documents(base: dict, enrichment: dict | None) -> dict:
    """Merge only additive evidence; deterministic scalar facts always win."""
    if enrichment is None or not validate_summary_document(enrichment):
        return base

    def merge(left, right):
        if isinstance(left, dict) and isinstance(right, dict):
            return {key: merge(left[key], right.get(key)) for key in left}
        if isinstance(left, list) and isinstance(right, list):
            result = list(left)
            seen = {
                json.dumps(item, ensure_ascii=False, sort_keys=True) for item in result
            }
            for item in right:
                marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if marker not in seen:
                    result.append(item)
                    seen.add(marker)
            return result[:100]
        if left in ("", "unknown", None) and right not in ("", None):
            return right
        return left

    return merge(base, enrichment)


def project_summary_input(
    messages: list[dict], *, max_rounds: int = 24, max_chars: int = 30_000
) -> list[dict]:
    """Bound input on complete API-round boundaries with an explicit loss marker."""
    from reuleauxcoder.domain.context.rounds import group_api_rounds

    rounds = group_api_rounds(messages)
    omitted_rounds = max(0, len(rounds) - max_rounds)
    selected = rounds[omitted_rounds:]

    def render() -> list[dict]:
        projected: list[dict] = []
        if omitted_rounds:
            projected.append(
                {
                    "role": "system",
                    "content": (
                        "[summary input loss marker: "
                        f"{omitted_rounds} older complete API rounds omitted]"
                    ),
                }
            )
        for round_ in selected:
            for message in round_.messages:
                projected.append(_project_summary_message(message))
        return projected

    projected = render()
    while (
        len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")))
        > max_chars
        and len(selected) > 1
    ):
        selected = selected[1:]
        omitted_rounds += 1
        projected = render()
    if len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"))) > max_chars:
        # One complete API round may itself exceed the summarizer budget. Keep
        # every protocol item/call ID but compact payloads inside that round.
        for item in projected:
            content = str(item.get("content") or "")
            if len(content) > 500:
                item["content"] = (
                    content[:220]
                    + "\n… [summary projection payload omitted] …\n"
                    + content[-220:]
                )
            for call in item.get("tool_calls") or []:
                function = call.get("function") or {}
                if function.get("arguments"):
                    function["arguments"] = "[arguments omitted; call ID preserved]"
    return projected


def _project_summary_message(message: dict) -> dict:
    role = message.get("role", "unknown")
    raw_content = message.get("content")
    if isinstance(raw_content, list):
        parts: list[str] = []
        for part in raw_content:
            if isinstance(part, dict) and part.get("type") in {
                "image",
                "image_url",
                "document",
                "input_image",
            }:
                source = part.get("name") or part.get("id") or "embedded"
                parts.append(f"[{part.get('type')} source={source}]")
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        content = "\n".join(item for item in parts if item)
    else:
        content = str(raw_content or "")
    if role == "tool" and len(content) > 1_200:
        lines = content.splitlines()
        content = "\n".join(
            lines[:4] + ["… [artifact-backed output omitted] …"] + lines[-4:]
        )
    item = {
        "role": role,
        "content": content[:2_400],
    }
    if message.get("tool_call_id"):
        item["tool_call_id"] = message["tool_call_id"]
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            projected_call = dict(call)
            function = dict(projected_call.get("function") or {})
            arguments = function.get("arguments")
            if arguments is not None:
                function["arguments"] = str(arguments)[:1_200]
            projected_call["function"] = function
            calls.append(projected_call)
        item["tool_calls"] = calls
    return item


def flatten_messages(messages: list[dict], truncate: int = 1200) -> str:
    """Compatibility human projection used by diagnostics/tests."""
    parts = []
    for message in messages:
        role = message.get("role", "?")
        text = str(message.get("content") or "")
        if text:
            if len(text) > truncate:
                half = truncate // 2
                text = text[:half] + "\n…\n" + text[-half:]
            parts.append(f"[{role}] {text}")
    return "\n".join(parts)


def extract_key_info(messages: list[dict]) -> str:
    document = build_summary_document(messages)
    parts = []
    files = document["code_state"]["files_read"]
    if files:
        parts.append(f"Files touched: {', '.join(files[:20])}")
    errors = document["errors_and_learning"]
    if errors:
        parts.append(f"Errors seen: {'; '.join(item['error'] for item in errors[:5])}")
    decisions = document["decisions"]
    if decisions:
        parts.append(
            f"Decisions: {'; '.join(item['decision'] for item in decisions[:3])}"
        )
    return "\n".join(parts) or "(no extractable context)"


def build_summary_skeleton(messages: list[dict]) -> str:
    """Compatibility name for the deterministic canonical document."""
    return json.dumps(
        build_summary_document(messages), ensure_ascii=False, sort_keys=True
    )


def _parse_summary(text: str) -> dict | None:
    try:
        value = json.loads(text.strip())
    except (TypeError, ValueError):
        return None
    return value if validate_summary_document(value) else None


def _empty_summary_document() -> dict:
    return build_summary_document([])


def _count_user_rounds(messages: list[dict]) -> int:
    return sum(message.get("role") == "user" for message in messages)
